# Observability

## Turn latency metric

The agent records every turn's latency per pipeline stage in one OpenTelemetry histogram
([`agent/turn_metrics.py`](../agent/turn_metrics.py)) and pushes it over OTLP/HTTP.

| | |
|---|---|
| Name (OTel) | `mini_kulu.turn.stage_latency`, unit `s` |
| Name (Prometheus, after OTLP ingestion) | `mini_kulu_turn_stage_latency_seconds_{bucket,sum,count}` |
| Labels | `stage`, `model`; resource `service.name=mini-kulu-agent`, `service.instance.id` (one per job process) |
| Buckets (s) | 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10 |

| `stage` | Measured from → to | Source (`ChatMessage.metrics`) |
|---|---|---|
| `stt_final` | end of user speech → final transcript (0 if the transcript was already there) | `transcription_delay` |
| `end_of_turn` | end of user speech → turn detector decides the user is done | `end_of_turn_delay` |
| `llm_ttft` | LLM request → first token | `llm_node_ttft` |
| `tts_ttfb` | first text sent to TTS → first audio back | `tts_node_ttfb` |
| `e2e` | end of user speech → agent starts speaking | `e2e_latency` |

`e2e` is roughly `end_of_turn` + `llm_ttft` + `tts_ttfb` (+ a little scheduling): it is what the
user feels. The `model` label separates providers, so a TTS fallback shows up as a second series.
Only replies to a user turn have `e2e` (the greeting doesn't).

## Sending it somewhere

Set the standard OTel variables in `agent/.env.local` (locally) or Secret Manager (deployed):

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-<zone>.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 of instance-id:token>
OTEL_METRIC_EXPORT_INTERVAL=60000   # ms; each job also flushes when it ends
```

Without an endpoint nothing is exported; each turn is still logged:

```
turn latency (user): stt_final=0.612s end_of_turn=2.501s
turn latency (assistant): llm_ttft=0.952s tts_ttfb=0.624s e2e=3.155s
```

## Queries (PromQL)

p50 / p95 per stage over the dashboard window:

```promql
histogram_quantile(0.95,
  sum by (le, stage) (rate(mini_kulu_turn_stage_latency_seconds_bucket[$__rate_interval])))
```

Turns per minute: `sum(rate(mini_kulu_turn_stage_latency_seconds_count{stage="e2e"}[5m])) * 60`

Percentiles from buckets are interpolated, so they're only as exact as the bucket edges; the
README's latency table also lists raw p50/p95 from the synthetic caller's own measurements.
