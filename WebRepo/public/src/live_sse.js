const el = document.getElementById('liveData');

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

try {
  const source = new EventSource('/api/stream');

  const close = () => {
    try { source.close(); } catch {}
  };
  window.addEventListener('beforeunload', close);
  window.addEventListener('pagehide', close);

  source.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      // Check if data is stale (received_at > 10 s ago)
      if (obj.received_at) {
        const ageSec = Date.now() / 1000 - obj.received_at;
        if (ageSec > 10) {
          setText('No live data (last update ' + Math.round(ageSec) + 's ago)');
          return;
        }
      }
      setText(JSON.stringify(obj, null, 2));
    } catch {
      setText(String(ev.data));
    }
  };

  source.onerror = () => {
    // EventSource will auto-reconnect; keep message minimal.
    setText('Disconnected. Retrying...');
  };
} catch (e) {
  setText(`SSE not supported: ${e}`);
}
