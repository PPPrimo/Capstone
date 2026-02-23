const el = document.getElementById('liveData');
let lastReceivedAt = 0;

function setText(text) {
  if (!el) return;
  el.textContent = text;
}

function display(obj) {
  // Deduplicate: skip if we already showed this exact timestamp
  if (obj.received_at && obj.received_at <= lastReceivedAt) return;
  lastReceivedAt = obj.received_at || 0;
  setText(JSON.stringify(obj, null, 2));
}

// ── Primary: poll /api/latest every 500ms (works through Cloudflare reliably) ──
async function poll() {
  try {
    const res = await fetch('/api/latest', { credentials: 'same-origin' });
    if (!res.ok) return;
    const obj = await res.json();
    if (obj.payload) display(obj);
  } catch { /* ignore, will retry next tick */ }
}

let pollTimer = setInterval(poll, 500);

// ── Bonus: SSE for lower-latency updates when Cloudflare cooperates ──
try {
  const source = new EventSource('/api/stream');

  window.addEventListener('beforeunload', () => { try { source.close(); } catch {} });
  window.addEventListener('pagehide',     () => { try { source.close(); } catch {} });

  source.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      if (obj.payload) display(obj);
    } catch { /* ignore malformed */ }
  };
} catch { /* SSE not supported — polling alone is fine */ }

// Initial fetch
poll();
