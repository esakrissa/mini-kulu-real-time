"""Per-stage turn latency, recorded as an OpenTelemetry histogram and pushed over OTLP.

LiveKit Agents puts the timing of each turn on the chat messages it adds to the conversation
(`ChatMessage.metrics`): the user's message carries how long the transcript and the end-of-turn
decision took after they stopped speaking, the agent's reply carries LLM time to first token, TTS
time to first audio and the end-to-end latency. We listen for `conversation_item_added` and record
each stage as it happens, so a dashboard sees turns live rather than when the session ends.

    stage        message    what it measures (seconds)
    stt_final    user       end of user speech -> final transcript
    end_of_turn  user       end of user speech -> turn detector decides the user is done
    llm_ttft     assistant  LLM request -> first token
    tts_ttfb     assistant  first text sent to TTS -> first audio back
    e2e          assistant  end of user speech -> agent starts speaking
    tool_call    (a tool)   the agent calls a tool (web search, a repo file) -> its result;
                            `model` is the tool's name; recorded by agent_tools, not a message

All stages go into one histogram, `mini_kulu.turn.stage_latency` (unit s), labelled by `stage`
and `model`, so p50/p95 per stage is a single query; a TTS fallback shows up as a second `model`.

Why our own MeterProvider rather than the global one: the SDK claims the global provider for
LiveKit Cloud's agent observability, and its own turn histograms are only written at session end,
labelled with room and job IDs, and use default buckets that are too coarse below one second.

Export is configured with the standard OTel environment variables: set
OTEL_EXPORTER_OTLP_ENDPOINT (plus OTEL_EXPORTER_OTLP_HEADERS for auth, e.g. Grafana Cloud's OTLP
gateway) to push; without it the latencies are only logged, one line per message.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Mapping
from typing import Any

from livekit.agents import llm
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger("mini-kulu-agent")

METRIC_NAME = "mini_kulu.turn.stage_latency"

# Bucket edges in seconds: fine below 1 s, where the stages live, coarse up to 10 s for stalls.
BUCKETS_S = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0,
             5.0, 7.5, 10.0)  # fmt: skip

# stage -> (MetricsReport key, MetricsReport key of the model that stage belongs to)
USER_STAGES = {
    "stt_final": ("transcription_delay", "stt_metadata"),
    "end_of_turn": ("end_of_turn_delay", "stt_metadata"),
}
ASSISTANT_STAGES = {
    "llm_ttft": ("llm_node_ttft", "llm_metadata"),
    "tts_ttfb": ("tts_node_ttfb", "tts_metadata"),
    "e2e": ("e2e_latency", "llm_metadata"),
}
STAGES = (*USER_STAGES, *ASSISTANT_STAGES)
TOOL_STAGE = "tool_call"

FLUSH_TIMEOUT_S = 5.0


def stage_latencies(msg: llm.ChatMessage) -> dict[str, tuple[float, str]]:
    """The stages this message has timings for: {stage: (seconds, model)}.

    Missing or negative values are skipped (the SDK uses -1 for "no first token", and an
    interrupted reply may have no audio at all).
    """
    stages = {"user": USER_STAGES, "assistant": ASSISTANT_STAGES}.get(msg.role, {})
    report: Mapping[str, Any] = msg.metrics or {}
    out: dict[str, tuple[float, str]] = {}
    for stage, (key, meta_key) in stages.items():
        value = report.get(key)
        if isinstance(value, int | float) and value >= 0:
            out[stage] = (float(value), (report.get(meta_key) or {}).get("model_name", "unknown"))
    return out


class TurnMetrics:
    """Records stage latencies from conversation items into a stage-latency histogram."""

    def __init__(self, provider: MeterProvider) -> None:
        self.provider = provider
        self._histogram = provider.get_meter("mini-kulu-agent").create_histogram(
            METRIC_NAME, unit="s", description="Voice-agent turn latency per pipeline stage"
        )

    def record(self, msg: llm.ChatMessage) -> dict[str, tuple[float, str]]:
        stages = stage_latencies(msg)
        for stage, (seconds, model) in stages.items():
            self._histogram.record(seconds, attributes={"stage": stage, "model": model})
        if stages:
            logger.info(
                "turn latency (%s): %s",
                msg.role,
                " ".join(f"{s}={v:.3f}s" for s, (v, _) in stages.items()),
                extra={f"latency_{s}": v for s, (v, _) in stages.items()},
            )
        return stages

    def record_tool_call(self, tool: str, seconds: float) -> None:
        self._histogram.record(seconds, attributes={"stage": TOOL_STAGE, "model": tool})
        logger.info("tool latency: %s=%.3fs", tool, seconds, extra={"latency_tool_call": seconds})

    def on_conversation_item_added(self, ev: Any) -> None:
        """AgentSession `conversation_item_added` handler. Never raises into the session."""
        if not isinstance(ev.item, llm.ChatMessage):
            return
        try:
            self.record(ev.item)
        except Exception:
            logger.exception("failed to record turn latency")

    async def flush(self) -> None:
        """Push what's recorded now (e.g. at the end of a job): the next periodic export
        might never come if the process is recycled or the container stops."""
        ok = await asyncio.to_thread(self.provider.force_flush, int(FLUSH_TIMEOUT_S * 1000))
        if not ok:
            logger.warning("turn metrics flush timed out")


def build_meter_provider(
    readers: list[MetricReader] | None = None, env: Mapping[str, str] = os.environ
) -> MeterProvider:
    """A private MeterProvider with our histogram buckets.

    Without explicit readers, adds an OTLP/HTTP exporter when OTEL_EXPORTER_OTLP_ENDPOINT (or
    the metrics-specific endpoint) is set; the exporter reads endpoint, headers and the export
    interval (OTEL_METRIC_EXPORT_INTERVAL, default 60 s) from the standard environment variables.
    """
    if readers is None:
        readers = []
        if env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
        logger.info("turn metrics: %s", "exporting over OTLP" if readers else "log only")
    # Job processes run side by side and each pushes its own cumulative series, so each one
    # needs a distinct instance id or the backend would merge them into one broken series.
    resource = Resource.create(
        {"service.name": "mini-kulu-agent", "service.instance.id": uuid.uuid4().hex}
    )
    view = View(
        instrument_name=METRIC_NAME,
        aggregation=ExplicitBucketHistogramAggregation(boundaries=BUCKETS_S),
    )
    return MeterProvider(
        resource=resource, metric_readers=readers, views=[view], shutdown_on_exit=True
    )


_process_metrics: TurnMetrics | None = None


def process_turn_metrics() -> TurnMetrics:
    """One TurnMetrics per process: job processes are reused across calls, and a provider per
    call would leave an exporter thread behind each time."""
    global _process_metrics
    if _process_metrics is None:
        _process_metrics = TurnMetrics(build_meter_provider())
    return _process_metrics
