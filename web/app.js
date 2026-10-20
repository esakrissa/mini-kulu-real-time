// mini-kulu browser client: one voice call with the agent over LiveKit.
//
// Refresh handling: the room name and identity live in sessionStorage. A reload within
// REJOIN_WINDOW_MS rejoins the same room as the same identity, and the agent (which waits
// AGENT_REJOIN_GRACE_S = 30 s for exactly this) carries on with the same conversation.
// Short network drops are handled by livekit-client itself (Reconnecting/Reconnected).
//
// Transcript: the agent's words arrive as a text stream per reply, chunk by chunk, in step
// with its audio (LiveKit's synchronized transcription), so they're rendered as they stream.
// The user's words arrive as interim streams (full text so far) and one final per segment;
// consecutive user segments share one bubble until the agent replies.

import {
  ConnectionState,
  DisconnectReason,
  Room,
  RoomEvent,
  Track,
  TokenSource,
  createAudioAnalyser,
} from 'https://cdn.jsdelivr.net/npm/livekit-client@2.22.4/+esm';

const AGENT_NAME = 'mini-kulu';
const SESSION_KEY = 'mini-kulu-session';
const REJOIN_WINDOW_MS = 25_000; // a bit under the agent's 30 s grace
const HANGUP_RPC = 'mini_kulu.hang_up'; // agent/rejoin.py
const MIC_TIMEOUT_MS = 8_000;

const $ = (id) => document.getElementById(id);
const tokens = TokenSource.endpoint('/api/token');
let room = null;
let heartbeat = null;
let startedAt = null;

// -- session persistence -------------------------------------------------------------------

function loadSession() {
  try {
    const s = JSON.parse(sessionStorage.getItem(SESSION_KEY));
    if (s && Date.now() - s.lastSeen < REJOIN_WINDOW_MS) return s;
  } catch {}
  sessionStorage.removeItem(SESSION_KEY);
  return null;
}

function saveSession() {
  if (room?.state !== ConnectionState.Connected) return;
  const s = {
    roomName: room.name,
    identity: room.localParticipant.identity,
    startedAt,
    lastSeen: Date.now(),
  };
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
}

// -- status readouts -----------------------------------------------------------------------

const CONN_LED = { connected: 'ok', connecting: 'warn', reconnecting: 'warn', 'fetching token': 'warn', failed: 'bad' };
const AGENT_LED = { listening: 'ok', thinking: 'warn', speaking: 'ok', initializing: 'warn', left: 'bad' };
const STATE_TEXT = {
  initializing: 'agent joining',
  listening: 'listening',
  thinking: 'thinking',
  speaking: 'speaking',
  idle: 'idle',
  left: 'agent left',
};

let agentState = null;
let thinkingSince = null;
let pendingLatency = null;

function setConnection(text) {
  $('conn').textContent = text;
  $('conn-led').dataset.on = CONN_LED[text] ?? '';
}

function setAgentState(state) {
  agentState = state ?? null;
  $('agent').textContent = state ?? '–';
  $('agent-led').dataset.on = AGENT_LED[state] ?? '';
  $('state-text').textContent = STATE_TEXT[state] ?? (room ? 'waiting for the agent' : 'ready');

  // Reply latency as the page sees it: from the agent deciding the user is done (thinking)
  // to its first audio (speaking). The agent-side metric (README) also counts end of turn.
  if (state === 'thinking') thinkingSince = performance.now();
  if (state === 'speaking' && thinkingSince) {
    pendingLatency = (performance.now() - thinkingSince) / 1000;
    thinkingSince = null;
    attachLatency();
  }
  if (state === 'listening' || state === 'idle') thinkingSince = null;
}

function tickTimer() {
  if (!startedAt) return ($('timer').textContent = '00:00');
  const s = Math.floor((Date.now() - startedAt) / 1000);
  $('timer').textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}
setInterval(tickTimer, 1000);

// -- transcript ----------------------------------------------------------------------------

const list = $('transcript');
let userBubble = null; // the bubble the next user segment joins, until the agent replies
let lastAgent = null;
let turns = 0;

function clock() {
  if (!startedAt) return '';
  const s = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function append(el) {
  $('empty')?.remove();
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  list.append(el);
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function keepScrolled() {
  if (list.scrollHeight - list.scrollTop - list.clientHeight < 160) list.scrollTop = list.scrollHeight;
}

function note(text) {
  const li = document.createElement('li');
  li.className = 'note mono';
  li.textContent = text;
  append(li);
}

function bubble(who) {
  const li = document.createElement('li');
  li.className = `msg ${who}`;
  li.innerHTML = '<div class="meta mono"><span class="who"></span><time></time></div><p></p>';
  li.querySelector('.who').textContent = who === 'agent' ? 'kulu' : 'you';
  li.querySelector('time').textContent = clock();
  append(li);
  return li;
}

function attachLatency() {
  // The speaking state and the reply's text stream arrive in either order.
  if (pendingLatency == null || !lastAgent || lastAgent.querySelector('.lat')) return;
  if (performance.now() - lastAgent.createdAt > 3000) return;
  const chip = document.createElement('span');
  chip.className = 'lat';
  chip.title = 'From the agent deciding you were done to its first audio, as seen by this page';
  chip.textContent = `${pendingLatency.toFixed(2)} s`;
  lastAgent.querySelector('.meta').append(chip);
  pendingLatency = null;
}

function showUserText(segmentId, text, final) {
  if (!userBubble) userBubble = bubble('user');
  const p = userBubble.querySelector('p');
  let span = p.querySelector(`span[data-segment="${CSS.escape(segmentId)}"]`);
  if (!span) {
    span = document.createElement('span');
    span.dataset.segment = segmentId;
    if (p.childElementCount) p.append(' ');
    p.append(span);
  }
  span.textContent = text;
  span.classList.toggle('interim', !final);
  setCaption(final ? '' : text, 'user');
  keepScrolled();
}

async function streamAgentText(reader, segmentId) {
  userBubble = null;
  let li = segmentId && list.querySelector(`li.agent[data-segment="${CSS.escape(segmentId)}"]`);
  if (!li) {
    li = bubble('agent');
    if (segmentId) li.dataset.segment = segmentId;
    li.createdAt = performance.now();
    lastAgent = li;
    $('turns').textContent = ++turns;
    attachLatency();
  }
  const p = li.querySelector('p');
  li.classList.add('speaking');
  setCaption('', 'agent');
  try {
    for await (const chunk of reader) {
      p.textContent += chunk;
      addCaptionWords(chunk);
      keepScrolled();
    }
  } catch (err) {
    // The sender aborted the stream (e.g. the user interrupted), or we reconnected mid-stream.
    console.warn('transcription stream ended early', err);
  } finally {
    li.classList.remove('speaking');
    p.textContent = p.textContent.trim();
  }
}

// -- live caption under the waveform -------------------------------------------------------

const caption = $('caption');
const CAPTION_MAX_CHARS = 160;

function setCaption(text, who) {
  caption.className = `caption ${who}`;
  caption.textContent = text.length > CAPTION_MAX_CHARS ? `…${text.slice(-CAPTION_MAX_CHARS)}` : text;
}

function addCaptionWords(chunk) {
  if (caption.classList.contains('user')) setCaption('', 'agent');
  const span = document.createElement('span');
  span.className = 'word';
  span.textContent = chunk;
  caption.append(span);
  // Keep the last couple of lines: drop whole words from the front.
  while (caption.textContent.length > CAPTION_MAX_CHARS && caption.firstChild) caption.firstChild.remove();
}

// -- waveform ------------------------------------------------------------------------------

const canvas = $('wave');
const ctx = canvas.getContext('2d');
const BARS = 44;
const heights = new Float32Array(BARS);
const analysers = { agent: null, user: null };
let colors = {};

function readColors() {
  const css = getComputedStyle(document.documentElement);
  colors = {
    agent: css.getPropertyValue('--brand').trim(),
    user: css.getPropertyValue('--user').trim(),
    idle: css.getPropertyValue('--wave-idle').trim(),
  };
}
readColors();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', readColors);

function sizeCanvas() {
  const dpr = devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * dpr;
  canvas.height = canvas.clientHeight * dpr;
}
new ResizeObserver(sizeCanvas).observe(canvas);

function watch(kind, track) {
  analysers[kind]?.cleanup();
  analysers[kind] = null;
  if (!track) return;
  try {
    const a = createAudioAnalyser(track, { fftSize: 256, smoothingTimeConstant: 0.75 });
    a.bins = new Uint8Array(a.analyser.frequencyBinCount);
    analysers[kind] = a;
  } catch (err) {
    console.warn(`no ${kind} waveform`, err);
  }
}

// Speech energy sits in the low bins; spread them over half the bars, mirrored from the middle.
function levels(a) {
  if (!a) return null;
  a.analyser.getByteFrequencyData(a.bins);
  const half = BARS / 2;
  const out = new Float32Array(half);
  const span = Math.floor(a.bins.length * 0.45);
  let sum = 0;
  for (let i = 0; i < half; i++) {
    const from = Math.floor((i / half) * span);
    const to = Math.max(from + 1, Math.floor(((i + 1) / half) * span));
    let peak = 0;
    for (let j = from; j < to; j++) peak = Math.max(peak, a.bins[j]);
    out[i] = peak / 255;
    sum += out[i];
  }
  out.energy = sum / half;
  return out;
}

function draw(t) {
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const agent = levels(analysers.agent);
  const user = levels(analysers.user);
  const agentLoud = (agent?.energy ?? 0) >= (user?.energy ?? 0);
  const active = agentLoud ? agent : user;
  const speaking = (active?.energy ?? 0) > 0.04;

  const half = BARS / 2;
  const gap = w / BARS;
  const barW = Math.max(2, gap * 0.42);
  for (let i = 0; i < BARS; i++) {
    const k = i < half ? half - 1 - i : i - half; // distance from the middle
    let target;
    if (speaking) {
      target = active[k] * (1 - k / (half * 1.6));
    } else if (agentState === 'thinking') {
      // A highlight sweeping across, like a progress scan.
      const pos = ((t / 900) % 1) * BARS;
      target = 0.08 + 0.32 * Math.exp(-((i - pos) ** 2) / 10);
    } else {
      // Breathing at rest; a little livelier while a call is up.
      const amp = room ? 0.07 : 0.045;
      target = amp * (1 + Math.sin(t / 650 + i * 0.42)) + 0.02;
    }
    heights[i] += (target - heights[i]) * (target > heights[i] ? 0.45 : 0.14);
    const bh = Math.max(barW, heights[i] * h * 0.92);
    ctx.fillStyle = speaking ? (agentLoud ? colors.agent : colors.user) : agentState === 'thinking' ? colors.agent : colors.idle;
    ctx.globalAlpha = speaking ? 0.35 + 0.65 * Math.min(1, heights[i] * 2.2) : 0.9;
    const x = i * gap + (gap - barW) / 2;
    ctx.beginPath();
    ctx.roundRect(x, (h - bh) / 2, barW, bh, barW / 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

// -- controls ------------------------------------------------------------------------------

function setInCall(inCall) {
  const call = $('call');
  call.classList.toggle('live', inCall);
  call.setAttribute('aria-label', inCall ? 'Hang up' : 'Start call');
  $('mic').disabled = !inCall;
  $('title').textContent = inCall ? 'On a call with Kulu' : 'Talk to the agent';
  if (!inCall) {
    $('resume-audio').hidden = true;
    setMicState(true);
    watch('agent', null);
    watch('user', null);
    setCaption('', 'agent');
  }
}

function setMicState(on, warn = false) {
  const mic = $('mic');
  mic.setAttribute('aria-pressed', String(on));
  mic.setAttribute('aria-label', warn ? 'Enable microphone' : on ? 'Mute microphone' : 'Unmute microphone');
  mic.classList.toggle('warn', warn);
}

// -- call ----------------------------------------------------------------------------------

function wireRoom(r) {
  r.on(RoomEvent.ConnectionStateChanged, (state) => setConnection(state))
    .on(RoomEvent.Reconnecting, () => note('network drop, reconnecting'))
    .on(RoomEvent.Reconnected, () => note('reconnected'))
    .on(RoomEvent.Disconnected, (reason) => {
      clearInterval(heartbeat);
      room = null;
      startedAt = null;
      setInCall(false);
      setAgentState(null);
      if (reason !== DisconnectReason.CLIENT_INITIATED) note(`disconnected (${reason})`);
    })
    .on(RoomEvent.TrackSubscribed, (track, _pub, participant) => {
      if (track.kind !== Track.Kind.Audio) return;
      $('audio').append(track.attach());
      if (participant.isAgent) watch('agent', track);
    })
    .on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()))
    .on(RoomEvent.LocalTrackPublished, (pub) => {
      if (pub.source === Track.Source.Microphone) watch('user', pub.track);
    })
    .on(RoomEvent.AudioPlaybackStatusChanged, () => {
      // After a refresh there's no user gesture yet, so the browser may block playback.
      $('resume-audio').hidden = r.canPlaybackAudio;
    })
    .on(RoomEvent.ParticipantAttributesChanged, (changed, p) => {
      if ('lk.agent.state' in changed) setAgentState(p.attributes['lk.agent.state']);
    })
    .on(RoomEvent.ParticipantConnected, (p) => {
      if (p.isAgent) setAgentState(p.attributes['lk.agent.state'] ?? 'initializing');
    })
    .on(RoomEvent.ParticipantDisconnected, (p) => {
      if (p.isAgent) setAgentState('left');
    });

  r.registerTextStreamHandler('lk.transcription', async (reader, participant) => {
    const attrs = reader.info.attributes ?? {};
    const segmentId = attrs['lk.segment_id'] ?? reader.info.id;
    if (participant.identity !== r.localParticipant.identity) {
      await streamAgentText(reader, segmentId);
      return;
    }
    let text;
    try {
      text = (await reader.readAll()).trim();
    } catch (err) {
      console.warn('transcription stream failed', err);
      return;
    }
    if (text) showUserText(segmentId, text, attrs['lk.transcription_final'] === 'true');
  });
}

async function connect(session) {
  setConnection('fetching token');
  const { serverUrl, participantToken } = await tokens.fetch({
    agentName: AGENT_NAME,
    roomName: session?.roomName,
    participantIdentity: session?.identity,
  });

  room = new Room({ adaptiveStream: true, dynacast: true });
  wireRoom(room);
  await room.connect(serverUrl, participantToken);
  startedAt = session?.startedAt ?? Date.now();
  setInCall(true);
  await enableMic();

  // On a rejoin the agent is already here, so ParticipantConnected won't fire for it.
  for (const p of room.remoteParticipants.values()) {
    if (p.isAgent) setAgentState(p.attributes['lk.agent.state']);
  }
  $('resume-audio').hidden = room.canPlaybackAudio;
  saveSession();
  heartbeat = setInterval(saveSession, 5_000);
  note(session ? 'rejoined the call' : 'call started');
}

// A blocked or missing mic shouldn't drop the call: stay connected (the agent keeps
// waiting) and offer a retry, e.g. after the user allows the permission prompt.
async function enableMic() {
  try {
    // getUserMedia can hang instead of failing, e.g. when macOS blocks the browser's mic
    // access at the OS level. Give up after a while so the user gets told.
    await Promise.race([
      room.localParticipant.setMicrophoneEnabled(true),
      new Promise((_, reject) =>
        setTimeout(() => reject(new DOMException('no answer from the OS', 'TimeoutError')), MIC_TIMEOUT_MS),
      ),
    ]);
    setMicState(true);
    const pub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
    if (pub?.track) watch('user', pub.track);
  } catch (err) {
    console.warn('microphone unavailable', err);
    note(`microphone unavailable (${err.name ?? err}): allow it, then press the mic button`);
    setMicState(false, true);
  }
}

async function start(session = null) {
  const call = $('call');
  call.disabled = true;
  call.classList.add('busy');
  try {
    await connect(session);
  } catch (err) {
    console.error(err);
    note(`could not connect: ${err.message ?? err}`);
    setConnection('failed');
    sessionStorage.removeItem(SESSION_KEY);
    await room?.disconnect();
  } finally {
    call.disabled = false;
    call.classList.remove('busy');
  }
}

async function hangUp() {
  sessionStorage.removeItem(SESSION_KEY);
  // Tell the agent this is a hang-up, not a refresh, so it ends now instead of waiting 30 s.
  const agent = room && [...room.remoteParticipants.values()].find((p) => p.isAgent);
  if (agent) {
    await room.localParticipant
      .performRpc({ destinationIdentity: agent.identity, method: HANGUP_RPC, payload: '' })
      .catch((err) => console.warn('hang-up RPC failed; the agent will time out instead', err));
  }
  note('call ended');
  await room?.disconnect();
}

$('call').onclick = () => (room ? hangUp() : start());
$('mic').onclick = async () => {
  if (!room) return;
  if ($('mic').classList.contains('warn')) return enableMic();
  const on = !room.localParticipant.isMicrophoneEnabled;
  await room.localParticipant.setMicrophoneEnabled(on);
  setMicState(on);
};
$('resume-audio').onclick = () => room?.startAudio();

// Stamp the time we left, so the next load knows whether the agent is still waiting.
window.addEventListener('pagehide', saveSession);

const previous = loadSession();
if (previous) start(previous);
