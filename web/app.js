// mini-kulu browser client: one voice call with the agent over LiveKit.
//
// Refresh handling: the room name and identity live in sessionStorage. A reload within
// REJOIN_WINDOW_MS rejoins the same room as the same identity, and the agent (which waits
// AGENT_REJOIN_GRACE_S = 30 s for exactly this) carries on with the same conversation.
// Short network drops are handled by livekit-client itself (Reconnecting/Reconnected).

import {
  ConnectionState,
  DisconnectReason,
  Room,
  RoomEvent,
  Track,
  TokenSource,
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
  const s = { roomName: room.name, identity: room.localParticipant.identity, lastSeen: Date.now() };
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
}

// -- UI ------------------------------------------------------------------------------------

function setConnection(text) {
  $('conn').textContent = text;
}

function setAgentState(state) {
  $('agent').textContent = state ?? '–';
}

function note(text) {
  const li = document.createElement('li');
  li.className = 'note';
  li.textContent = text;
  $('transcript').append(li);
}

function showLine(segmentId, who, text, final) {
  let li = segmentId && document.querySelector(`li[data-segment="${CSS.escape(segmentId)}"]`);
  if (!li) {
    li = document.createElement('li');
    li.className = who;
    if (segmentId) li.dataset.segment = segmentId;
    $('transcript').append(li);
  }
  li.textContent = text;
  li.classList.toggle('interim', !final);
  li.scrollIntoView({ block: 'end' });
}

function setInCall(inCall) {
  $('start').hidden = inCall;
  $('hangup').hidden = !inCall;
  if (!inCall) $('resume-audio').hidden = $('enable-mic').hidden = true;
}

// -- call ----------------------------------------------------------------------------------

function wireRoom(r) {
  r.on(RoomEvent.ConnectionStateChanged, (state) => setConnection(state))
    .on(RoomEvent.Reconnecting, () => note('network drop, reconnecting…'))
    .on(RoomEvent.Reconnected, () => note('reconnected'))
    .on(RoomEvent.Disconnected, (reason) => {
      clearInterval(heartbeat);
      setInCall(false);
      setAgentState(null);
      if (reason !== DisconnectReason.CLIENT_INITIATED) note(`disconnected (${reason})`);
    })
    .on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === Track.Kind.Audio) $('audio').append(track.attach());
    })
    .on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()))
    .on(RoomEvent.AudioPlaybackStatusChanged, () => {
      // After a refresh there's no user gesture yet, so the browser may block playback.
      $('resume-audio').hidden = r.canPlaybackAudio;
    })
    .on(RoomEvent.ParticipantAttributesChanged, (changed, p) => {
      if ('lk.agent.state' in changed) setAgentState(p.attributes['lk.agent.state']);
    })
    .on(RoomEvent.ParticipantConnected, (p) => {
      if (p.isAgent) setAgentState(p.attributes['lk.agent.state'] ?? 'joining');
    })
    .on(RoomEvent.ParticipantDisconnected, (p) => {
      if (p.isAgent) setAgentState('left');
    });

  r.registerTextStreamHandler('lk.transcription', async (reader, participant) => {
    const attrs = reader.info.attributes ?? {};
    const isUser = participant.identity === r.localParticipant.identity;
    let text;
    try {
      text = (await reader.readAll()).trim();
    } catch (err) {
      // The sender aborted the stream, or we reconnected mid-stream.
      console.warn('transcription stream failed', err);
      return;
    }
    if (!text) return;
    const final = !isUser || attrs['lk.transcription_final'] === 'true';
    showLine(attrs['lk.segment_id'], isUser ? 'user' : 'agent', text, final);
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
  await enableMic();

  // On a rejoin the agent is already here, so ParticipantConnected won't fire for it.
  for (const p of room.remoteParticipants.values()) {
    if (p.isAgent) setAgentState(p.attributes['lk.agent.state']);
  }
  $('resume-audio').hidden = room.canPlaybackAudio;
  setInCall(true);
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
    $('enable-mic').hidden = true;
  } catch (err) {
    console.warn('microphone unavailable', err);
    note(`microphone unavailable (${err.name ?? err}); allow it, then click Enable mic`);
    $('enable-mic').hidden = false;
  }
}

async function start(session = null) {
  $('start').disabled = true;
  try {
    await connect(session);
  } catch (err) {
    console.error(err);
    note(`could not connect: ${err.message ?? err}`);
    setConnection('failed');
    sessionStorage.removeItem(SESSION_KEY);
    await room?.disconnect();
  } finally {
    $('start').disabled = false;
  }
}

$('start').onclick = () => start();
$('hangup').onclick = async () => {
  sessionStorage.removeItem(SESSION_KEY);
  // Tell the agent this is a hang-up, not a refresh, so it ends now instead of waiting 30 s.
  const agent = room && [...room.remoteParticipants.values()].find((p) => p.isAgent);
  if (agent) {
    await room.localParticipant
      .performRpc({ destinationIdentity: agent.identity, method: HANGUP_RPC, payload: '' })
      .catch((err) => console.warn('hang-up RPC failed; the agent will time out instead', err));
  }
  await room?.disconnect();
};
$('resume-audio').onclick = () => room?.startAudio();
$('enable-mic').onclick = () => room && enableMic();

// Stamp the time we left, so the next load knows whether the agent is still waiting.
window.addEventListener('pagehide', saveSession);

const previous = loadSession();
if (previous) start(previous);
