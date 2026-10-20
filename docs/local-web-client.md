# Talking to the agent from a browser

The agent runs on GCP (see `infra/`). The easiest way to talk to it is the hosted page,
[mini-kulu.esakrissa.com](https://mini-kulu.esakrissa.com) (passcode required). For
development, run the same web client on your machine and open it in Safari or Chrome; with
`CLIENT_PASSCODE` set in `agent/.env.local` it asks for the passcode too.

## How the pieces fit

```
Browser ──1. POST /api/token──► token server (your machine, localhost:8080)
   │    ◄──2. signed token────  holds the LiveKit API secret
   │
   └──3. join the room (WebRTC)──► LiveKit Cloud ◄──4. dispatch──► agent worker on GCP
                                        ▲                              │
                                        └──── 5. audio both ways ──────┘
```

1. The page asks the token server for a token.
2. The token server signs one with the LiveKit API secret: one random room, one random identity,
   and a dispatch for the `mini-kulu` agent. The secret never reaches the browser.
3. The browser joins the room on LiveKit Cloud. From here audio goes browser ⇄ LiveKit; the
   token server is out of the call path and doesn't affect latency.
4. LiveKit sees a new room that asks for `mini-kulu` and hands the job to a registered worker:
   the VM on GCP, which dialled out to LiveKit when it booted.
5. The agent joins the room, listens, and answers.

The token server is needed again only to start a new call or to rejoin after a refresh.

## Start and stop

```bash
scripts/web-client.sh start safari    # or: start chrome, or just start
scripts/web-client.sh status
scripts/web-client.sh logs            # follow the server log (Ctrl-C to stop following)
scripts/web-client.sh stop
```

`start` runs `agent/token_server.py` in the background, waits until the page answers and opens
http://localhost:8080. The PID file and log live in `.cache/web-client/` (gitignored).
`PORT=8081 scripts/web-client.sh start` uses another port.

It reads the LiveKit URL, key and secret from `agent/.env.local`, the same file the agent uses
locally.

## Making a call

1. Click **Start call** and allow the microphone.
2. The agent greets you within a couple of seconds. Talk normally; you can interrupt it.
3. Refreshing the page mid-call rejoins the same room and the agent keeps the conversation
   (it waits 30 s for you to come back).
4. **Hang up** ends the agent's session at once.

Each answered turn shows up in the agent's log with its per-stage latency:

```bash
cd infra && tofu output -raw agent_logs | sh
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The mic prompt never appears, or "Permission denied" | Use Safari or Chrome that you opened yourself. Embedded browser panes (IDE previews, automation browsers) often have no real microphone. Browsers only allow the mic on `https://` or `localhost`. |
| Connected, but no agent joins | The agent isn't registered. Check `tofu output -raw agent_logs \| sh` for "registered worker". |
| Two different agents answer on different calls | A local agent (`agent.py dev`) is also registered under `mini-kulu`; LiveKit picks either. Stop the local one. |
| `port 8080 is taken` | Another process uses the port: `PORT=8081 scripts/web-client.sh start`. |
| The agent sounds clipped in a noisy room | Noise cancellation is off on the VM by default (CPU, and the free plan's 100 min/month); set `noise_cancellation = true` in `infra/terraform.tfvars`. |

## Why the client isn't hosted

Anyone who can reach the token endpoint can start sessions that use LiveKit inference credit.
On `localhost` that is only you. Hosting it (a scale-to-zero Cloud Run service would fit) needs
a gate first: a passcode or sign-in on `/api/token`, plus a rate limit. The server listens on
127.0.0.1 by default; a container would set `HOST=0.0.0.0`.
