// live_webrtc.js
// Connects to /api/webrtc/signal (subscriber role).
// Waits for the robot publisher to send a WebRTC offer, establishes an
// unreliable/unordered DataChannel (UDP semantics), and renders the latest
// telemetry JSON in #liveData.

const el = document.getElementById('liveData');
function setText(t) { if (el) el.textContent = t; }

// Use Google's public STUN server for ICE candidate gathering.
// Replace with a TURN server if the robot and browser are on different NATs.
const ICE_SERVERS = [{ urls: 'stun:stun.l.google.com:19302' }];

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS  = 4000;

let ws              = null;
let pc              = null;
let reconnectDelayMs = RECONNECT_BASE_MS;
let reconnectTimer   = null;
let shouldReconnect  = true;

// ── Peer connection helpers ─────────────────────────────────────────────────

function closePc() {
    if (!pc) return;
    try { pc.close(); } catch { /* ignore */ }
    pc = null;
}

async function handleOffer(msg) {
    closePc();
    pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });

    // The robot opens the DataChannel; browser receives it via ondatachannel.
    pc.ondatachannel = (ev) => {
        const ch = ev.channel;
        ch.onopen    = () => setText('WebRTC DataChannel open — receiving live data...');
        ch.onclose   = () => setText('DataChannel closed. Waiting for reconnect...');
        ch.onmessage = (e) => {
            try {
                const obj = JSON.parse(e.data);
                setText(JSON.stringify(obj, null, 2));
            } catch { /* ignore malformed frames */ }
        };
    };

    // Build and send the SDP answer with all ICE candidates embedded.
    await pc.setRemoteDescription({ type: msg.sdpType, sdp: msg.sdp });
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);

    // Wait for ICE gathering to complete before sending the answer so we
    // avoid trickle-ICE round-trips (keeps signaling simple).
    await new Promise((resolve) => {
        if (pc.iceGatheringState === 'complete') { resolve(); return; }
        pc.addEventListener('icegatheringstatechange', function handler() {
            if (pc.iceGatheringState === 'complete') {
                pc.removeEventListener('icegatheringstatechange', handler);
                resolve();
            }
        });
        setTimeout(resolve, 3000);  // safety timeout if STUN is unreachable
    });

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type:    'answer',
            sdp:     pc.localDescription.sdp,
            sdpType: pc.localDescription.type,
        }));
    }
}

// ── Signaling WebSocket ─────────────────────────────────────────────────────

function scheduleReconnect() {
    if (!shouldReconnect || reconnectTimer) return;
    const delay = reconnectDelayMs;
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, RECONNECT_MAX_MS);
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
}

function connect() {
    if (!shouldReconnect) return;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/api/webrtc/signal?role=subscriber`);

    ws.onopen = () => {
        setText('Signaling connected — waiting for robot publisher...');
        reconnectDelayMs = RECONNECT_BASE_MS;
    };

    ws.onmessage = async (ev) => {
        try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'offer') {
                await handleOffer(msg);
            }
        } catch { /* ignore malformed signaling messages */ }
    };

    ws.onclose = (ev) => {
        closePc();
        if (!shouldReconnect) return;
        if (ev.code === 4401 || ev.code === 4403) {
            setText('Unauthorized. Please refresh and log in again.');
            shouldReconnect = false;
            return;
        }
        setText('Signaling disconnected. Reconnecting...');
        scheduleReconnect();
    };

    ws.onerror = () => ws.close();
}

// ── Lifecycle ───────────────────────────────────────────────────────────────

function cleanup() {
    shouldReconnect = false;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    closePc();
    if (ws) { ws.close(); ws = null; }
}

window.addEventListener('beforeunload', cleanup);
window.addEventListener('pagehide',     cleanup);
window.addEventListener('pageshow', () => {
    shouldReconnect = true;
    if (!ws || ws.readyState === WebSocket.CLOSED) connect();
});

connect();
