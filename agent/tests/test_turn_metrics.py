import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from livekit.agents import llm
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from turn_metrics import (
    BUCKETS_S,
    METRIC_NAME,
    TurnMetrics,
    build_meter_provider,
    stage_latencies,
)

STT = {"model_name": "assemblyai/universal-3-6-pro", "model_provider": "livekit"}
LLM = {"model_name": "google/gemma-4-31b-it", "model_provider": "livekit"}
TTS = {"model_name": "fishaudio/s2.1-pro", "model_provider": "livekit"}


def user_msg(**metrics) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=["hi"], metrics={"stt_metadata": STT, **metrics})


def reply_msg(**metrics) -> llm.ChatMessage:
    return llm.ChatMessage(
        role="assistant",
        content=["hello"],
        metrics={"llm_metadata": LLM, "tts_metadata": TTS, **metrics},
    )


def in_memory() -> tuple[TurnMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return TurnMetrics(build_meter_provider(readers=[reader])), reader


def histogram_points(reader: InMemoryMetricReader) -> dict[tuple[str, str], object]:
    data = reader.get_metrics_data()
    points = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                assert metric.name == METRIC_NAME
                for p in metric.data.data_points:
                    points[(p.attributes["stage"], p.attributes["model"])] = p
    return points


def test_user_message_stages():
    msg = user_msg(transcription_delay=0.21, end_of_turn_delay=0.48, stopped_speaking_at=1.0)
    assert stage_latencies(msg) == {
        "stt_final": (0.21, STT["model_name"]),
        "end_of_turn": (0.48, STT["model_name"]),
    }


def test_assistant_message_stages():
    msg = reply_msg(llm_node_ttft=0.35, tts_node_ttfb=0.25, e2e_latency=1.1)
    assert stage_latencies(msg) == {
        "llm_ttft": (0.35, LLM["model_name"]),
        "tts_ttfb": (0.25, TTS["model_name"]),
        "e2e": (1.1, LLM["model_name"]),
    }


def test_missing_and_negative_values_are_skipped():
    # The greeting has no user turn (no e2e); the SDK uses -1 for "no first token".
    msg = reply_msg(llm_node_ttft=-1.0, tts_node_ttfb=0.3)
    assert stage_latencies(msg) == {"tts_ttfb": (0.3, TTS["model_name"])}


def test_unknown_model_and_other_roles():
    msg = llm.ChatMessage(role="assistant", content=["x"], metrics={"llm_node_ttft": 0.2})
    assert stage_latencies(msg) == {"llm_ttft": (0.2, "unknown")}
    assert stage_latencies(llm.ChatMessage(role="system", content=["x"])) == {}


def test_records_into_histogram_with_our_buckets():
    metrics, reader = in_memory()
    metrics.record(user_msg(transcription_delay=0.21, end_of_turn_delay=0.48))
    metrics.record(reply_msg(llm_node_ttft=0.35, tts_node_ttfb=0.25, e2e_latency=1.1))
    metrics.record(reply_msg(llm_node_ttft=0.45, tts_node_ttfb=0.3, e2e_latency=1.3))

    points = histogram_points(reader)
    e2e = points[("e2e", LLM["model_name"])]
    assert e2e.count == 2
    assert e2e.sum == pytest.approx(2.4)
    assert tuple(e2e.explicit_bounds) == BUCKETS_S
    assert points[("stt_final", STT["model_name"])].count == 1
    assert points[("tts_ttfb", TTS["model_name"])].count == 2


def test_handler_ignores_non_messages_and_never_raises():
    metrics, reader = in_memory()
    metrics.on_conversation_item_added(SimpleNamespace(item=llm.AgentHandoff(new_agent_id="x")))
    assert histogram_points(reader) == {}

    metrics.record = lambda _msg: 1 / 0  # a bug in recording must not reach the session
    metrics.on_conversation_item_added(SimpleNamespace(item=reply_msg(llm_node_ttft=0.2)))


def test_no_endpoint_means_no_exporter():
    provider = build_meter_provider(env={})
    try:
        assert provider.force_flush(1000)  # nothing to push, nothing to fail
    finally:
        provider.shutdown()


async def test_pushes_otlp_to_the_configured_endpoint(monkeypatch):
    """End to end over HTTP: a fake OTLP receiver gets our histogram on flush."""
    received: list[ExportMetricsServiceRequest] = []
    got_one = asyncio.Event()

    async def v1_metrics(request: web.Request) -> web.Response:
        req = ExportMetricsServiceRequest()
        req.ParseFromString(await request.read())
        received.append(req)
        received_headers.append(request.headers.get("Authorization"))
        got_one.set()
        return web.Response(status=200)

    received_headers: list[str | None] = []
    app = web.Application()
    app.router.add_post("/v1/metrics", v1_metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic dGVzdDp0ZXN0",
        "OTEL_METRIC_EXPORT_INTERVAL": "600000",  # only our flush should send
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    metrics = TurnMetrics(build_meter_provider(env=env))
    try:
        metrics.record(reply_msg(llm_node_ttft=0.35, tts_node_ttfb=0.25, e2e_latency=1.1))
        await metrics.flush()
        await asyncio.wait_for(got_one.wait(), timeout=5)
    finally:
        await asyncio.to_thread(metrics.provider.shutdown)
        await runner.cleanup()

    names = {
        m.name: m
        for rm in received[0].resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    stages = {
        kv.value.string_value
        for p in names[METRIC_NAME].histogram.data_points
        for kv in p.attributes
        if kv.key == "stage"
    }
    assert stages == {"llm_ttft", "tts_ttfb", "e2e"}
    assert received_headers[0] == "Basic dGVzdDp0ZXN0"
    resource = {
        kv.key: kv.value.string_value for kv in received[0].resource_metrics[0].resource.attributes
    }
    assert resource["service.name"] == "mini-kulu-agent"
    assert resource["service.instance.id"]
