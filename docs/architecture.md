# Architecture

## System

```mermaid
flowchart LR
    browser["Browser<br/>web/ (livekit-client)<br/>mini-kulu.esakrissa.com"]
    dns["Cloudflare DNS<br/>CNAME → Google<br/>(DNS only)"]

    subgraph livekit["LiveKit Cloud"]
        sfu["Media server (SFU)<br/>rooms, WebRTC"]
        dispatch["Agent dispatch<br/>agent name: mini-kulu"]
        subgraph inference["LiveKit Inference gateway"]
            stt["STT<br/>AssemblyAI Universal 3.6 Pro"]
            llm["LLM<br/>Gemma 4 31B → GPT-4.1 mini"]
            tts["TTS<br/>Fish Audio S2.1 → Cartesia Sonic 3"]
        end
    end

    eleven["ElevenLabs TTS<br/>(optional, own key)"]

    subgraph gcp["GCP project (OpenTofu, free tier)"]
        run["Cloud Run: mini-kulu-client<br/>token server + page<br/>passcode, rate limits<br/>scale to zero"]
        vm["e2-micro VM<br/>agent worker (systemd)<br/>VAD, turn handling,<br/>watchdog + fallbacks"]
        sm["Secret Manager<br/>LiveKit key/secret, passcode,<br/>OTLP headers"]
        src["GCS<br/>agent source,<br/>content-addressed"]
        ar["Artifact Registry<br/>client image"]
    end

    grafana["Grafana Cloud<br/>Prometheus + dashboard"]

    browser -. "name lookup" .-> dns
    browser -- "1 passcode, then POST /api/token" --> run
    browser -- "2 join room (WebRTC audio)" --> sfu
    sfu --> dispatch -- "3 job" --> vm
    vm <-- "4 audio both ways (WSS + UDP)<br/>+ transcript text stream" --> sfu
    vm -- "5 speech, text, tokens" --> inference
    vm -. "voice, ElevenLabs mode" .-> eleven
    vm -- "turn latency (OTLP/HTTP)" --> grafana
    sm -. "read at boot / cold start" .-> vm
    sm -.-> run
    src -. "read at boot" .-> vm
    ar -. "image" .-> run
```

- **The VM only dials out.** It registers with LiveKit as a worker for `mini-kulu`, then
  waits for jobs; nothing listens on a public port (SSH goes through Identity-Aware Proxy).
- **The browser never talks to the agent directly.** Its audio goes to LiveKit's media
  server; the agent joins the same room as another participant.
- **The token server is the only place the LiveKit secret meets the browser path.** It signs
  a short-lived token for one random room that also dispatches the agent. Hosted, it runs on
  Cloud Run behind a passcode (traded for a signed, HttpOnly session cookie) with per-IP rate
  limits and a daily cap on new calls (`agent/access.py`); the same server runs on localhost
  for development. It's out of the call path once the browser has joined.
- **The page shows the call as it happens.** Waveforms come from Web Audio analysers on both
  audio tracks; the agent's words arrive as a LiveKit text stream synchronized with its
  speech, so the transcript and live caption fill in word by word.
- **Models run behind LiveKit Inference**: one credential, swappable providers. Each of LLM
  and TTS has a backup from a different vendor (`agent/llm_watchdog.py`,
  `agent/tts_fallback.py`). ElevenLabs mode (off by default, own key) puts an ElevenLabs
  voice in front of the Inference voices.

## One voice turn, and what each metric measures

```mermaid
sequenceDiagram
    autonumber
    participant U as Caller
    participant LK as LiveKit (SFU)
    participant A as Agent (VM)
    participant I as LiveKit Inference

    U->>LK: speech (WebRTC)
    LK->>A: audio frames
    A->>I: audio stream (STT)
    Note over U,A: caller stops speaking
    I-->>A: final transcript
    Note right of A: stt_final
    A->>A: VAD + turn detector decide the turn is over
    Note right of A: end_of_turn
    A->>I: chat request (LLM, 3 s watchdog)
    I-->>A: first token
    Note right of A: llm_ttft
    A->>I: first sentence (TTS, 5 s timeout)
    I-->>A: first audio
    Note right of A: tts_ttfb
    A->>LK: agent speech + transcript text stream
    LK->>U: audio, words in step with it
    Note over U,A: e2e = end of user speech → agent starts speaking
```

Every stage lands in one OpenTelemetry histogram, `mini_kulu.turn.stage_latency`, with
`stage` and `model` labels ([observability/README.md](../observability/README.md)), so a
failover shows up on the dashboard as a second model.

## Failure handling

| Failure | What happens | Where |
|---|---|---|
| LLM slow or stalled (no bytes for 3 s) | backup model takes the same turn; background probes switch back when the primary recovers | `agent/llm_watchdog.py` |
| Both LLMs fail | the agent apologizes instead of going silent; the session ends after 3 failed turns in a row | `agent/llm_watchdog.py` |
| TTS errors or no audio for 5 s | backup voice speaks the sentence | `agent/tts_fallback.py` |
| Browser refresh or network drop | agent keeps the session 30 s; rejoining with the same identity continues the conversation | `agent/rejoin.py` |
| Web search or repo read slow or failing (MCP) | the tool returns one sentence after 4 s at most and the agent answers without it; the next call reconnects | `agent/agent_tools.py` |
| VM reboot | systemd restarts the worker; the startup script reinstalls from the pinned release | `infra/startup.sh.tftpl` |
| Microphone blocked or missing | the call stays up; the mic button offers a retry | `web/app.js` |
| Public link abused | passcode required for tokens; per-IP limits (401/429) and 100 new calls a day | `agent/access.py` |

## Deploys

```mermaid
flowchart LR
    pr["Pull request"] --> plan["tofu plan as tofu-planner<br/>(read-only)<br/>+ build the client image"]
    plan --> review["Review the plan<br/>in the job summary"]
    review --> merge["Merge to main"]
    merge --> push["Push the client image<br/>tag = hash of its files"]
    push --> apply["tofu apply<br/>as tofu-deployer<br/>(main only)"]
    apply --> run["Cloud Run: new revision<br/>(only if the image changed)"]
    apply --> upload["Upload agent files<br/>under a new content hash"]
    upload --> replace["Replace the VM<br/>(only if the agent changed)"]
    replace --> boot["Boot: uv sync --frozen,<br/>secrets from Secret Manager,<br/>systemd starts the worker"]
```

GitHub Actions authenticates with Workload Identity Federation: no service account keys
exist. The LiveKit key and secret are ephemeral OpenTofu variables passed through write-only
arguments, so they never appear in a plan or in the state ([infra/README.md](../infra/README.md)).
The custom domain is a Cloud Run domain mapping in OpenTofu; Google issues and renews its
certificate, which is why the Cloudflare record stays DNS only.
