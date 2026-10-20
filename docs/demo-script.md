# Demo script (2 minutes, Screen Studio)

A 2-minute screen recording that shows the whole path: a live call, a refresh that keeps the
conversation, the latency dashboard, a forced failover, and the infra pipeline. Times are
targets; the voice-over is a guide, not a script to read word for word.

## Before recording (10 minutes)

- [ ] Cloud agent is up: `cd infra && tofu output -raw agent_logs | sh` shows "registered worker".
- [ ] Warm the VM: make one short call ~5 minutes before recording (first call after boot is slow),
      then **let it rest ~10 minutes** so the e2-micro has its burst CPU back for the take.
- [ ] Tabs open in Safari, in this order:
      1. the hosted client (`https://mini-kulu.esakrissa.com`, passcode at hand) or `http://localhost:8080`
      2. Grafana dashboard, time range "Last 30 minutes", refresh 10 s
      3. a merged infra PR on GitHub, scrolled to the `tofu plan` job summary
      4. the repo README, at the architecture diagram
- [ ] For the failover scene, a terminal with the local agent ready (see scene 5).
- [ ] macOS: Do Not Disturb on, hide desktop icons, close Slack/Mail, Safari bookmarks bar off.
- [ ] Screen Studio: record the Safari window (not full screen), **system audio on** (the agent's
      voice) and your mic, auto-zoom on clicks, cursor size slightly larger, 1080p or 1440p export.
- [ ] Wear headphones so the agent's voice doesn't feed back into your mic (or rely on the
      browser's echo cancellation and keep the volume moderate).

## Shot list

### 1. Hook, 0:00–0:12 (README)

Screen: the README architecture diagram.

> "This is a small version of a realtime voice-agent path: browser to LiveKit over WebRTC, a
> Python agent doing speech to text, an LLM and text to speech, all deployed on GCP with OpenTofu
> and measured per stage. Let me call it."

### 2. A live call, 0:12–0:50 (client)

Screen: the client. Enter the passcode, press the **phone** button.

- Wait for the greeting. Point the cursor at the waveform as the agent speaks.
- Ask: *"What's a good way to explain WebRTC to a five-year-old?"*
- Interrupt it halfway with *"Shorter, please."* (shows barge-in).
- Ask: *"What did I ask you first?"* (memory, sets up the next scene).

> "End to end it's about a second from when I stop talking to the first audio. The transcript
> streams in as I talk, and the agent's words appear as it says them."

### 3. Refresh mid-call, 0:50–1:05 (client)

Press **Cmd-R** in the middle of the call. The page rejoins the same room.

- Ask: *"Are you still there? What were we talking about?"*

> "A refresh or a network drop doesn't end the session: the agent waits 30 seconds for the same
> identity to come back, and carries on with the same conversation."

Hang up with the phone button.

### 4. The numbers, 1:05–1:25 (Grafana)

Screen: the Grafana dashboard. Hover the e2e p50/p95 stat, then the per-stage table.

> "Every turn records five stages in one OpenTelemetry histogram: speech to text, end of turn,
> LLM first token, TTS first audio, and end to end. Here's p50 and p95 from the calls you just
> saw. One finding from the load run: on the free shared-core VM, voice detection falls behind
> after a couple of minutes of non-stop audio. That's the first thing I'd size and alert on."

### 5. Failover, 1:25–1:45 (client + Grafana)

Recorded as its own take and cut in. Stop the cloud agent so the local one gets the call:

```bash
gcloud compute ssh mini-kulu-agent --zone us-central1-a --project mini-kulu-realtime \
  --tunnel-through-iap -- sudo systemctl stop mini-kulu-agent
cd agent
AGENT_TTS_VOICES=fishaudio/does-not-exist:x,cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc \
  AGENT_NOISE_CANCELLATION=0 uv run python agent.py dev
```

Show the terminal command for two seconds, then call from `http://localhost:8080` (or the
hosted page) and ask one question. Cut to the "TTS first audio by model" panel showing Cartesia.

> "Each model has a backup from another vendor. Here the primary voice is broken on purpose:
> the backup speaks the same sentence and the call never notices. The LLM has the same
> watchdog: three seconds without tokens and the backup model takes the turn."

Afterwards: stop the local agent, then `... -- sudo systemctl start mini-kulu-agent`.

### 6. Infra, 1:45–2:00 (GitHub)

Screen: the PR's `tofu plan` job summary, then the Actions run list.

> "Infra is OpenTofu: a pull request gets a plan from a read-only identity, merging to main
> applies it with a deploy-only identity, keyless through Workload Identity Federation. It all
> runs inside GCP's free tier. The README has the numbers, the scale notes and the migration
> plan."

End on the README.

## Fallbacks if something goes wrong while recording

| Problem | Do this |
|---|---|
| Agent doesn't join within ~5 s | check the agent logs; the VM may be redeploying (~3 min after a merge) |
| Replies get slow mid-take (2–3 s) | the e2-micro ran out of burst CPU: stop, let it rest 10 minutes, retake |
| Echo / the agent hears itself | headphones, or lower the volume |
| Passcode rejected | the passcode lives in Secret Manager (`mini-kulu-client-passcode`) |
