const el = document.getElementById('liveData');
let hasReceivedData = false;

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

function connectSSE() {
  try {
    const source = new EventSource('/api/stream');

    const close = () => {
      try { source.close(); } catch {}
    };
    window.addEventListener('beforeunload', close);
    window.addEventListener('pagehide', close);

    source.onopen = () => {
      // Only show "waiting" if we haven't received real data yet
      if (!hasReceivedData) {
        setText('Connected — waiting for data...');
      }
    };

    source.onmessage = (ev) => {
      try {
        const obj = JSON.parse(ev.data);
        // Check if data is stale (received_at > 10 s ago)
        if (obj.received_at) {
          const ageSec = Date.now() / 1000 - obj.received_at;
          if (ageSec > 10) {
            // Stale initial snapshot — don't overwrite fresh live data
            if (!hasReceivedData) {
              setText('No live data (last update ' + Math.round(ageSec) + 's ago)');
            }
            return;
          }
        }
        hasReceivedData = true;
        setText(JSON.stringify(obj, null, 2));
      } catch {
        setText(String(ev.data));
      }
    };

    source.onerror = () => {
      // EventSource auto-reconnects; just update status text
      if (!hasReceivedData) {
        setText('Disconnected. Retrying...');
      }
    };
  } catch (e) {
    setText(`SSE not supported: ${e}`);
  }
}

connectSSE();
