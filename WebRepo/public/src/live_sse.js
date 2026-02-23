const el = document.getElementById('liveData');

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

// ── WebSocket connection with auto-reconnect ──
const WS_RECONNECT_MS = 1000;
let ws = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);

  ws.onopen = () => {
    setText('Connected — waiting for data...');
  };

  ws.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      if (obj.ping) return;            // keepalive, ignore
      if (obj.payload) {
        setText(JSON.stringify(obj, null, 2));
      }
    } catch { /* ignore malformed */ }
  };

  ws.onclose = () => {
    setText('Disconnected. Reconnecting...');
    setTimeout(connect, WS_RECONNECT_MS);
  };

  ws.onerror = () => {
    // onclose will fire after this, which triggers reconnect
    ws.close();
  };
}

connect();
