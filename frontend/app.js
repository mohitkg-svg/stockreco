const { useState, useEffect, useRef, useCallback, useMemo } = React;

// r42: max age in ms before a live quote is considered stale (visual dimming
// + dropped from "live" overlay). Tuned for the ~5s WS quote cadence with
// generous slack for bg-tab throttling.
const QUOTE_STALE_MS = 30000;

// Whitelist URL schemes for hrefs supplied by API responses (news, alerts).
// r42 fix #2.25: protect against `javascript:` URLs in attacker-controlled
// API content. Returns '#' for anything not http(s) or relative.
function safeHref(url) {
  if (!url) return '#';
  try {
    const s = String(url).trim();
    if (s.startsWith('/') || s.startsWith('./') || s.startsWith('#')) return s;
    const u = new URL(s, window.location.origin);
    if (u.protocol === 'http:' || u.protocol === 'https:') return u.toString();
  } catch (_) {}
  return '#';
}

// r42 fix #2.9: Naive ISO strings (no Z suffix) coming from
// `datetime.utcnow().isoformat()` should be parsed as UTC, not local.
function parseServerDate(s) {
  if (!s) return null;
  // ISO with explicit TZ (...Z or +HH:MM) is parsed correctly already.
  if (/[zZ]|[+\-]\d{2}:?\d{2}$/.test(s)) {
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }
  // Bare ISO — append Z to force UTC interpretation.
  const d = new Date(s.endsWith('Z') ? s : s + 'Z');
  return isNaN(d.getTime()) ? null : d;
}

// ============================================================================
// r49 UX OVERHAUL — core utilities
// ============================================================================

// r49: localStorage with safe JSON + try/catch (Safari Private Mode).
const ls = {
  get(key, fallback = null) {
    try {
      const v = localStorage.getItem(key);
      if (v === null) return fallback;
      try { return JSON.parse(v); } catch (_) { return v; }
    } catch (_) { return fallback; }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value));
    } catch (_) {}
  },
  remove(key) {
    try { localStorage.removeItem(key); } catch (_) {}
  },
};

// r49: persistent state hook backed by localStorage. Survives reload.
//
// r50 fix: treat `null` as "no value", same as `undefined`. A previous build
// (or a manual ls.set(key, null) anywhere) could persist literal null which
// then defeats the default fallback — code reading `state.field` then throws
// "Cannot read properties of null". Also: when the persisted shape doesn't
// match the default's shape (e.g. defaults is an object but stored is a
// scalar), prefer the default. This is the cheapest crash-prevention layer
// for cross-version localStorage compatibility.
function usePersistentState(key, initial) {
  const [v, setV] = useState(() => {
    const stored = ls.get(key, undefined);
    const initVal = (typeof initial === 'function' ? initial() : initial);
    if (stored === undefined || stored === null) return initVal;
    // If default is a plain object but stored is a primitive (or vice versa),
    // discard the stored value — schema drifted between releases.
    const isObj = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
    if (isObj(initVal) && !isObj(stored)) return initVal;
    if (Array.isArray(initVal) && !Array.isArray(stored)) return initVal;
    return stored;
  });
  useEffect(() => { ls.set(key, v); }, [key, v]);
  return [v, setV];
}

// r49: density mode. 'compact' = tight grids, smaller cards, scannable.
function useDensity() {
  const [density, setDensity] = usePersistentState('uiDensity', 'regular');
  useEffect(() => {
    document.documentElement.setAttribute('data-density', density);
  }, [density]);
  return [density, setDensity];
}

// r49: friendly-error mapper. Convert raw fetch / SQL / broker errors
// into ops-readable strings. Falls back to passthrough.
function friendlyError(e) {
  if (!e) return '';
  const raw = String(e?.detail || e?.message || e);
  const lower = raw.toLowerCase();
  if (/insufficient/.test(lower) && /buying power/.test(lower)) return 'Buying power exhausted. New entries paused.';
  if (/pattern day trader/.test(lower) || /pdt/.test(lower)) return 'PDT violation — bot locked out 24h.';
  if (/wash trade/.test(lower)) return 'Wash-trade rejection (same-day reverse).';
  if (/sub.?penny/.test(lower)) return 'Sub-penny rejection — limit price below tick size.';
  if (/not tradable|halt/.test(lower)) return 'Asset not tradable (halted or delisted).';
  if (/connection|timeout|refused|unreachable/.test(lower)) return 'Network error. Retry.';
  if (/forbidden|401|unauthorized/.test(lower)) return 'Auth expired — log in again.';
  if (/database is locked/.test(lower)) return 'Database busy — retrying automatically.';
  if (lower.length > 220) return raw.slice(0, 220) + '…';
  return raw;
}

// ============================================================================
// r49: undoable toast system (replaces native confirm/alert for destructive ops)
// ============================================================================
const _undoActions = new Map(); // id → { action, label, expires }
const _toastListeners = new Set();
let _toastSeq = 1;

function toast(opts) {
  const id = _toastSeq++;
  const t = {
    id,
    msg: opts.msg || '',
    kind: opts.kind || 'info',  // 'info' | 'success' | 'warn' | 'error'
    duration: opts.duration ?? 4000,
    actionLabel: opts.actionLabel || null,
    onAction: opts.onAction || null,
    createdAt: Date.now(),
  };
  _toastListeners.forEach(fn => fn({ type: 'add', toast: t }));
  if (t.duration > 0) {
    setTimeout(() => _toastListeners.forEach(fn => fn({ type: 'remove', id })), t.duration);
  }
  return id;
}

// r49: undoable destructive action. Stages the operation; if not undone in
// `delayMs`, executes. Returns a cancel function.
function stageAction({ label, delayMs = 4000, onConfirm, onUndo, kind = 'warn' }) {
  let cancelled = false;
  const id = _toastSeq++;
  const undo = () => {
    if (cancelled) return;
    cancelled = true;
    _toastListeners.forEach(fn => fn({ type: 'remove', id }));
    if (onUndo) try { onUndo(); } catch (_) {}
  };
  _toastListeners.forEach(fn => fn({
    type: 'add',
    toast: {
      id,
      msg: label,
      kind,
      duration: delayMs,
      actionLabel: 'Undo',
      onAction: undo,
      createdAt: Date.now(),
      progress: true,
      progressMs: delayMs,
    },
  }));
  setTimeout(() => {
    if (cancelled) return;
    cancelled = true;
    _toastListeners.forEach(fn => fn({ type: 'remove', id }));
    try { onConfirm(); } catch (e) { toast({ msg: 'Action failed: ' + friendlyError(e), kind: 'error' }); }
  }, delayMs);
  return undo;
}

function ToastHost() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    const onEvent = (ev) => {
      if (ev.type === 'add') setItems(arr => [...arr, ev.toast]);
      else if (ev.type === 'remove') setItems(arr => arr.filter(t => t.id !== ev.id));
    };
    _toastListeners.add(onEvent);
    return () => _toastListeners.delete(onEvent);
  }, []);
  if (items.length === 0) return null;
  const colorMap = {
    success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
    warn: 'border-amber-500/50 bg-amber-500/15 text-amber-100',
    error: 'border-red-500/50 bg-red-500/15 text-red-100',
    info: 'border-blue-500/40 bg-blue-500/10 text-blue-200',
  };
  return (
    <div className="fixed bottom-4 right-4 z-[60] space-y-2 max-w-sm">
      {items.map(t => (
        <div
          key={t.id}
          role={t.kind === 'error' || t.kind === 'warn' ? 'alert' : 'status'}
          className={`relative rounded-lg border px-3 py-2.5 shadow-xl text-xs ${colorMap[t.kind] || colorMap.info}`}
        >
          <div className="flex items-start gap-3">
            <div className="flex-1 leading-relaxed">{t.msg}</div>
            {t.actionLabel && (
              <button
                onClick={() => { try { t.onAction?.(); } catch (_) {} }}
                className="font-bold underline whitespace-nowrap focus:outline-none focus:ring-2 focus:ring-white/40 rounded px-1"
              >
                {t.actionLabel}
              </button>
            )}
          </div>
          {t.progress && (
            <div className="absolute left-0 right-0 bottom-0 h-0.5 bg-white/30 rounded-b-lg overflow-hidden">
              <div
                className="h-full bg-white"
                style={{
                  animation: `r49ToastProgress ${t.progressMs}ms linear forwards`,
                }}
              />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// r49: SWR-style shared query layer with single-flight + visibility gating
// ============================================================================
const _swrCache = new Map(); // key → { data, ts, promise }
const _swrSubs = new Map();  // key → Set<setter>

function _swrNotify(key) {
  const subs = _swrSubs.get(key);
  if (!subs) return;
  const v = _swrCache.get(key);
  subs.forEach(s => s(v));
}

async function _swrFetch(key, fetcher) {
  const existing = _swrCache.get(key);
  if (existing?.promise) return existing.promise;  // single-flight
  const p = (async () => {
    try {
      const data = await fetcher();
      _swrCache.set(key, { data, ts: Date.now(), promise: null });
      _swrNotify(key);
      return data;
    } catch (e) {
      _swrCache.set(key, { data: existing?.data, ts: existing?.ts, promise: null, error: e });
      _swrNotify(key);
      throw e;
    }
  })();
  _swrCache.set(key, { ...(existing || {}), promise: p });
  return p;
}

function useSWR(key, fetcher, { intervalMs = 30000, enabled = true } = {}) {
  const [state, setState] = useState(() => _swrCache.get(key) || { data: undefined, ts: 0 });
  const fetcherRef = useRef(fetcher);
  useEffect(() => { fetcherRef.current = fetcher; }, [fetcher]);
  useEffect(() => {
    if (!enabled || !key) return;
    if (!_swrSubs.has(key)) _swrSubs.set(key, new Set());
    _swrSubs.get(key).add(setState);
    // Initial fetch if cache stale.
    const cached = _swrCache.get(key);
    if (!cached || (Date.now() - (cached.ts || 0)) > Math.max(1000, intervalMs / 2)) {
      _swrFetch(key, () => fetcherRef.current()).catch(() => {});
    }
    const iv = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      _swrFetch(key, () => fetcherRef.current()).catch(() => {});
    }, intervalMs);
    const onVis = () => {
      if (document.visibilityState === 'visible') {
        _swrFetch(key, () => fetcherRef.current()).catch(() => {});
      }
    };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      clearInterval(iv);
      document.removeEventListener('visibilitychange', onVis);
      _swrSubs.get(key)?.delete(setState);
    };
  }, [key, intervalMs, enabled]);
  return {
    data: state?.data,
    error: state?.error,
    loading: !state?.data && !state?.error,
    refresh: () => _swrFetch(key, () => fetcherRef.current()),
  };
}

function swrInvalidate(prefix) {
  const keys = Array.from(_swrCache.keys()).filter(k => !prefix || k.startsWith(prefix));
  keys.forEach(k => {
    _swrCache.delete(k);
    _swrNotify(k);
  });
}

// ============================================================================
// r49: keyboard shortcuts (palette, navigation, quick actions)
// ============================================================================
const _kbListeners = new Set();
function _kbHandler(e) {
  // Don't intercept while typing in inputs (unless ⌘/Ctrl+K explicitly).
  const tag = (e.target?.tagName || '').toUpperCase();
  const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || e.target?.isContentEditable;
  const cmd = e.metaKey || e.ctrlKey;
  const k = (e.key || '').toLowerCase();
  for (const fn of _kbListeners) {
    try { fn({ key: k, cmd, shift: e.shiftKey, alt: e.altKey, isInput, raw: e }); } catch (_) {}
  }
}
if (typeof window !== 'undefined' && !window.__r49KbInstalled) {
  window.addEventListener('keydown', _kbHandler);
  window.__r49KbInstalled = true;
}

function useKeyboard(callback, deps = []) {
  useEffect(() => {
    _kbListeners.add(callback);
    return () => _kbListeners.delete(callback);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

// ============================================================================
// r49: notification persistence (alert inbox)
// ============================================================================
function pushNotification(n) {
  try {
    const arr = ls.get('notifInbox', []) || [];
    arr.unshift({ ...n, id: _toastSeq++, ts: Date.now() });
    while (arr.length > 100) arr.pop();
    ls.set('notifInbox', arr);
    window.dispatchEvent(new CustomEvent('app:notif'));
  } catch (_) {}
}
function useNotifications() {
  const [arr, setArr] = useState(() => ls.get('notifInbox', []) || []);
  useEffect(() => {
    const onChange = () => setArr(ls.get('notifInbox', []) || []);
    window.addEventListener('app:notif', onChange);
    return () => window.removeEventListener('app:notif', onChange);
  }, []);
  const unread = arr.filter(n => !n.read).length;
  const markRead = useCallback(() => {
    const now = ls.get('notifInbox', []) || [];
    ls.set('notifInbox', now.map(n => ({ ...n, read: true })));
    window.dispatchEvent(new CustomEvent('app:notif'));
  }, []);
  const clear = useCallback(() => {
    ls.set('notifInbox', []);
    window.dispatchEvent(new CustomEvent('app:notif'));
  }, []);
  return { items: arr, unread, markRead, clear };
}

// ============================================================================
// r49: directional / colour-blind icons (▲/▼ paired with colour for redundancy)
// ============================================================================
function DirIcon({ dir, className = '' }) {
  if (dir === 'up' || dir === 'BUY') return <span className={`inline-block ${className}`} aria-hidden="true">▲</span>;
  if (dir === 'down' || dir === 'SELL') return <span className={`inline-block ${className}`} aria-hidden="true">▼</span>;
  return <span className={`inline-block ${className}`} aria-hidden="true">●</span>;
}

// ============================================================================
// r49: GLOBAL ERROR BOUNDARY
// ============================================================================
class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) {
    try {
      console.error('UI error:', error, info);
      // Forward to backend frontend-error endpoint (added in r48).
      fetch(`${API_BASE}/api/log/frontend-error`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          msg: String(error?.message || error),
          stack: String(error?.stack || ''),
          url: window.location.href,
          componentStack: info?.componentStack || '',
        }),
      }).catch(() => {});
    } catch (_) {}
  }
  render() {
    if (this.state.error) {
      return (
        <div role="alert" className="m-4 p-4 rounded-xl border border-red-500/50 bg-red-500/10 text-red-100">
          <div className="font-bold mb-1">Something broke in the UI.</div>
          <div className="text-xs opacity-80 mb-2">Error: {String(this.state.error.message || this.state.error)}</div>
          <div className="flex gap-2">
            <button
              onClick={() => this.setState({ error: null })}
              className="px-3 py-1 rounded-md bg-white/10 hover:bg-white/20 text-xs font-semibold"
            >Reset panel</button>
            <button
              onClick={() => window.location.reload()}
              className="px-3 py-1 rounded-md bg-white/10 hover:bg-white/20 text-xs font-semibold"
            >Reload app</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// ============================================================================
// r49: small visual helpers
// ============================================================================
function Sparkline({ values = [], width = 120, height = 32, stroke = '#10b981', fill = 'rgba(16,185,129,0.18)' }) {
  if (!values || values.length < 2) return <svg width={width} height={height} />;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - min) / span) * height).toFixed(1)}`);
  const d = 'M' + pts.join(' L');
  const fillD = `${d} L${width},${height} L0,${height} Z`;
  return (
    <svg width={width} height={height} aria-hidden="true">
      <path d={fillD} fill={fill} />
      <path d={d} fill="none" stroke={stroke} strokeWidth="1.5" />
    </svg>
  );
}

function ProgressBar({ pct, danger = false, warn = false }) {
  const w = Math.max(0, Math.min(100, pct || 0));
  const barColor = danger
    ? 'bg-gradient-to-r from-red-500 to-rose-500'
    : warn
    ? 'bg-gradient-to-r from-amber-500 to-orange-500'
    : 'bg-gradient-to-r from-blue-500 to-indigo-500';
  return (
    <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--surface-border)' }}>
      <div className={`h-full transition-all ${barColor}`} style={{ width: `${w}%` }} />
    </div>
  );
}

// r49: R-multiple progress bar — entry left, stop also left, T1/T2/T3 to right.
// Shows where current price is on the entry → T3 axis with stop sentinel.
function RProgressBar({ entry, stop, target1, target2, target3, current, side = 'BUY' }) {
  if (!entry || !stop || !current) return null;
  const isLong = side === 'BUY';
  // Range from stop → max(targets, current)
  const lo = isLong ? stop : Math.max(target1 || 0, target2 || 0, target3 || 0, current);
  const hi = isLong ? Math.max(target1 || 0, target2 || 0, target3 || 0, current) : stop;
  if (lo >= hi) return null;
  const span = hi - lo;
  const pos = (v) => `${Math.max(0, Math.min(100, ((v - lo) / span) * 100))}%`;
  const r = (current - entry) / Math.max(1e-9, Math.abs(entry - stop)) * (isLong ? 1 : -1);
  const winning = r >= 0;
  return (
    <div className="relative h-3 rounded-full overflow-hidden border app-border-soft" style={{ background: 'rgba(148,163,184,0.08)' }}>
      {/* entry → current shaded */}
      <div
        className={`absolute top-0 bottom-0 ${winning ? 'bg-emerald-500/30' : 'bg-red-500/30'}`}
        style={{
          left: isLong ? pos(entry) : pos(current),
          right: isLong ? `calc(100% - ${pos(current)})` : `calc(100% - ${pos(entry)})`,
        }}
      />
      {/* Stop tick */}
      <div className="absolute top-0 bottom-0 w-0.5 bg-red-500" style={{ left: pos(stop) }} title={`Stop ${stop}`} />
      {/* Entry tick */}
      <div className="absolute top-0 bottom-0 w-0.5 bg-blue-400" style={{ left: pos(entry) }} title={`Entry ${entry}`} />
      {[target1, target2, target3].filter(Boolean).map((t, i) => (
        <div key={i} className="absolute top-0 bottom-0 w-0.5 bg-emerald-400/70" style={{ left: pos(t) }} title={`T${i + 1} ${t}`} />
      ))}
      {/* Current marker */}
      <div className="absolute top-1/2 -translate-y-1/2 w-2.5 h-2.5 rounded-full bg-white border-2 border-blue-500 shadow-md"
           style={{ left: `calc(${pos(current)} - 5px)` }} title={`Now ${current}`} />
    </div>
  );
}

// ---------- Theme ----------
// Persist in localStorage; the index.html pre-paint script applies it before
// the React tree mounts so there is never a flash of the wrong theme.
function useTheme() {
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem('theme') || 'dark'; } catch (_) { return 'dark'; }
  });
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('theme', theme); } catch (_) {}
  }, [theme]);
  const toggle = useCallback(() => setTheme(t => t === 'dark' ? 'light' : 'dark'), []);
  return [theme, toggle];
}

function ThemeToggle({ theme, onToggle }) {
  return (
    <button
      onClick={onToggle}
      aria-label="Toggle theme"
      title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
      className="theme-toggle"
    />
  );
}

// Theme-aware chart options — passed to lightweight-charts createChart.
function chartThemeOptions() {
  const css = getComputedStyle(document.documentElement);
  const bg = css.getPropertyValue('--chart-bg').trim() || '#0f1419';
  const grid = css.getPropertyValue('--chart-grid').trim() || '#1f2937';
  const border = css.getPropertyValue('--chart-border').trim() || '#374151';
  const text = css.getPropertyValue('--chart-text').trim() || '#d1d5db';
  return {
    layout: { background: { color: bg }, textColor: text },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: border },
    rightPriceScale: { borderColor: border },
  };
}

// API base resolution order:
//  1) window.__API_BASE__ injected by index.html / server template
//  2) <meta name="api-base" content="..."> tag
//  3) Same-origin (assumes prod deploys backend behind same hostname/port)
//  4) Dev fallback: replace any port with :8000
const API_BASE = (() => {
  if (typeof window !== 'undefined' && window.__API_BASE__) return window.__API_BASE__;
  const meta = typeof document !== 'undefined' && document.querySelector('meta[name="api-base"]');
  if (meta && meta.content) return meta.content;
  const origin = window.location.origin;
  // If we're already on a non-default port, just use same-origin (likely a proxied prod deploy).
  if (window.location.port && window.location.port !== '5173' && window.location.port !== '3000') {
    return origin;
  }
  // Dev: assume backend is on :8000
  return origin.replace(/:\d+$/, ':8000');
})();
const TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d', '1mo'];

// How many most-recent bars to show by default per timeframe. Two competing
// forces: (a) enough bars for context, (b) small enough span that lightweight-
// charts picks HH:MM axis labels instead of dates. The library shows dates
// whenever the visible range crosses midnight, so intraday TFs are sized to
// fit inside a single trading session (6.5h = 78 bars @5m, 26 @15m, etc).
const DEFAULT_VISIBLE_BARS = {
  '5m': 78,     // ~1 trading session → HH:MM labels
  '15m': 52,    // ~2 sessions
  '30m': 52,    // ~4 sessions
  '1h': 60,     // ~1.5 weeks
  '4h': 80,     // ~1 month
  '1d': 200,    // ~10 months
  '1mo': 120,   // 10 years
};

// ---------- API key (shared secret) ----------
// The backend gates every /api/* endpoint with `X-API-Key`. The browser
// can't set headers on WebSockets, so we also append `?token=` to the WS
// URL. r42 fix #2.26: prefer sessionStorage so the secret dies on tab
// close; fall back to localStorage only for users who explicitly opted
// into "remember me" via the login screen.
const API_KEY_STORAGE = 'app_api_key';
const API_KEY_REMEMBER = 'app_api_key_remember';
function getApiKey() {
  try {
    return sessionStorage.getItem(API_KEY_STORAGE)
        || localStorage.getItem(API_KEY_STORAGE)
        || '';
  } catch (_) { return ''; }
}
function setApiKey(k, remember) {
  try {
    if (k) {
      sessionStorage.setItem(API_KEY_STORAGE, k);
      if (remember) {
        localStorage.setItem(API_KEY_STORAGE, k);
        localStorage.setItem(API_KEY_REMEMBER, '1');
      } else {
        localStorage.removeItem(API_KEY_STORAGE);
        localStorage.removeItem(API_KEY_REMEMBER);
      }
    } else {
      sessionStorage.removeItem(API_KEY_STORAGE);
      localStorage.removeItem(API_KEY_STORAGE);
      localStorage.removeItem(API_KEY_REMEMBER);
    }
  } catch (_) {}
}
function authHeaders() {
  const k = getApiKey();
  return k ? { 'X-API-Key': k } : {};
}

function on401() {
  setApiKey('');
  window.dispatchEvent(new CustomEvent('app:unauthorized'));
}

// r42 fix #1.23: surface meaningful error detail to callers instead of
// reducing every failure to a bare statusText. Callers that want to show
// a toast/banner can read `err.detail` and `err.status`.
async function readError(r) {
  let detail = '';
  try {
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      const j = await r.json();
      detail = j.detail || j.error || JSON.stringify(j);
    } else {
      detail = (await r.text()).slice(0, 400);
    }
  } catch (_) {}
  const err = new Error(detail || r.statusText || `HTTP ${r.status}`);
  err.status = r.status;
  err.detail = detail;
  return err;
}

const api = {
  get: (path) => fetch(`${API_BASE}${path}`, { headers: authHeaders() })
    .then(async r => {
      if (r.status === 401) { on401(); throw await readError(r); }
      if (!r.ok) throw await readError(r);
      return r.json();
    }),
  post: (path, body) => fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  }).then(async r => {
    if (r.status === 401) { on401(); throw await readError(r); }
    if (!r.ok) throw await readError(r);
    return r.json();
  }),
  patch: (path, body) => fetch(`${API_BASE}${path}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  }).then(async r => {
    if (r.status === 401) { on401(); throw await readError(r); }
    if (!r.ok) throw await readError(r);
    return r.json();
  }),
  delete: (path) => fetch(`${API_BASE}${path}`, { method: 'DELETE', headers: authHeaders() })
    .then(async r => {
      if (r.status === 401) { on401(); throw await readError(r); }
      if (!r.ok) throw await readError(r);
      return r.json();
    }),
};

// ---------- Live Quotes WebSocket ----------
// Connects to /ws/quotes and maintains {SYMBOL: {bid, ask, last, ts}} in state.
// `onSignalUpdate(ticker)` fires when the server live-recomputes signals so
// the UI can refetch.
//
// r42 changes:
//  • Exponential backoff + jitter (#2.8) — was a constant 3s, hammered a
//    restarting server.
//  • visibilitychange-driven re-check (#2.7) — when the user returns to a
//    backgrounded tab the connection is force-recycled if dead, and a
//    `app:resync` event fires so panels can refetch state lost during the
//    disconnect window (#1.11 reconnect-event-loss).
//  • Broader event fan-out (#1.12) — trade_opened, alert, news, option_quote
//    are forwarded as window events; previously only target_hit and
//    trade_closed were broadcast.
//  • Quotes carry `_localTs` (Date.now() at receipt) so the UI can render a
//    staleness pill independent of server clock skew.
function useLiveQuotes(onSignalUpdate) {
  const [quotes, setQuotes] = useState({});
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    let closed = false;
    let reconnectTimer = null;
    let backoff = 1000;  // start at 1s, cap at 30s

    const connect = () => {
      const key = getApiKey();
      const q = key ? `?token=${encodeURIComponent(key)}` : '';
      const wsUrl = API_BASE.replace(/^http/, 'ws') + '/ws/quotes' + q;
      let ws;
      try { ws = new WebSocket(wsUrl); }
      catch (_) {
        if (!closed) reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(30000, backoff * 2) + Math.floor(Math.random() * 500);
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        backoff = 1000;
        // Tell every panel to refetch — events delivered while the socket
        // was down are gone; the only safe recovery is a state refresh.
        try { window.dispatchEvent(new CustomEvent('app:resync')); } catch (_) {}
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(30000, backoff * 2) + Math.floor(Math.random() * 500);
      };
      ws.onerror = () => { try { ws.close(); } catch (e) {} };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); }
        catch (e) { console.warn('ws: invalid json', e); return; }
        if (!msg || typeof msg.type !== 'string') {
          console.warn('ws: missing type', msg);
          return;
        }
        const localTs = Date.now();
        if (msg.type === 'snapshot') {
          const stocks = msg.stocks && typeof msg.stocks === 'object' ? msg.stocks : {};
          // Stamp _localTs on each ingested entry so we can flag stale
          // overlay quotes after disconnect / tab-throttle.
          const stamped = {};
          for (const sym of Object.keys(stocks)) {
            stamped[sym] = { ...stocks[sym], _localTs: localTs };
          }
          setQuotes(stamped);
        } else if (msg.type === 'stock_trade' || msg.type === 'stock_quote') {
          if (typeof msg.symbol !== 'string' || !msg.symbol) {
            console.warn('ws: missing symbol on', msg.type);
            return;
          }
          setQuotes(prev => ({
            ...prev,
            [msg.symbol]: { ...(prev[msg.symbol] || {}), ...msg, _localTs: localTs },
          }));
        } else if (msg.type === 'signals_updated' && onSignalUpdate && typeof msg.symbol === 'string') {
          onSignalUpdate(msg.symbol);
        } else if (msg.type === 'target_hit') {
          try { window.dispatchEvent(new CustomEvent('app:target_hit', { detail: msg })); } catch (_) {}
        } else if (msg.type === 'trade_closed') {
          try { window.dispatchEvent(new CustomEvent('app:trade_closed', { detail: msg })); } catch (_) {}
        } else if (msg.type === 'trade_opened') {
          try { window.dispatchEvent(new CustomEvent('app:trade_opened', { detail: msg })); } catch (_) {}
        } else if (msg.type === 'alert') {
          try { window.dispatchEvent(new CustomEvent('app:alert', { detail: msg })); } catch (_) {}
        } else if (msg.type === 'news') {
          try { window.dispatchEvent(new CustomEvent('app:news', { detail: msg })); } catch (_) {}
        } else if (msg.type === 'option_quote') {
          try { window.dispatchEvent(new CustomEvent('app:option_quote', { detail: msg })); } catch (_) {}
        }
        // Unknown types are silently ignored to keep forward-compat — but
        // we don't drop quote/event semantics behind the user's back.
      };
    };

    connect();

    // r42 fix #2.7: when the tab returns to focus, if our socket is
    // dead, force-reconnect immediately rather than waiting out the
    // backoff timer.
    const onVis = () => {
      if (document.visibilityState !== 'visible') return;
      const ws = wsRef.current;
      if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        backoff = 1000;
        connect();
      } else {
        // Even if the socket says it's open, force a state refresh on
        // returning from background — bg-throttled tabs miss events.
        try { window.dispatchEvent(new CustomEvent('app:resync')); } catch (_) {}
      }
    };
    document.addEventListener('visibilitychange', onVis);

    return () => {
      closed = true;
      document.removeEventListener('visibilitychange', onVis);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) { try { wsRef.current.close(); } catch (e) {} }
    };
  }, [onSignalUpdate]);

  return { quotes, connected };
}

// ---------- Signal badge ----------
function SignalBadge({ type, confidence, isNew }) {
  const colors = {
    BUY: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30 shadow-sm shadow-emerald-500/10',
    SELL: 'bg-red-500/15 text-red-300 border-red-500/30 shadow-sm shadow-red-500/10',
    NEUTRAL: 'bg-slate-500/15 text-slate-300 border-slate-500/25',
  };
  const klass = colors[type] || colors.NEUTRAL;
  // r42 fix #2.11: confidence tooltip — explains it's a composite weighted
  // score (0-95), NOT win-probability. Operators were over-trusting "85%".
  const confTitle = (
    "Composite signal score, 0-95.\n"
    + "It's a weighted blend of trend alignment, momentum, S/R position,\n"
    + "regime, volume, fundamentals, and analyst consensus — NOT a literal\n"
    + "win-probability. The auto-trader takes signals ≥ confidence_threshold\n"
    + "and scales risk by a [0.7, 1.4] multiplier within the band."
  );
  return (
    <div
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-bold tracking-wide border ${klass}`}
      title={confidence ? confTitle : undefined}
    >
      {isNew && <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />}
      {type}
      {confidence ? <span className="opacity-70">{Math.round(confidence)}<span aria-hidden>%</span></span> : null}
    </div>
  );
}

// ============================================================================
// r49: NEW WIDGETS — equity curve, command bar, freshness, daily-loss, sectors, alert inbox, quick-trade palette
// ============================================================================

// r49: data-feed freshness strip — shows live status of each upstream feed
function FreshnessStrip({ liveConnected }) {
  const { data: health } = useSWR('/api/health', () => api.get('/api/health'), { intervalMs: 15000 });
  const items = useMemo(() => {
    const arr = [];
    arr.push({
      label: 'Quotes',
      ok: !!liveConnected,
      hint: liveConnected ? 'WS connected' : 'WS disconnected',
    });
    if (health) {
      arr.push({ label: 'Broker', ok: !health.broker_down, hint: health.broker_down ? 'Down' : 'OK' });
      arr.push({ label: 'BP', ok: !health.bp_breaker_active, hint: health.bp_breaker_active ? 'Exhausted' : 'OK' });
      arr.push({ label: 'PDT', ok: !health.pdt_locked, hint: health.pdt_locked ? 'Locked 24h' : 'OK' });
      arr.push({ label: 'DB', ok: !health.db_down, hint: health.db_down ? 'Down' : 'OK' });
      if (health.crisis_mode) arr.push({ label: 'Crisis', ok: false, hint: 'Crisis mode active' });
    }
    return arr;
  }, [health, liveConnected]);
  return (
    <div className="flex items-center gap-2 flex-wrap text-[10px]" role="status" aria-label="Data feed health">
      {items.map((it, i) => (
        <span
          key={i}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border font-semibold uppercase tracking-wider ${
            it.ok
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
              : 'border-red-500/40 bg-red-500/10 text-red-400'
          }`}
          title={it.hint}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${it.ok ? 'bg-emerald-400' : 'bg-red-400'}`} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

// r49: equity curve panel — area chart of equity over N days + drawdown shaded
function EquityCurvePanel({ lookbackDays = 30 }) {
  const { data, error, refresh } = useSWR(
    `/api/trading/equity-curve?lookback_days=${lookbackDays}`,
    () => api.get(`/api/trading/equity-curve?lookback_days=${lookbackDays}`),
    { intervalMs: 60000 }
  );
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef({});
  useEffect(() => {
    if (!containerRef.current || !window.LightweightCharts) return;
    const chart = LightweightCharts.createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 200,
      ...chartThemeOptions(),
    });
    chartRef.current = chart;
    seriesRef.current.equity = chart.addAreaSeries({
      lineColor: '#3b82f6', topColor: 'rgba(59,130,246,0.4)', bottomColor: 'rgba(59,130,246,0.02)',
      lineWidth: 2, priceLineVisible: false,
    });
    seriesRef.current.spy = chart.addLineSeries({
      color: 'rgba(148,163,184,0.5)', lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    });
    const ro = new ResizeObserver(() => {
      if (chartRef.current && containerRef.current)
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
    });
    ro.observe(containerRef.current);
    return () => {
      ro.disconnect();
      try { chart.remove(); } catch (_) {}
      chartRef.current = null;
      seriesRef.current = {};
    };
  }, []);
  useEffect(() => {
    if (!data || !chartRef.current || !seriesRef.current.equity) return;
    const points = (data.snapshots || []).map(s => {
      const d = parseServerDate(s.ts);
      if (!d) return null;
      return { time: Math.floor(d.getTime() / 1000), value: s.equity };
    }).filter(Boolean);
    if (points.length) {
      seriesRef.current.equity.setData(points);
      // SPY-relative overlay: scale to a $100 baseline at start
      if (data.spy_curve) {
        const spy0 = data.spy_curve[0]?.spy_close;
        const eq0 = points[0].value;
        if (spy0 && eq0) {
          const spyPts = data.spy_curve.map(p => {
            const d = parseServerDate(p.ts);
            if (!d) return null;
            return { time: Math.floor(d.getTime() / 1000), value: eq0 * (p.spy_close / spy0) };
          }).filter(Boolean);
          seriesRef.current.spy.setData(spyPts);
        }
      }
      chartRef.current.timeScale().fitContent();
    }
  }, [data]);
  const stats = useMemo(() => {
    if (!data?.snapshots?.length) return null;
    const eq = data.snapshots.map(s => s.equity);
    const start = eq[0], cur = eq[eq.length - 1];
    const peak = Math.max(...eq);
    const dd = ((cur - peak) / peak) * 100;
    const ret = ((cur - start) / start) * 100;
    return { start, cur, peak, dd, ret };
  }, [data]);
  return (
    <div className="surface rounded-2xl p-4 shadow-xl">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-bold uppercase tracking-wider app-text-secondary">Equity Curve</h3>
          <span className="text-[10px] app-text-muted">Last {lookbackDays}d · vs SPY</span>
        </div>
        {stats && (
          <div className="flex items-center gap-3 text-xs font-mono">
            <span className={stats.ret >= 0 ? 'text-emerald-400' : 'text-red-400'}>
              <DirIcon dir={stats.ret >= 0 ? 'up' : 'down'} /> {stats.ret >= 0 ? '+' : ''}{stats.ret.toFixed(2)}%
            </span>
            <span className="app-text-muted">DD <span className="text-red-400">{stats.dd.toFixed(2)}%</span></span>
            <button onClick={refresh} className="app-text-muted hover:app-text-primary" aria-label="Refresh">↻</button>
          </div>
        )}
      </div>
      {error && <div role="alert" className="text-xs text-red-300 mb-2">{friendlyError(error)}</div>}
      <div ref={containerRef} className="w-full" style={{ height: 200 }} />
      {!data && !error && <div className="skel h-8 w-full mt-2" />}
      {data && !data?.snapshots?.length && (
        <div className="text-[11px] app-text-muted italic mt-2 text-center">
          No equity snapshots yet. Recorder fires every 5 min during RTH; data will appear after the first market session.
        </div>
      )}
    </div>
  );
}

// r49: daily loss + risk-budget progress
function DailyLossProgress() {
  const { data: status } = useSWR('/api/trading/auto/status', () => api.get('/api/trading/auto/status'), { intervalMs: 30000 });
  const { data: health } = useSWR('/api/health', () => api.get('/api/health'), { intervalMs: 30000 });
  if (!status) return null;
  const cfg = status.config || {};
  const equity = status.equity || 0;
  const dailyLossLimitPct = cfg.daily_loss_limit_pct || 0.03;
  const dailyLossLimitDollar = equity * dailyLossLimitPct;
  // Pull today's P/L from auto-trader trades — naively use status fields if present
  const todayLoss = Math.max(0, -(status.realized_today || 0));
  const usedPct = dailyLossLimitDollar > 0 ? (todayLoss / dailyLossLimitDollar) * 100 : 0;
  const ddRaw = health?.session_dd_pct;
  const ddAvail = ddRaw != null && Number.isFinite(ddRaw);
  const sessionDd = ddAvail ? ddRaw * 100 : 0;
  return (
    <div className="surface rounded-2xl p-4 shadow-xl" data-r49-card>
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="app-text-secondary uppercase tracking-wider text-[10px] font-semibold">Daily Loss Used</span>
            <span className="font-mono app-text-primary">${todayLoss.toFixed(0)} / ${dailyLossLimitDollar.toFixed(0)}</span>
          </div>
          <ProgressBar pct={usedPct} danger={usedPct >= 80} warn={usedPct >= 50} />
          <div className="text-[10px] app-text-muted mt-1">{usedPct.toFixed(0)}% of {(dailyLossLimitPct * 100).toFixed(1)}% cap</div>
        </div>
        <div>
          <div className="flex items-center justify-between mb-1">
            <span className="app-text-secondary uppercase tracking-wider text-[10px] font-semibold">Session DD</span>
            <span className="font-mono">{ddAvail ? `${sessionDd.toFixed(2)}%` : '—'}</span>
          </div>
          <ProgressBar pct={ddAvail ? Math.min(100, (sessionDd / 5) * 100) : 0} danger={ddAvail && sessionDd >= 4} warn={ddAvail && sessionDd >= 2.5} />
          <div className="text-[10px] app-text-muted mt-1">
            {health?.crisis_mode ? <span className="text-red-400 font-semibold">⚠ CRISIS MODE</span> : (ddAvail ? 'Healthy' : 'Awaiting equity baseline')}
          </div>
        </div>
      </div>
    </div>
  );
}

// r49: sector / correlation exposure widget
function SectorExposureWidget({ positions = [] }) {
  if (!positions || positions.length < 2) return null;
  // Approximation — real backend has sector lookup via fundamentals; here we
  // group by symbol prefix as a fallback if sector field missing.
  const totals = {};
  let total = 0;
  positions.forEach(p => {
    const notional = Math.abs((p.qty || 0) * (p.current_price || p.avg_entry_price || 0));
    const sec = p.sector || p.industry || 'Unknown';
    totals[sec] = (totals[sec] || 0) + notional;
    total += notional;
  });
  const buckets = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const colors = ['#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#64748b'];
  return (
    <div className="surface rounded-2xl p-4 shadow-xl" data-r49-card>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-bold uppercase tracking-wider app-text-secondary">Sector Exposure</h3>
        <span className="text-[10px] app-text-muted">{positions.length} positions · ${total.toFixed(0)}</span>
      </div>
      <div className="h-2 rounded-full overflow-hidden flex">
        {buckets.map(([sec, val], i) => (
          <div
            key={sec}
            style={{ width: `${(val / total) * 100}%`, background: colors[i % colors.length] }}
            title={`${sec}: $${val.toFixed(0)} (${((val / total) * 100).toFixed(1)}%)`}
          />
        ))}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-3 gap-y-1 mt-3 text-[10px]">
        {buckets.map(([sec, val], i) => (
          <div key={sec} className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full" style={{ background: colors[i % colors.length] }} />
            <span className="app-text-secondary truncate">{sec}</span>
            <span className="ml-auto font-mono app-text-primary">{((val / total) * 100).toFixed(0)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// r49: command bar — sticky single-row exposure summary at top of trading view
function CommandBar() {
  const { data: status } = useSWR('/api/trading/auto/status', () => api.get('/api/trading/auto/status'), { intervalMs: 30000 });
  const { data: health } = useSWR('/api/health', () => api.get('/api/health'), { intervalMs: 30000 });
  const { data: account } = useSWR('/api/trading/account', () => api.get('/api/trading/account'), { intervalMs: 30000 });
  if (!account && !status) {
    return <div className="surface rounded-xl p-3 skel h-14" data-r49-card />;
  }
  const equity = account?.equity || status?.equity || 0;
  const bp = account?.buying_power || 0;
  const today = status?.realized_today || 0;
  const ddRaw = health?.session_dd_pct;
  const ddAvail = ddRaw != null && Number.isFinite(ddRaw);
  const dd = ddAvail ? ddRaw * 100 : 0;
  const heat = status?.current_heat_pct ?? null;
  const heatCap = (status?.config?.max_pct_of_equity || 0.5) * 100;
  const cells = [
    { label: 'Equity', value: `$${equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}`, kind: '' },
    { label: 'Today P/L', value: `${today >= 0 ? '+' : ''}$${today.toFixed(0)}`, kind: today >= 0 ? 'good' : 'bad', icon: today >= 0 ? 'up' : 'down' },
    { label: 'DD', value: ddAvail ? `${dd.toFixed(2)}%` : '—', kind: ddAvail ? (dd >= 4 ? 'bad' : dd >= 2 ? 'warn' : '') : '' },
    { label: 'BP', value: `$${bp.toLocaleString(undefined, { maximumFractionDigits: 0 })}`, kind: '' },
    { label: 'Heat', value: heat != null ? `${heat.toFixed(1)}%/${heatCap.toFixed(0)}%` : '—', kind: heat != null && heat >= heatCap * 0.85 ? 'warn' : '' },
    { label: 'Mode', value: health?.crisis_mode ? 'CRISIS' : (status?.enabled ? 'ARMED' : 'PAUSED'), kind: health?.crisis_mode ? 'bad' : (status?.enabled ? 'good' : 'warn') },
  ];
  const kindClass = { good: 'text-emerald-400', bad: 'text-red-400', warn: 'text-amber-400' };
  return (
    <div className="surface rounded-xl px-3 py-2 shadow-xl border app-border-soft" data-r49-card>
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-3">
        {cells.map((c, i) => (
          <div key={i} className="flex flex-col">
            <div className="text-[9px] uppercase tracking-wider app-text-muted font-semibold">{c.label}</div>
            <div className={`font-mono font-bold text-sm sm:text-base ${kindClass[c.kind] || 'app-text-primary'}`}>
              {c.icon && <DirIcon dir={c.icon} className="text-xs mr-0.5" />}
              {c.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// r49: alert inbox — persistent notification surface; ⌘+K to open
function AlertInbox() {
  const { items, unread, markRead, clear } = useNotifications();
  const [open, setOpen] = useState(false);
  useKeyboard(({ key, cmd, isInput }) => {
    if (cmd && key === 'i' && !isInput) {
      setOpen(o => !o);
      if (!open) markRead();
    }
    if (key === 'escape' && open) setOpen(false);
  }, [open, markRead]);
  return (
    <>
      <button
        onClick={() => { setOpen(o => !o); if (!open) markRead(); }}
        className="relative px-2.5 py-1 rounded-lg surface-soft border app-border-soft text-xs font-semibold app-text-secondary hover:app-text-primary"
        aria-label={`Alerts inbox${unread ? ` (${unread} unread)` : ''}`}
        title="⌘I — Alert inbox"
      >
        🔔
        {unread > 0 && (
          <span className="absolute -top-1 -right-1 bg-red-500 text-white text-[9px] font-bold rounded-full w-4 h-4 flex items-center justify-center">
            {unread > 9 ? '9+' : unread}
          </span>
        )}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Alert inbox"
          className="fixed top-14 right-4 w-80 max-h-[70vh] overflow-y-auto z-50 surface rounded-xl border app-border shadow-2xl"
        >
          <div className="p-3 border-b app-border flex items-center justify-between">
            <h3 className="text-sm font-bold">Notifications</h3>
            <div className="flex gap-2">
              <button onClick={clear} className="text-xs app-text-muted hover:app-text-primary">Clear</button>
              <button onClick={() => setOpen(false)} className="text-xs app-text-muted hover:app-text-primary" aria-label="Close">✕</button>
            </div>
          </div>
          {items.length === 0 ? (
            <div className="p-4 text-xs app-text-muted text-center">No notifications.</div>
          ) : (
            <div className="divide-y app-border-soft">
              {items.map(n => (
                <div key={n.id} className="p-3 text-xs">
                  <div className="flex items-start gap-2">
                    <span className={`inline-block w-1.5 h-1.5 rounded-full mt-1.5 ${
                      n.severity === 'critical' || n.severity === 'error' ? 'bg-red-400' :
                      n.severity === 'warning' ? 'bg-amber-400' :
                      n.severity === 'success' ? 'bg-emerald-400' : 'bg-blue-400'
                    }`} />
                    <div className="flex-1 min-w-0">
                      <div className="font-medium leading-relaxed">{n.message || n.msg}</div>
                      <div className="text-[10px] app-text-muted mt-1">
                        {n.category} · {new Date(n.ts).toLocaleString()}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}

// r49: quick-trade ticket palette — ⌘+K opens; ticker + side + qty + bracket
function QuickTradePalette({ onClose }) {
  const [ticker, setTicker] = useState('');
  const [side, setSide] = useState('buy');
  const [qty, setQty] = useState(10);
  const [type, setType] = useState('market');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const inputRef = useRef(null);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Quick analysis preview when ticker entered
  useEffect(() => {
    if (!ticker || ticker.length < 1) { setAnalysis(null); return; }
    let cancelled = false;
    const t = setTimeout(() => {
      api.get(`/api/analysis/${ticker.toUpperCase()}`)
        .then(d => { if (!cancelled) setAnalysis(d); })
        .catch(() => { if (!cancelled) setAnalysis(null); });
    }, 250);
    return () => { cancelled = true; clearTimeout(t); };
  }, [ticker]);

  const submit = async () => {
    if (!ticker || qty < 1) { setErr('Ticker + qty required'); return; }
    setSubmitting(true); setErr(null); setResult(null);
    try {
      const sig = analysis?.primary_signal;
      const payload = {
        symbol: ticker.toUpperCase(),
        qty: Number(qty),
        side,
        entry_type: type,
        time_in_force: 'gtc',
      };
      if (sig?.target1) payload.take_profit = sig.target1;
      if (sig?.stop_loss) payload.stop_loss = sig.stop_loss;
      const res = await api.post('/api/trading/order', payload);
      setResult(res);
      pushNotification({ severity: 'info', category: 'order_submitted', message: `Quick-ticket ${side.toUpperCase()} ${qty} ${ticker.toUpperCase()} → ${res.id?.slice(0, 8)}` });
      swrInvalidate('/api/trading');
      setTimeout(onClose, 1500);
    } catch (e) {
      setErr(friendlyError(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[55] bg-black/60 flex items-start justify-center pt-24" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Quick trade ticket"
        className="surface rounded-2xl border app-border w-full max-w-lg shadow-2xl mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="p-4 border-b app-border flex items-center justify-between">
          <div>
            <h3 className="text-lg font-bold">Quick Ticket</h3>
            <div className="text-[11px] app-text-muted">⌘K · Esc to close</div>
          </div>
          <button onClick={onClose} className="app-text-muted hover:app-text-primary" aria-label="Close">✕</button>
        </div>
        <div className="p-4 space-y-3">
          <div>
            <label className="text-[10px] uppercase tracking-wider app-text-muted block mb-1">Ticker</label>
            <input
              ref={inputRef}
              value={ticker}
              onChange={e => setTicker(e.target.value.toUpperCase())}
              placeholder="e.g. AAPL"
              className="w-full bg-transparent border app-border rounded-lg px-3 py-2 text-lg font-mono font-bold focus:border-blue-500"
            />
          </div>
          {analysis?.current_price && (
            <div className="text-xs surface-soft rounded-lg p-2">
              <div className="flex items-baseline gap-2">
                <span className="font-mono font-bold text-base">${analysis.current_price.toFixed(2)}</span>
                {analysis.change_pct != null && (
                  <span className={analysis.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                    {analysis.change_pct >= 0 ? '+' : ''}{analysis.change_pct.toFixed(2)}%
                  </span>
                )}
                {analysis.primary_signal && (
                  <span className="ml-auto">
                    <SignalBadge type={analysis.primary_signal.signal_type} confidence={analysis.primary_signal.confidence} />
                  </span>
                )}
              </div>
              {analysis.primary_signal?.entry && (
                <div className="text-[10px] app-text-muted mt-1 font-mono">
                  Bot: entry ${analysis.primary_signal.entry.toFixed(2)} ·
                  stop ${analysis.primary_signal.stop_loss?.toFixed(2)} ·
                  T1 ${analysis.primary_signal.target1?.toFixed(2)}
                </div>
              )}
            </div>
          )}
          <div className="grid grid-cols-3 gap-2">
            <div>
              <label className="text-[10px] uppercase tracking-wider app-text-muted block mb-1">Side</label>
              <div className="flex gap-1">
                <button
                  onClick={() => setSide('buy')}
                  className={`flex-1 px-2 py-1.5 rounded text-xs font-bold ${side === 'buy' ? 'bg-emerald-600 text-white' : 'surface-soft app-text-secondary'}`}
                >▲ BUY</button>
                <button
                  onClick={() => setSide('sell')}
                  className={`flex-1 px-2 py-1.5 rounded text-xs font-bold ${side === 'sell' ? 'bg-red-600 text-white' : 'surface-soft app-text-secondary'}`}
                >▼ SELL</button>
              </div>
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-wider app-text-muted block mb-1">Qty</label>
              <input
                type="number" min="1" max="10000" value={qty} onChange={e => setQty(Number(e.target.value))}
                className="w-full bg-transparent border app-border rounded px-2 py-1.5 text-sm font-mono"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase tracking-wider app-text-muted block mb-1">Type</label>
              <select
                value={type} onChange={e => setType(e.target.value)}
                className="w-full bg-transparent border app-border rounded px-2 py-1.5 text-xs"
              >
                <option value="market">Market</option>
                <option value="limit_at_mid">Limit @ mid</option>
              </select>
            </div>
          </div>
          {err && <div role="alert" className="text-xs text-red-300 bg-red-500/10 border border-red-500/40 rounded p-2">⚠ {err}</div>}
          {result && <div role="status" className="text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/40 rounded p-2">✅ Submitted #{result.id?.slice(0, 8)}</div>}
          <button
            onClick={submit}
            disabled={submitting || !ticker || qty < 1}
            className="w-full py-2.5 rounded-lg font-bold bg-gradient-to-b from-blue-500 to-blue-600 disabled:opacity-50 text-white"
          >
            {submitting ? 'Submitting…' : `Submit ${side.toUpperCase()} ${qty} ${ticker || '—'}`}
          </button>
          <div className="text-[10px] app-text-muted text-center">Bracket attached when bot has a current signal · ⏎ submit · Esc cancel</div>
        </div>
      </div>
    </div>
  );
}

// r49: skip-link for accessibility
function SkipLink() {
  return <a href="#r49-main" className="r49-skip-link">Skip to main content</a>;
}

// r49: my-position banner — only when holding the analyzed ticker
function MyPositionBanner({ ticker, position, currentPrice, signal }) {
  if (!position || !currentPrice) return null;
  const entry = position.avg_entry_price;
  const stop = position.current_stop || (signal && signal.stop_loss);
  const t1 = position.target1 || (signal && signal.target1);
  const t2 = position.target2 || (signal && signal.target2);
  const t3 = position.target3 || (signal && signal.target3);
  const side = (position.side || (position.qty < 0 ? 'sell' : 'buy')).toLowerCase();
  const isLong = side === 'buy' || side === 'long';
  const direction = isLong ? 'BUY' : 'SELL';
  const r = stop && entry
    ? (currentPrice - entry) / Math.max(1e-9, Math.abs(entry - stop)) * (isLong ? 1 : -1)
    : null;
  const distToStop = stop ? Math.abs(currentPrice - stop) : null;
  const distToStopPct = distToStop && currentPrice ? (distToStop / currentPrice) * 100 : null;
  const winning = (position.unrealized_pl ?? 0) >= 0;

  // Time held
  const opened = parseServerDate(position.opened_at) || parseServerDate(position.created_at);
  const heldMin = opened ? Math.max(0, (Date.now() - opened.getTime()) / 60000) : null;
  const heldFmt = heldMin == null ? '—' :
    heldMin < 60 ? `${heldMin.toFixed(0)}m` :
    heldMin < 1440 ? `${(heldMin / 60).toFixed(1)}h` :
    `${(heldMin / 1440).toFixed(1)}d`;

  const closeUndoable = () => {
    stageAction({
      label: `Closing ${ticker} (${position.qty} sh) — undo within 4s`,
      delayMs: 4000,
      kind: 'warn',
      onConfirm: async () => {
        try {
          await api.post(`/api/trading/close/${ticker}`);
          toast({ msg: `Closed ${ticker}`, kind: 'success', duration: 3000 });
          swrInvalidate('/api/trading');
          window.dispatchEvent(new CustomEvent('app:trade_closed'));
        } catch (e) {
          toast({ msg: `Close ${ticker} failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
        }
      },
    });
  };

  const moveStopBE = async () => {
    if (!entry) return;
    try {
      // r53f: was POSTing {action:'move_stop_be',...} to /api/trading/order
      // which silently 422'd because OrderRequest's schema rejected the
      // extra fields. Now uses the dedicated /move-stop endpoint and
      // surfaces the actual server response.
      const res = await api.post('/api/trading/move-stop', {
        symbol: ticker,
        new_stop: entry,
      });
      const broker = res?.broker || {};
      const note = broker.replaced_id ? `replaced broker SL ${String(broker.replaced_id).slice(0,8)}`
        : broker.resubmitted_id ? `resubmitted broker SL ${String(broker.resubmitted_id).slice(0,8)}`
        : broker.note || 'updated';
      toast({
        msg: `Stop → BE @ $${Number(res?.new_stop ?? entry).toFixed(2)} (${note})`,
        kind: 'success',
        duration: 4000,
      });
      window.dispatchEvent(new CustomEvent('app:trade_closed'));  // refresh positions
    } catch (e) {
      toast({ msg: `Move stop failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
    }
  };

  return (
    <div
      className={`surface rounded-2xl border-2 ${winning ? 'border-emerald-500/40' : 'border-red-500/40'} p-4 shadow-xl`}
      role="region"
      aria-label="Your open position in this ticker"
    >
      <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <span className="text-xs uppercase tracking-wider app-text-muted">Your Position</span>
            <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${isLong ? 'bg-emerald-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'}`}>
              <DirIcon dir={direction} /> {direction}
            </span>
          </div>
          <div className="font-mono text-sm">
            <span className="font-bold">{Math.abs(position.qty)}</span>
            <span className="app-text-muted"> @ ${entry?.toFixed(2)}</span>
          </div>
          <div className="text-xs app-text-muted">held {heldFmt}</div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={moveStopBE}
            className="text-xs px-2.5 py-1 rounded-md bg-blue-500/20 hover:bg-blue-500/30 text-blue-300 border border-blue-500/40 font-semibold"
            disabled={!entry}
            title="Set stop-loss to break-even"
          >Move stop → BE</button>
          <button
            onClick={closeUndoable}
            className="text-xs px-2.5 py-1 rounded-md bg-red-500/20 hover:bg-red-500/30 text-red-300 border border-red-500/40 font-semibold"
          >Close (undoable)</button>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
        <div>
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Now</div>
          <div className="font-mono font-bold text-base">${currentPrice.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Unrealized</div>
          <div className={`font-mono font-bold text-base ${winning ? 'text-emerald-400' : 'text-red-400'}`}>
            {winning ? '+' : ''}${(position.unrealized_pl ?? 0).toFixed(2)}
            <span className="text-xs ml-1">({(position.unrealized_plpc ?? 0).toFixed(2)}%)</span>
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider app-text-muted">R-multiple</div>
          <div className={`font-mono font-bold text-base ${r >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {r != null ? `${r >= 0 ? '+' : ''}${r.toFixed(2)}R` : '—'}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Dist to stop</div>
          <div className="font-mono font-bold text-base text-amber-400">
            {distToStop != null ? `$${distToStop.toFixed(2)} (${distToStopPct?.toFixed(1)}%)` : '—'}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Stop / T1</div>
          <div className="font-mono text-xs">
            <span className="text-red-400">${stop?.toFixed(2) ?? '—'}</span>
            <span className="app-text-muted"> / </span>
            <span className="text-emerald-400">${t1?.toFixed(2) ?? '—'}</span>
          </div>
        </div>
      </div>
      {entry && stop && (
        <div className="mt-3">
          <RProgressBar
            entry={entry} stop={stop} target1={t1} target2={t2} target3={t3}
            current={currentPrice} side={direction}
          />
          <div className="flex items-center justify-between mt-1 text-[9px] app-text-muted font-mono">
            <span>Stop ${stop.toFixed(2)}</span>
            <span>Entry ${entry.toFixed(2)}</span>
            <span>{t1 ? `T1 $${t1.toFixed(2)}` : ''}</span>
            <span>{t2 ? `T2 $${t2.toFixed(2)}` : ''}</span>
            <span>{t3 ? `T3 $${t3.toFixed(2)}` : ''}</span>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Watchlist Panel ----------
// r42 fix #1.26: WatchlistPanel re-renders on every quote tick because
// `overview` is memoized at the parent. Wrap with React.memo so equal-
// reference props skip the re-render entirely. Stable parent callbacks
// (loadOverview, handleAdd, handleRemove) make this safe.
const WatchlistPanel = React.memo(function WatchlistPanelImpl({ overview, selected, onSelect, onAdd, onRemove, onRefresh, onCloseMobile }) {
  const [newTicker, setNewTicker] = useState('');
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState(null);
  // r49: watchlist sort + filter persisted to localStorage
  const [sortKey, setSortKey] = usePersistentState('watchlistSort', 'default');
  const [filterText, setFilterText] = useState('');

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!newTicker.trim()) return;
    setAdding(true);
    setError(null);
    try {
      await onAdd(newTicker.toUpperCase().trim());
      setNewTicker('');
    } catch (err) {
      setError(String(err));
    } finally {
      setAdding(false);
    }
  };

  const sortedOverview = useMemo(() => {
    const arr = filterText
      ? overview.filter(it => it.ticker.toLowerCase().includes(filterText.toLowerCase()))
      : [...overview];
    const cmp = {
      'default': (a, b) => 0,
      'ticker': (a, b) => a.ticker.localeCompare(b.ticker),
      'change': (a, b) => (b.change_pct ?? -Infinity) - (a.change_pct ?? -Infinity),
      'conf': (a, b) => (b.confidence ?? -Infinity) - (a.confidence ?? -Infinity),
      'price': (a, b) => (b.price ?? 0) - (a.price ?? 0),
    }[sortKey] || ((a, b) => 0);
    return arr.sort(cmp);
  }, [overview, sortKey, filterText]);

  return (
    <div className="w-full surface border-r app-border flex flex-col h-full">
      <div className="p-3.5 border-b app-border">
        <div className="flex items-center justify-between mb-2.5 gap-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] app-text-secondary">Watchlist</h2>
          <div className="flex items-center gap-1">
            <button onClick={onRefresh} className="w-7 h-7 rounded-md app-text-muted hover:app-text-primary hover:bg-white/5 flex items-center justify-center" title="Refresh">⟳</button>
            {onCloseMobile && (
              <button onClick={onCloseMobile} className="md:hidden w-7 h-7 rounded-md app-text-muted hover:app-text-primary hover:bg-white/5 flex items-center justify-center" title="Close">✕</button>
            )}
          </div>
        </div>
        <form onSubmit={handleAdd} className="flex gap-1">
          <input
            type="text"
            value={newTicker}
            onChange={(e) => setNewTicker(e.target.value)}
            placeholder="Add ticker (e.g. AAPL)"
            className="flex-1 bg-gray-900/60 border border-white/10 rounded-lg px-3 py-1.5 text-sm placeholder-gray-500 focus:border-blue-500/70"
            disabled={adding}
          />
          <button type="submit" disabled={adding} className="bg-gradient-to-b from-blue-500 to-blue-600 hover:from-blue-400 hover:to-blue-500 disabled:opacity-50 px-3 py-1.5 rounded-lg text-sm font-semibold shadow-lg shadow-blue-500/20">+</button>
        </form>
        {/* r49: filter + sort */}
        <div className="flex gap-1 mt-2">
          <input
            type="text"
            value={filterText}
            onChange={e => setFilterText(e.target.value)}
            placeholder="Filter…"
            className="flex-1 bg-transparent border app-border-soft rounded px-2 py-1 text-xs"
            aria-label="Filter watchlist"
          />
          <select
            value={sortKey}
            onChange={e => setSortKey(e.target.value)}
            className="bg-transparent border app-border-soft rounded px-1 py-1 text-[10px]"
            aria-label="Sort watchlist"
          >
            <option value="default">Sort: order</option>
            <option value="ticker">A→Z</option>
            <option value="change">% chg</option>
            <option value="conf">Conf</option>
            <option value="price">Price</option>
          </select>
        </div>
        {error && <div className="text-xs text-red-400 mt-1">{error}</div>}
      </div>
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {sortedOverview.length === 0 && <div className="p-4 text-sm text-gray-500 text-center">{overview.length === 0 ? 'No stocks yet. Add one above.' : 'No matches.'}</div>}
        {sortedOverview.map((item) => (
          <div
            key={item.ticker}
            onClick={() => onSelect(item.ticker)}
            className={`px-3 py-2.5 border-b border-white/5 cursor-pointer transition-colors ${selected === item.ticker ? 'bg-gradient-to-r from-blue-500/15 via-blue-500/5 to-transparent border-l-2 border-l-blue-500' : 'border-l-2 border-l-transparent hover:bg-white/5'}`}
          >
            <div className="flex items-center justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{item.ticker}</span>
                  {item.signal_type && item.signal_type !== 'NEUTRAL' && (
                    <SignalBadge type={item.signal_type} confidence={item.confidence} isNew={item.is_new} />
                  )}
                </div>
                <div className="text-xs text-gray-500 truncate">{item.name}</div>
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); onRemove(item.ticker); }}
                className="text-gray-600 hover:text-red-400 text-xs ml-2"
                title="Remove"
              >
                ✕
              </button>
            </div>
            {item.price != null && (
              <div className="flex items-baseline justify-between mt-1">
                <span className={`text-sm font-mono font-semibold tabular-nums ${item.stale ? 'opacity-50' : ''}`}>
                  ${item.price.toFixed(2)}
                  {item.stale && (
                    <span
                      className="ml-1 text-[9px] uppercase tracking-wider text-amber-400"
                      title="Quote is stale — last tick > 30s ago, or WS disconnected"
                    >stale</span>
                  )}
                </span>
                {/* Audit fix M8: `null >= 0` is true in JS, so guard rendering
                    explicitly to avoid showing "+undefined%" when change_pct
                    is null/NaN. */}
                {item.change_pct != null && Number.isFinite(item.change_pct) ? (
                  <span className={`text-xs ${item.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {item.change_pct >= 0 ? '+' : ''}{item.change_pct.toFixed(2)}%
                  </span>
                ) : (
                  <span className="text-xs text-gray-500">—</span>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
});

// ---------- Stock Chart ----------
function StockChart({
  ticker, timeframe, liveQuote = null, theme = 'dark',
  hideIndicators = false,
  // r49: layered overlay toggles (default all on for parity with prior code)
  showMAs = true, showBB = true, showSR = true, showZones = true, showFibs = true, showNews = true,
  // r49: trade-level rendering — entry/SL/T1/T2/T3 lines on chart
  signal = null,
  // r49: open-position overlay — entry-line + R-progress shading
  position = null,
  // r49: news markers (array of {timestamp, severity, headline, sentiment_label})
  newsEvents = [],
  height = 460,
}) {
  const lastCandleRef = useRef(null);  // {time, open, high, low, close} of the bar we keep mutating
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef({});
  // Explicit price-line tracking — lightweight-charts has no public iterable
  // for createPriceLine outputs, so we track every line we add and removePriceLine
  // them on the next refresh. Without this, lines accumulated on every fetch.
  const priceLineRefs = useRef([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [srLevels, setSrLevels] = useState([]);
  // Cache the last-fetched chart data so toggling hideIndicators can redraw
  // without re-hitting the backend.
  const lastDataRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = LightweightCharts.createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: height,
      ...chartThemeOptions(),
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    chartRef.current = chart;

    const candle = chart.addCandlestickSeries({
      upColor: '#10b981', downColor: '#ef4444',
      borderUpColor: '#10b981', borderDownColor: '#ef4444',
      wickUpColor: '#10b981', wickDownColor: '#ef4444',
    });
    const volume = chart.addHistogramSeries({
      color: '#3b82f6', priceFormat: { type: 'volume' },
      priceScaleId: '', scaleMargins: { top: 0.85, bottom: 0 },
    });
    seriesRef.current = { candle, volume, indicators: {} };

    const ro = new ResizeObserver(() => {
      // Container may be detached during unmount race — guard before applying.
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      // Order matters: disconnect the observer FIRST so no queued resize
      // callback fires on a destroyed chart instance, then remove the chart.
      ro.disconnect();
      try { chart.remove(); } catch (e) { /* already removed */ }
      chartRef.current = null;
      seriesRef.current = { candle: null, volume: null, indicators: {} };
    };
  }, [theme, height]);

  // r49: live-tick last-bar update is handled by the existing
  // bar-roll-aware effect further down (around line ~1850); my new
  // trade-level / position / news-marker effects below extend the chart
  // without duplicating that logic.

  // r49: trade-level overlay — render entry/SL/T1/T2/T3 horizontal lines
  // when a signal is provided. Lines persist across data-loads via separate
  // refs so they don't get cleared by `clearPriceLines()` in the data effect.
  const tradeLineRefs = useRef([]);
  useEffect(() => {
    const c = seriesRef.current?.candle;
    if (!c) return;
    // Clear existing trade lines
    for (const ln of tradeLineRefs.current) {
      try { c.removePriceLine(ln); } catch (_) {}
    }
    tradeLineRefs.current = [];
    if (!signal || hideIndicators) return;
    const addLine = (price, color, title, lineWidth = 2, style = LightweightCharts.LineStyle.Solid) => {
      if (!price || !Number.isFinite(price)) return;
      try {
        const ln = c.createPriceLine({ price, color, lineWidth, lineStyle: style, axisLabelVisible: true, title });
        tradeLineRefs.current.push(ln);
      } catch (_) {}
    };
    if (signal.entry) addLine(signal.entry, '#3b82f6', `Entry $${signal.entry.toFixed(2)}`, 2, LightweightCharts.LineStyle.Solid);
    if (signal.stop_loss) addLine(signal.stop_loss, '#ef4444', `Stop $${signal.stop_loss.toFixed(2)}`, 2, LightweightCharts.LineStyle.Dashed);
    if (signal.target1) addLine(signal.target1, '#10b981', `T1 $${signal.target1.toFixed(2)}`, 2, LightweightCharts.LineStyle.Dashed);
    if (signal.target2) addLine(signal.target2, '#10b981', `T2 $${signal.target2.toFixed(2)}`, 1, LightweightCharts.LineStyle.Dashed);
    if (signal.target3) addLine(signal.target3, '#10b981', `T3 $${signal.target3.toFixed(2)}`, 1, LightweightCharts.LineStyle.Dotted);
    return () => {
      const cc = seriesRef.current?.candle;
      if (cc) for (const ln of tradeLineRefs.current) { try { cc.removePriceLine(ln); } catch (_) {} }
      tradeLineRefs.current = [];
    };
  }, [signal?.entry, signal?.stop_loss, signal?.target1, signal?.target2, signal?.target3, hideIndicators]);

  // r49: open-position overlay — entry-line + current-stop line. Distinct
  // styling from signal-levels so trader can tell "what bot wants" vs "what
  // I actually have".
  const positionLineRefs = useRef([]);
  useEffect(() => {
    const c = seriesRef.current?.candle;
    if (!c) return;
    for (const ln of positionLineRefs.current) { try { c.removePriceLine(ln); } catch (_) {} }
    positionLineRefs.current = [];
    if (!position) return;
    const addLine = (price, color, title, lineWidth, style) => {
      if (!price || !Number.isFinite(price)) return;
      try {
        const ln = c.createPriceLine({ price, color, lineWidth, lineStyle: style, axisLabelVisible: true, title });
        positionLineRefs.current.push(ln);
      } catch (_) {}
    };
    if (position.avg_entry_price) {
      addLine(position.avg_entry_price, '#a855f7', `★ Entry ${position.avg_entry_price.toFixed(2)}`, 3, LightweightCharts.LineStyle.Solid);
    }
    if (position.current_stop && position.current_stop !== position.avg_entry_price) {
      addLine(position.current_stop, '#fb923c', `★ Stop ${position.current_stop.toFixed(2)}`, 2, LightweightCharts.LineStyle.Dashed);
    }
    return () => {
      const cc = seriesRef.current?.candle;
      if (cc) for (const ln of positionLineRefs.current) { try { cc.removePriceLine(ln); } catch (_) {} }
      positionLineRefs.current = [];
    };
  }, [position?.avg_entry_price, position?.current_stop]);

  // r49: news markers — vertical pins at bar timestamps for sev≥35 events.
  useEffect(() => {
    const c = seriesRef.current?.candle;
    if (!c || !showNews || !newsEvents || newsEvents.length === 0) return;
    // lightweight-charts v4 supports series.setMarkers([{time, position, color, shape, text}])
    try {
      const markers = newsEvents.slice(0, 30).map(n => {
        const d = parseServerDate(n.published_at || n.ts);
        if (!d) return null;
        const time = Math.floor(d.getTime() / 1000);
        const positive = n.sentiment_label === 'positive';
        const negative = n.sentiment_label === 'negative';
        return {
          time,
          position: positive ? 'belowBar' : 'aboveBar',
          color: positive ? '#10b981' : negative ? '#ef4444' : '#94a3b8',
          shape: positive ? 'arrowUp' : negative ? 'arrowDown' : 'circle',
          text: '',
        };
      }).filter(Boolean);
      c.setMarkers(markers);
    } catch (_) {}
    return () => { try { c.setMarkers([]); } catch (_) {} };
  }, [newsEvents, showNews]);

  useEffect(() => {
    if (!ticker || !chartRef.current) return;
    setLoading(true); setError(null);
    // Abort in-flight fetches when ticker/timeframe changes mid-flight, so we
    // don't apply stale data to the new chart context (or write to a destroyed
    // series during ticker switches).
    const ac = new AbortController();
    let cancelled = false;
    // Audit fix M6: when ticker/timeframe changes the chart-creation effect
    // does NOT recreate the chart (its deps are []), only this data-load
    // effect re-fires. Without clearing lastCandleRef, a live tick arriving
    // between abort + new setData mutates the OLD timeframe's last bar with
    // a 1d-bar timestamp on a chart now showing 5m bars — lightweight-charts
    // either rejects or inserts at the wrong location.
    lastCandleRef.current = null;
    // Helper: removePriceLine on every line we tracked, clearing the registry.
    const clearPriceLines = () => {
      const c = seriesRef.current?.candle;
      if (c) {
        for (const ln of priceLineRefs.current) {
          try { c.removePriceLine(ln); } catch (e) { /* line/series gone */ }
        }
      }
      priceLineRefs.current = [];
    };
    // Helper: createPriceLine + register so the next refresh can purge it.
    const addPriceLine = (opts) => {
      const c = seriesRef.current?.candle;
      if (!c) return null;
      try {
        const ln = c.createPriceLine(opts);
        priceLineRefs.current.push(ln);
        return ln;
      } catch (e) {
        return null;
      }
    };
    // Debounce: when the user rapidly clicks through timeframe tabs
    // (5m → 15m → 30m → 1h), each click previously kicked off a full
    // backend request — indicators + S/R + zones + fibs + gaps. Even with
    // abort, every intermediate request started network I/O and
    // competed for the Yahoo rate-limiter. 160ms covers a double-click
    // without making deliberate switches feel laggy.
    const _debounceMs = 160;
    const _fetchTimer = setTimeout(() => {
    fetch(`${API_BASE}/api/analysis/${ticker}/chart?timeframe=${timeframe}`, { signal: ac.signal, headers: authHeaders() })
      .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
      .then(data => {
        if (cancelled || !chartRef.current || !seriesRef.current.candle) return;
        const { candle, volume, indicators } = seriesRef.current;
        const candleBars = data.candles.map(c => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        }));
        candle.setData(candleBars);
        lastCandleRef.current = candleBars.length ? { ...candleBars[candleBars.length - 1] } : null;
        volume.setData(data.candles.map(c => ({
          time: c.time, value: c.volume,
          color: c.close >= c.open ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)',
        })));

        // Clear old indicator series
        Object.values(indicators).forEach(s => chartRef.current.removeSeries(s));
        seriesRef.current.indicators = {};

        // Cache raw data for the hideIndicators-toggle redraw path.
        lastDataRef.current = data;

        if (!hideIndicators) {
          // r49: layered MA / BB toggles. Indicator names from the backend
          // include SMA_20 / SMA_50 / SMA_200 / EMA_21 / BBU_20 / BBL_20 / BBM_20.
          // MAs (showMAs=true): SMA*, EMA* | BB (showBB=true): BBU/BBL/BBM
          const isBB = (n) => /^BB[ULM]/.test(n);
          const isMA = (n) => /^(SMA|EMA)/.test(n);
          data.indicators.forEach(ind => {
            const name = ind.name || '';
            if (isBB(name) && !showBB) return;
            if (isMA(name) && !showMAs) return;
            // Other indicators (RSI, MACD, ATR) are handled in separate panes;
            // gate them under showMAs as the broad "indicator overlay" toggle.
            if (!isBB(name) && !isMA(name) && !showMAs) return;
            const lineSeries = chartRef.current.addLineSeries({
              color: ind.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
            });
            lineSeries.setData(ind.values.filter(v => v.value != null));
            seriesRef.current.indicators[name] = lineSeries;
          });
        }

        // Support/Resistance price lines on the candle series
        clearPriceLines();
        if (hideIndicators) {
          setSrLevels(data.support_resistance);
          // Skip all the overlay drawing — candles + volume only.
          const n = candleBars.length;
          const want = DEFAULT_VISIBLE_BARS[timeframe] || 150;
          if (n > 0) {
            const from = Math.max(0, n - want);
            chartRef.current.timeScale().setVisibleLogicalRange({ from, to: n + 2 });
          } else {
            chartRef.current.timeScale().fitContent();
          }
          setLoading(false);
          return;
        }
        if (showSR) data.support_resistance.forEach(lvl => {
          addPriceLine({
            price: lvl.price,
            color: lvl.type === 'support' ? '#10b981' : '#ef4444',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: lvl.type === 'support' ? `S ${lvl.price}` : `R ${lvl.price}`,
          });
        });

        // Supply/Demand zones — drawn as two price lines per zone (high + low) forming a band
        const zones = showZones ? (data.supply_demand_zones || {}) : {};
        (zones.demand || []).forEach(z => {
          addPriceLine({
            price: z.high, color: 'rgba(16,185,129,0.55)', lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true,
            title: `Demand ${z.low}-${z.high} (${z.score})`,
          });
          addPriceLine({
            price: z.low, color: 'rgba(16,185,129,0.55)', lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: false, title: '',
          });
        });
        (zones.supply || []).forEach(z => {
          addPriceLine({
            price: z.high, color: 'rgba(239,68,68,0.55)', lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true,
            title: `Supply ${z.low}-${z.high} (${z.score})`,
          });
          addPriceLine({
            price: z.low, color: 'rgba(239,68,68,0.55)', lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: false, title: '',
          });
        });

        // Fibonacci retracements + extensions
        const fib = showFibs ? data.fibonacci : null;
        if (fib) {
          const KEY = new Set([0.382, 0.5, 0.618]);
          const retColor = (r) => KEY.has(r) ? 'rgba(250,204,21,0.85)' : 'rgba(250,204,21,0.45)';
          (fib.retracements || []).forEach(r => {
            addPriceLine({
              price: r.price,
              color: retColor(r.ratio),
              lineWidth: KEY.has(r.ratio) ? 2 : 1,
              lineStyle: LightweightCharts.LineStyle.Dotted,
              axisLabelVisible: true,
              title: `Fib ${r.label}`,
            });
          });
          (fib.extensions || []).forEach(e => {
            addPriceLine({
              price: e.price,
              color: 'rgba(168,85,247,0.55)',
              lineWidth: 1,
              lineStyle: LightweightCharts.LineStyle.Dotted,
              axisLabelVisible: true,
              title: `Fib ext ${e.label}`,
            });
          });
        }

        // Price gaps + Fair Value Gaps — drawn as banded price lines (top + bottom of zone)
        const gaps = data.gaps || {};
        const drawGap = (g) => {
          const isBull = g.direction === 'bull';
          const baseColor = isBull ? 'rgba(59,130,246,' : 'rgba(244,114,182,';
          const alpha = g.filled ? '0.25)' : '0.7)';
          const color = baseColor + alpha;
          const tag = g.kind === 'fvg' ? 'FVG' : 'Gap';
          const dirArrow = isBull ? '↑' : '↓';
          const fillStr = g.filled ? ' (filled)' : ` (${Math.round(g.fill_pct * 100)}% filled)`;
          addPriceLine({
            price: g.top,
            color,
            lineWidth: 1,
            lineStyle: g.filled ? LightweightCharts.LineStyle.Dotted : LightweightCharts.LineStyle.Solid,
            axisLabelVisible: true,
            title: `${tag}${dirArrow} ${g.bottom}-${g.top}${fillStr}`,
          });
          addPriceLine({
            price: g.bottom,
            color,
            lineWidth: 1,
            lineStyle: g.filled ? LightweightCharts.LineStyle.Dotted : LightweightCharts.LineStyle.Solid,
            axisLabelVisible: false,
            title: '',
          });
        };
        (gaps.price_gaps || []).filter(g => !g.filled || g.age_bars < 20).forEach(drawGap);
        (gaps.fvgs || []).filter(g => !g.filled).forEach(drawGap);

        setSrLevels(data.support_resistance);
        // Zoom to the last N bars rather than fitContent(). fitContent squeezes
        // ~11k 5-min bars into <1px each — the intraday chart then looks
        // indistinguishable from the daily line. setVisibleLogicalRange lets
        // individual candles render at readable widths.
        const n = candleBars.length;
        const want = DEFAULT_VISIBLE_BARS[timeframe] || 150;
        // Force HH:MM labels for intraday timeframes via a custom tickMarkFormatter.
        // Without this, lightweight-charts falls back to date labels any time
        // the visible range crosses midnight — even with timeVisible:true.
        const isIntraday = ['5m','15m','30m','1h','4h'].includes(timeframe);
        chartRef.current.applyOptions({
          timeScale: {
            timeVisible: isIntraday,
            secondsVisible: false,
            tickMarkFormatter: isIntraday
              ? (time) => {
                  const d = new Date(time * 1000);
                  const hh = String(d.getHours()).padStart(2, '0');
                  const mm = String(d.getMinutes()).padStart(2, '0');
                  return `${hh}:${mm}`;
                }
              : undefined,
          },
        });
        if (n > 0) {
          const from = Math.max(0, n - want);
          // +2 on the right so the most recent bar isn't flush against the
          // price-label gutter — leaves room for the live-tick extension.
          chartRef.current.timeScale().setVisibleLogicalRange({ from, to: n + 2 });
        } else {
          chartRef.current.timeScale().fitContent();
        }
        setLoading(false);
      })
      .catch(e => {
        if (cancelled || ac.signal.aborted) return;  // expected on switch — silent
        setError(String(e));
        setLoading(false);
      });
    }, _debounceMs);
    return () => {
      cancelled = true;
      clearTimeout(_fetchTimer);
      ac.abort();
    };
    // `theme` is in the deps because the chart creation effect recreates the
    // lightweight-charts instance on theme change — without re-running the
    // data effect, the new chart instance renders empty (no candles,
    // indicators, or price lines).
  }, [ticker, timeframe, hideIndicators, theme, showMAs, showBB, showSR, showZones, showFibs]);

  // ----- Live tick: extend OR ROLL the most recent bar based on wall-clock -----
  // Bug fix: previously this effect always mutated the most recent candle's
  // high/low/close with the latest tick. That meant once the 5-min (or any
  // intraday) bar's wall-clock window had passed, the chart kept extending
  // the SAME candle forever — until the next backend refetch overwrote
  // it. The fix detects when the live-tick time has crossed into a new
  // bar bucket and APPENDS a new candle in that case (lightweight-charts'
  // .update() accepts a strictly-newer time and creates a new bar).
  useEffect(() => {
    if (!liveQuote || !chartRef.current || !seriesRef.current.candle || !lastCandleRef.current) return;
    const px = liveQuote.last || (liveQuote.bid && liveQuote.ask ? (liveQuote.bid + liveQuote.ask) / 2 : null);
    if (!px) return;

    // Bar duration in seconds for the current timeframe
    const _BAR_SECS = {
      '5m': 300, '15m': 900, '30m': 1800, '1h': 3600,
      '4h': 14400, '1d': 86400, '1mo': 30 * 86400,
    };
    const dur = _BAR_SECS[timeframe];
    const bar = lastCandleRef.current;

    // If we don't know the duration (unexpected tf), fall back to the
    // legacy behavior — extending the last bar — rather than refusing to update.
    if (!dur) {
      bar.high = Math.max(bar.high, px);
      bar.low = Math.min(bar.low, px);
      bar.close = px;
      try { seriesRef.current.candle.update(bar); } catch (_) {}
      return;
    }

    // r42 Tier 3: backend ts can arrive in ms (>1e12) or sec; normalize.
    let _ts = liveQuote.ts;
    if (typeof _ts === 'number' && _ts > 1e12) _ts = _ts / 1000;
    const tickSec = Math.floor((_ts || Date.now() / 1000));
    const lastBucket = Math.floor(bar.time / dur) * dur;
    const tickBucket = Math.floor(tickSec / dur) * dur;

    if (tickBucket <= lastBucket) {
      // Same bucket — extend the existing bar.
      bar.high = Math.max(bar.high, px);
      bar.low = Math.min(bar.low, px);
      bar.close = px;
      try {
        seriesRef.current.candle.update(bar);
      } catch (e) {
        if (chartRef.current) console.debug('chart live-update skipped:', e?.message || e);
      }
    } else {
      // Crossed into a new bucket — open a new bar at tickBucket.
      // Open at previous close (typical for tick-rolled candles when the
      // first print of the new bar is the tick we just received).
      const newBar = {
        time: tickBucket,
        open: bar.close,
        high: Math.max(bar.close, px),
        low: Math.min(bar.close, px),
        close: px,
      };
      try {
        seriesRef.current.candle.update(newBar);
        lastCandleRef.current = newBar;
      } catch (e) {
        if (chartRef.current) console.debug('chart bar-roll skipped:', e?.message || e);
      }
    }
  }, [liveQuote?.last, liveQuote?.bid, liveQuote?.ask, liveQuote?.ts, timeframe]);

  return (
    <div className="relative w-full">
      <div ref={containerRef} className="w-full" />
      {loading && <div className="absolute inset-0 flex items-center justify-center bg-black/50 text-sm text-gray-400">Loading chart…</div>}
      {error && <div className="absolute top-2 left-2 text-xs text-red-400 bg-black/70 px-2 py-1 rounded">Error: {error}</div>}
    </div>
  );
}

// ---------- Timeframe Selector ----------
function TimeframeSelector({ value, onChange }) {
  return (
    <div className="flex surface-soft rounded-xl p-1 text-xs overflow-x-auto scrollbar-thin max-w-full">
      {TIMEFRAMES.map(tf => (
        <button
          key={tf}
          onClick={() => onChange(tf)}
          className={`px-2 sm:px-3 py-1.5 rounded-lg font-semibold tracking-wide shrink-0 ${value === tf ? 'bg-gradient-to-b from-blue-500 to-blue-600 text-white glow-blue' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
        >
          {tf.toUpperCase()}
        </button>
      ))}
    </div>
  );
}

// ---------- Signal Card ----------
function SignalCard({ signal, currentPrice, primarySignal = null }) {
  if (!signal) return null;
  const isBuy = signal.signal_type === 'BUY';
  const borderColor = isBuy ? 'border-emerald-600/50' : signal.signal_type === 'SELL' ? 'border-red-600/50' : 'border-gray-600/50';

  const rr = signal.entry && signal.stop_loss && signal.target1
    ? (Math.abs(signal.target1 - signal.entry) / Math.abs(signal.entry - signal.stop_loss)).toFixed(2)
    : null;

  // r49: dedup primary-signal duplication. When `signal` is for the user's
  // current TF AND primarySignal is for a different TF, we surface a single
  // pill that says "↗ Primary: 1d 75 BUY" rather than rendering a second card.
  const showPrimaryPill = primarySignal
    && primarySignal.timeframe !== signal.timeframe
    && primarySignal.signal_type !== 'NEUTRAL';

  return (
    <div className={`surface border ${borderColor} rounded-2xl p-5 shadow-xl shadow-black/20`} data-r49-card>
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-lg font-bold">Signal · {signal.timeframe}</h3>
          <SignalBadge type={signal.signal_type} confidence={signal.confidence} />
          <DirIcon dir={signal.signal_type} className={isBuy ? 'text-emerald-400' : signal.signal_type === 'SELL' ? 'text-red-400' : 'app-text-muted'} />
          {signal.strategy && (
            <span className="text-[10px] px-2 py-0.5 rounded bg-indigo-900/40 border border-indigo-700 text-indigo-300 uppercase tracking-wider" title="Strategy used to derive this signal">
              {signal.strategy}
            </span>
          )}
        </div>
        {showPrimaryPill && (
          <span
            className="text-[10px] px-2 py-0.5 rounded surface-soft border app-border-soft app-text-secondary"
            title={`Strongest signal across all timeframes: ${primarySignal.timeframe} ${primarySignal.signal_type} (${primarySignal.confidence})`}
          >
            ↗ Primary: {primarySignal.timeframe} {primarySignal.signal_type} {Math.round(primarySignal.confidence)}
          </span>
        )}
      </div>
      {signal.entry != null && (
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 mb-3 text-sm">
          <div className="surface-soft rounded-xl p-2.5">
            <div className="text-[10px] uppercase tracking-wider text-gray-500 font-semibold">Entry</div>
            <div className="font-mono font-semibold text-[15px] mt-0.5">${signal.entry.toFixed(2)}</div>
          </div>
          <div className="bg-red-500/10 rounded-xl p-2.5 border border-red-500/20">
            <div className="text-[10px] uppercase tracking-wider text-red-400 font-semibold">Stop Loss</div>
            <div className="font-mono font-semibold text-[15px] mt-0.5">${signal.stop_loss?.toFixed(2)}</div>
          </div>
          <div className="bg-emerald-500/10 rounded-xl p-2.5 border border-emerald-500/20">
            <div className="text-[10px] uppercase tracking-wider text-emerald-400 font-semibold">Target 1</div>
            <div className="font-mono font-semibold text-[15px] mt-0.5">${signal.target1?.toFixed(2)}</div>
          </div>
          <div className="bg-emerald-500/10 rounded-xl p-2.5 border border-emerald-500/20">
            <div className="text-[10px] uppercase tracking-wider text-emerald-400 font-semibold">Target 2</div>
            <div className="font-mono font-semibold text-[15px] mt-0.5">${signal.target2?.toFixed(2)}</div>
          </div>
          <div className="bg-emerald-500/10 rounded-xl p-2.5 border border-emerald-500/20">
            <div className="text-[10px] uppercase tracking-wider text-emerald-400 font-semibold">Target 3</div>
            <div className="font-mono font-semibold text-[15px] mt-0.5">${signal.target3?.toFixed(2)}</div>
          </div>
        </div>
      )}
      {rr && <div className="text-xs text-gray-400 mb-2">Risk/Reward (T1): <span className="text-white font-semibold">{rr}:1</span></div>}
      {signal.entry != null && signal.signal_type !== 'NEUTRAL' && (
        <TradeFromSignal signal={signal} />
      )}
      {signal.backtest_best_strategy && (
        <div className="mb-3 p-2 rounded border border-indigo-800/50 bg-indigo-950/30 text-xs">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div className="text-indigo-300">
              📊 Historical edge factored in: best {signal.signal_type} strategy
              <span className="font-semibold text-white"> {signal.backtest_best_strategy}</span>
            </div>
            <div className="text-gray-300">
              <span className={signal.backtest_return_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                {signal.backtest_return_pct >= 0 ? '+' : ''}{signal.backtest_return_pct?.toFixed(1)}%
              </span>
              <span className="text-gray-500"> · {signal.backtest_win_rate?.toFixed(0)}% WR · {signal.backtest_trades} trades · score {signal.backtest_score?.toFixed(0)}/100</span>
            </div>
          </div>
        </div>
      )}
      {!signal.backtest_best_strategy && signal.signal_type !== 'NEUTRAL' && (
        <div className="mb-3 p-2 rounded border border-yellow-800/50 bg-yellow-950/20 text-xs text-yellow-300">
          ⚠️ No {signal.signal_type} strategy has ≥3 historical trades on this ticker — confidence reduced by 25%.
        </div>
      )}
      <ReasoningBlock reasoning={signal.reasoning} />
      {signal.entry != null && <LevelMethodology signal={signal} />}
    </div>
  );
}

// ---------- Reasoning renderer ----------
// Parses signal.reasoning (multi-line text with ✅/❌/⚠️/📊 prefixes) into
// styled rows with coloured icons + aligned text. Falls back to plain
// pre-line text for unstructured content.
function ReasoningBlock({ reasoning }) {
  const [expanded, setExpanded] = useState(false);
  if (!reasoning || !reasoning.trim()) return null;

  const rawLines = reasoning.split(/\r?\n/).map(l => l.trim()).filter(Boolean);

  // Classify each line by leading symbol.
  const classify = (line) => {
    if (/^✅/.test(line))  return { kind: 'pos',  text: line.replace(/^✅\s*/, '') };
    if (/^❌/.test(line))  return { kind: 'neg',  text: line.replace(/^❌\s*/, '') };
    if (/^⚠️?/.test(line)) return { kind: 'warn', text: line.replace(/^⚠️?\s*/, '') };
    if (/^📊|^📈|^📉/.test(line)) return { kind: 'info', text: line.replace(/^[📊📈📉]\s*/, '') };
    if (/^[•\-–]/.test(line)) return { kind: 'bullet', text: line.replace(/^[•\-–]\s*/, '') };
    return { kind: 'prose', text: line };
  };

  const rows = rawLines.map(classify);
  const posCount = rows.filter(r => r.kind === 'pos').length;
  const negCount = rows.filter(r => r.kind === 'neg').length;
  const warnCount = rows.filter(r => r.kind === 'warn').length;

  // Collapsed by default when there are >6 rows; full list one click away.
  const VISIBLE = 12;  // r49: was 6 — most signals have 8-12 reasoning lines, truncating mid-list creates extra clicks per signal
  const shown = expanded ? rows : rows.slice(0, VISIBLE);
  const hiddenCount = Math.max(0, rows.length - VISIBLE);

  const iconFor = (kind) => {
    switch (kind) {
      case 'pos':    return <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-emerald-500/20 text-emerald-400 text-[10px] font-bold shrink-0">✓</span>;
      case 'neg':    return <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-red-500/20 text-red-400 text-[10px] font-bold shrink-0">×</span>;
      case 'warn':   return <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-amber-500/20 text-amber-400 text-[10px] font-bold shrink-0">!</span>;
      case 'info':   return <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-blue-500/20 text-blue-400 text-[10px] font-bold shrink-0">i</span>;
      case 'bullet': return <span className="inline-flex items-center justify-center w-4 h-4 app-text-muted text-[10px] shrink-0">•</span>;
      default:       return null;
    }
  };

  return (
    <div className="mt-3">
      {/* Header with factor tally */}
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-[0.14em] app-text-muted font-semibold">Reasoning</div>
        <div className="flex items-center gap-1.5 text-[10px]">
          {posCount > 0 && (
            <span className="pill pill-success font-mono">{posCount}✓</span>
          )}
          {negCount > 0 && (
            <span className="pill pill-danger font-mono">{negCount}×</span>
          )}
          {warnCount > 0 && (
            <span className="pill pill-warn font-mono">{warnCount}!</span>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="surface-soft rounded-xl overflow-hidden">
        <div className="divide-y" style={{ borderColor: 'var(--surface-border-soft)' }}>
          {shown.map((r, i) => {
            if (r.kind === 'prose' && !r.text.startsWith(' ')) {
              // Heading-style row
              return (
                <div key={i} className="px-3 py-1.5 text-[11px] font-semibold app-text-secondary uppercase tracking-wider">
                  {r.text}
                </div>
              );
            }
            return (
              <div key={i} className="px-3 py-2 flex items-start gap-2.5 text-xs leading-snug app-text-primary">
                {iconFor(r.kind)}
                <span className="flex-1">{r.text}</span>
              </div>
            );
          })}
        </div>
        {hiddenCount > 0 && (
          <button
            onClick={() => setExpanded(e => !e)}
            className="w-full py-2 text-[11px] app-text-secondary hover:app-text-primary border-t app-border-soft hover:bg-white/5"
          >
            {expanded ? 'Show less' : `Show ${hiddenCount} more factor${hiddenCount !== 1 ? 's' : ''}`}
          </button>
        )}
      </div>
    </div>
  );
}

// ---------- Level Methodology Explainer ----------
function LevelMethodology({ signal }) {
  const [open, setOpen] = useState(false);
  const isBuy = signal.signal_type === 'BUY';
  const e = signal.entry, sl = signal.stop_loss, t1 = signal.target1, t2 = signal.target2, t3 = signal.target3;
  const risk = e != null && sl != null ? Math.abs(e - sl) : null;
  const mult = (t) => risk && t != null ? (Math.abs(t - e) / risk).toFixed(2) : '—';
  return (
    <div className="mt-3 border border-gray-800 rounded">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs text-gray-300 hover:bg-gray-800/50"
      >
        <span className="uppercase tracking-wider">How entry, stop & targets are calculated</span>
        <span className="text-gray-500">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 text-xs text-gray-300 space-y-3">
          <div>
            <div className="font-semibold text-gray-200 mb-1">Entry — ${e?.toFixed(2)}</div>
            <div className="text-gray-400">
              Latest close on the {signal.timeframe} timeframe. For a {isBuy ? 'BUY' : 'SELL'} we enter at the current price because the Composite score has already confirmed the direction (MA alignment + RSI + MACD + ADX + volume + S/R break). Waiting for a pullback risks missing the move.
            </div>
          </div>
          <div>
            <div className="font-semibold text-gray-200 mb-1">Stop Loss — ${sl?.toFixed(2)} (risk ≈ ${risk?.toFixed(2)})</div>
            <div className="text-gray-400">
              {isBuy
                ? 'Placed just below whichever of these is tightest while still giving room to breathe: the nearest demand zone (institutional buy base), the nearest Fibonacci support (retracement of the recent up-leg), the nearest swing-low, or 1.5× ATR. Stop floors are pulled 0.3% under each level so a wick into it does not stop the trade out.'
                : 'Placed just above whichever of these is tightest: the nearest supply zone (institutional sell base), the nearest Fibonacci resistance (retracement of the recent down-leg), the nearest swing-high, or 1.5× ATR. Stop ceilings are pushed 0.3% above each level for the same wick-tolerance reason.'}
            </div>
          </div>
          <div>
            <div className="font-semibold text-gray-200 mb-1">Targets — chosen from levels {isBuy ? 'above' : 'below'} entry</div>
            <div className="text-gray-400 mb-2">
              The algorithm collects all candidate levels {isBuy ? 'above' : 'below'} the entry price: fresh {isBuy ? 'supply zones (institutional sell bases)' : 'demand zones (institutional buy bases)'}, Fibonacci retracements + extensions (127.2 / 161.8 / 200 / 261.8% projections of the recent swing leg), classical pivot points (R1/R2/R3 {isBuy ? '' : 'or S1/S2/S3'}) from the prior period's H/L/C, and the nearest swing-{isBuy ? 'high resistance' : 'low support'}. Anything on the wrong side of entry is discarded. If fewer than three valid levels remain, it falls back to R-multiple targets at 1.5×, 2.5×, and 4× risk — so T1 &lt; T2 &lt; T3 is always guaranteed. The chart shows demand/supply bands (green/red), Fibonacci retracements (yellow dotted, golden ratios bolder) and extensions (purple dotted).
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="bg-gray-950/50 rounded p-2">
                <div className="text-emerald-400 font-semibold">T1 ${t1?.toFixed(2)}</div>
                <div className="text-[10px] text-gray-500">{mult(t1)}× risk · first profit-taking / trailing level</div>
              </div>
              <div className="bg-gray-950/50 rounded p-2">
                <div className="text-emerald-400 font-semibold">T2 ${t2?.toFixed(2)}</div>
                <div className="text-[10px] text-gray-500">{mult(t2)}× risk · measured move</div>
              </div>
              <div className="bg-gray-950/50 rounded p-2">
                <div className="text-emerald-400 font-semibold">T3 ${t3?.toFixed(2)}</div>
                <div className="text-[10px] text-gray-500">{mult(t3)}× risk · runner / extension</div>
              </div>
            </div>
          </div>
          <div className="text-[11px] text-gray-500 pt-2 border-t border-gray-800">
            Backtest the Composite strategy (or a specific one from the panel below) to see how these rules have performed historically for this ticker before risking capital.
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Timeframe Alignment ----------
function TimeframeAlignment({ alignment, signals }) {
  const getColor = (type) => {
    if (type === 'BUY') return 'bg-emerald-600/30 text-emerald-400';
    if (type === 'SELL') return 'bg-red-600/30 text-red-400';
    return 'bg-gray-700/30 text-gray-400';
  };
  const byTf = {};
  (signals || []).forEach(s => { byTf[s.timeframe] = s; });
  const strategy = (signals && signals[0] && signals[0].strategy) || 'Composite (multi-factor)';
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Multi-Timeframe Alignment</h3>
        <span className="text-[10px] px-2 py-0.5 rounded bg-indigo-900/40 border border-indigo-700 text-indigo-300 uppercase tracking-wider">
          Strategy: {strategy}
        </span>
      </div>
      <div className="grid grid-cols-4 sm:grid-cols-7 gap-2">
        {TIMEFRAMES.map(tf => {
          const sig = alignment[tf] || 'NEUTRAL';
          const s = byTf[tf];
          const tip = s ? `${s.strategy || 'Composite'} · ${sig} · ${s.confidence}%` : sig;
          return (
            <div key={tf} title={tip} className={`rounded p-2 text-center text-xs ${getColor(sig)}`}>
              <div className="font-semibold uppercase text-[10px]">{tf}</div>
              <div className="text-xs font-semibold mt-0.5">{sig}</div>
            </div>
          );
        })}
      </div>
      <div className="text-[11px] text-gray-500 mt-2">
        All per-timeframe signals use the Composite rule-based strategy. Use the Backtest panel to see which dedicated strategy performs best historically for this stock.
      </div>
    </div>
  );
}

// ---------- Backtest Panel (multi-strategy) ----------
function BacktestPanel({ ticker }) {
  const [data, setData] = useState(null);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const chartRef = useRef(null);
  const chartInstance = useRef(null);

  const runBacktest = useCallback(async () => {
    if (!ticker) return;
    setLoading(true); setError(null);
    try {
      const result = await api.post(`/api/backtest/${ticker}`);
      setData(result);
      setSelectedIdx(0);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [ticker]);

  useEffect(() => { setData(null); }, [ticker]);

  const selected = data && data.results && data.results[selectedIdx];

  useEffect(() => {
    if (!selected || !chartRef.current) return;
    if (chartInstance.current) { chartInstance.current.remove(); chartInstance.current = null; }
    const chart = LightweightCharts.createChart(chartRef.current, {
      width: chartRef.current.clientWidth,
      height: 200,
      ...chartThemeOptions(),
      timeScale: { ...chartThemeOptions().timeScale, timeVisible: false },
    });
    chartInstance.current = chart;
    const line = chart.addAreaSeries({
      topColor: 'rgba(59, 130, 246, 0.3)', bottomColor: 'rgba(59, 130, 246, 0.05)',
      lineColor: '#3b82f6', lineWidth: 2,
    });
    line.setData(selected.equity_curve || []);
    chart.timeScale().fitContent();
  }, [selected]);

  const confColor = (c) => c >= 60 ? 'text-emerald-400' : c >= 40 ? 'text-yellow-400' : 'text-red-400';

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-400">Multi-Strategy Backtest</h3>
        <button onClick={runBacktest} disabled={loading} className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 px-3 py-1 rounded text-sm">
          {loading ? 'Running…' : 'Evaluate All Strategies (2yr)'}
        </button>
      </div>
      {error && <div className="text-sm text-red-400 mb-2">Error: {error}</div>}
      {!data && !loading && (
        <div className="text-sm text-gray-500">
          Click "Evaluate All Strategies" to backtest every strategy on 2 years of daily data and rank them by historical performance on this specific stock.
        </div>
      )}
      {data && data.results && data.results.length > 0 && (
        <div>
          <div className="mb-3 p-2 bg-emerald-900/30 border border-emerald-700 rounded">
            <div className="text-xs text-emerald-300 uppercase tracking-wider">Best Strategy for {data.ticker}</div>
            <div className="text-white font-semibold text-sm">
              {data.best_strategy} · {data.best_direction} · <span className={confColor(data.best_confidence)}>{data.best_confidence}% confidence</span>
            </div>
          </div>
          <div className="mb-3 overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-1 px-2">Strategy</th>
                  <th className="text-left py-1 px-2">Dir</th>
                  <th className="text-right py-1 px-2">Confidence</th>
                  <th className="text-right py-1 px-2">Trades</th>
                  <th className="text-right py-1 px-2">Win %</th>
                  <th className="text-right py-1 px-2">Return</th>
                  <th className="text-right py-1 px-2">PF</th>
                  <th className="text-right py-1 px-2">Sharpe</th>
                  <th className="text-right py-1 px-2">Max DD</th>
                </tr>
              </thead>
              <tbody>
                {data.results.map((r, i) => (
                  <tr key={i}
                      onClick={() => setSelectedIdx(i)}
                      className={`border-b border-gray-900 cursor-pointer hover:bg-gray-800 ${i === selectedIdx ? 'bg-gray-800' : ''}`}>
                    <td className="py-1 px-2 text-white">{r.strategy}</td>
                    <td className={`py-1 px-2 font-semibold ${r.direction === 'BUY' ? 'text-emerald-400' : 'text-red-400'}`}>{r.direction}</td>
                    <td className={`py-1 px-2 text-right font-semibold ${confColor(r.confidence)}`}>{r.confidence}%</td>
                    <td className="py-1 px-2 text-right text-gray-300">{r.stats.total_trades}</td>
                    <td className="py-1 px-2 text-right text-gray-300">{r.stats.win_rate}%</td>
                    <td className={`py-1 px-2 text-right ${r.stats.total_return_pct > 0 ? 'text-emerald-400' : 'text-red-400'}`}>{r.stats.total_return_pct > 0 ? '+' : ''}{r.stats.total_return_pct}%</td>
                    <td className="py-1 px-2 text-right text-gray-300">{r.stats.profit_factor}</td>
                    <td className="py-1 px-2 text-right text-gray-300">{r.stats.sharpe_ratio}</td>
                    <td className="py-1 px-2 text-right text-red-400">{r.stats.max_drawdown_pct}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {selected && (
            <div>
              <div className="text-xs text-gray-400 mb-2">
                Equity curve: <span className="text-white font-semibold">{selected.strategy} ({selected.direction})</span> — {selected.description}
              </div>
              <div ref={chartRef} className="w-full" />
            </div>
          )}
        </div>
      )}
      {data && data.results && data.results.length === 0 && (
        <div className="text-sm text-gray-500">No strategy produced any trades on this ticker's 2-year history.</div>
      )}
    </div>
  );
}

// Reusable collapsible section with a scrollable body frame. Used for
// Auto-Trades / Positions / Orders so long lists don't push the panel
// to a multi-screen height. Collapsed-by-default is the common case.
function CollapsibleSection({
  title,
  count,
  subtitle,
  children,
  defaultOpen = false,
  maxHeight = 440,
  actions,
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="surface-soft rounded-xl overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-3 py-2.5 text-left hover:bg-white/3"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs uppercase tracking-wider app-text-secondary font-semibold">{title}</span>
          {typeof count === 'number' && (
            <span className="pill text-[10px] font-mono">{count}</span>
          )}
          {subtitle && <span className="text-[11px] app-text-muted">{subtitle}</span>}
        </div>
        <div className="flex items-center gap-2">
          {actions && <div onClick={e => e.stopPropagation()}>{actions}</div>}
          <span className="app-text-muted text-xs">{open ? '▾' : '▸'}</span>
        </div>
      </button>
      {open && (
        <div
          className="border-t app-border-soft overflow-y-auto scrollbar-thin"
          style={{ maxHeight: `${maxHeight}px` }}
        >
          <div className="p-3">{children}</div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, positive, negative, hint }) {
  const color = positive ? 'text-emerald-400' : negative ? 'text-red-400' : 'app-text-primary';
  return (
    <div className="stat-card">
      <div className="text-[10px] uppercase tracking-[0.14em] app-text-muted">{label}</div>
      <div className={`font-mono font-semibold text-lg mt-0.5 ${color}`}>{value}</div>
      {hint && <div className="text-[10px] app-text-muted mt-0.5">{hint}</div>}
    </div>
  );
}

// ---------- News helpers ----------
function SentimentPill({ label, score, severity }) {
  const cls =
    label === 'positive' ? 'pill-success' :
    label === 'negative' ? 'pill-danger' :
    '';
  const fmt = (score || score === 0) ? (score >= 0 ? `+${score.toFixed(2)}` : score.toFixed(2)) : '';
  return (
    <span className={`pill ${cls}`} title={`Sentiment ${fmt} · severity ${severity ?? '?'}`}>
      {label || 'neutral'} {fmt && <span className="opacity-70 font-mono">{fmt}</span>}
    </span>
  );
}

function relativeTime(isoStr) {
  if (!isoStr) return '';
  // r42 fix #2.9: parse via parseServerDate so naive ISO strings (no Z)
  // are treated as UTC, not local. Previously a naive UTC timestamp was
  // parsed as local time → "X minutes ago" was wrong by hours-of-offset.
  const d = parseServerDate(isoStr);
  if (!d) return '';
  const diffMs = Date.now() - d.getTime();
  const mins = Math.max(1, Math.round(diffMs / 60000));
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}

// ---------- News Panel ----------
function NewsPanel({ ticker }) {
  const [items, setItems] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!ticker) return;
    setLoading(true); setErr(null);
    api.get(`/api/news/${ticker}?limit=20&hours=168`)
      .then(d => setItems(d || []))
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [ticker]);

  const shown = expanded ? (items || []) : (items || []).slice(0, 6);

  return (
    <div className="surface rounded-2xl p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-base font-bold flex items-center gap-2">
          <span>📰</span>
          <span>News</span>
          <span className="text-xs app-text-muted font-normal">— VADER sentiment · Alpaca feed</span>
        </h3>
        {items && items.length > 0 && (
          <div className="text-[11px] app-text-muted">{items.length} article{items.length !== 1 ? 's' : ''} · last 7d</div>
        )}
      </div>
      {loading && <div className="text-xs app-text-muted italic">Loading…</div>}
      {err && <div className="text-xs text-red-400">Error: {err}</div>}
      {items && items.length === 0 && !loading && (
        <div className="text-xs app-text-muted italic">
          No news indexed for {ticker} yet. News ingestion runs every 2 minutes — check back later.
        </div>
      )}
      {items && items.length > 0 && (
        <div className="space-y-2">
          {shown.map(it => (
            <a
              key={it.id}
              href={safeHref(it.url)}
              target="_blank"
              rel="noopener noreferrer"
              className="block surface-soft rounded-xl p-3 lift hover:bg-white/3"
            >
              <div className="flex items-start justify-between gap-3 mb-1">
                <div className="flex-1 min-w-0 font-semibold text-sm leading-snug">{it.headline}</div>
                <SentimentPill label={it.sentiment_label} score={it.sentiment_score} severity={it.severity} />
              </div>
              {it.summary && <div className="text-xs app-text-secondary line-clamp-2 mb-1.5">{it.summary.slice(0, 220)}{it.summary.length > 220 ? '…' : ''}</div>}
              <div className="text-[10px] app-text-muted font-mono flex items-center gap-2 flex-wrap">
                <span>{it.source || 'Unknown source'}</span>
                {it.author && <><span>·</span><span>{it.author}</span></>}
                <span>·</span>
                <span>{relativeTime(it.published_at)}</span>
                {it.symbols && it.symbols.length > 1 && <><span>·</span><span>+{it.symbols.length - 1} more</span></>}
              </div>
            </a>
          ))}
          {items.length > 6 && (
            <button
              onClick={() => setExpanded(e => !e)}
              className="text-xs app-text-secondary hover:app-text-primary px-3 py-1 rounded-md hover:bg-white/5"
            >
              {expanded ? 'Show less' : `Show ${items.length - 6} more`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- Trade-vs-News Context (inline explainer on a closed auto-trade) ----------
function TradeRationale({ tradeId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!tradeId) return;
    setLoading(true); setErr(null);
    api.get(`/api/trading/auto/rationale/${tradeId}`)
      .then(d => setData(d))
      .catch(e => setErr(typeof e === 'string' ? e : (e.detail || 'failed to load')))
      .finally(() => setLoading(false));
  }, [tradeId]);

  if (loading) return <div className="text-xs app-text-muted py-1">loading rationale…</div>;
  if (err) return <div className="text-xs text-red-400">Error: {err}</div>;
  if (!data) return null;

  const fmtPct = v => v == null ? '—' : `${(v * 100).toFixed(1)}%`;
  const fmtNum = (v, d = 2) => v == null ? '—' : Number(v).toFixed(d);
  const originLabel = {
    'watchlist': '🎯 Watchlist',
    'scanner': '🔭 Scanner-discovered',
    'watchlist+pool': '🎯 Watchlist + Scanner',
    'unknown': '? Unknown origin',
  }[data.origin] || data.origin;

  const Section = ({ title, children, accent = 'blue' }) => (
    <div className={`mt-2 p-2 rounded-md bg-${accent}-500/5 border border-${accent}-500/20`}>
      <div className="text-[10px] uppercase tracking-wider app-text-muted mb-1 font-semibold">{title}</div>
      {children}
    </div>
  );

  return (
    <div className="text-xs space-y-1">
      {/* Origin */}
      <div className="flex items-center gap-2 mb-1">
        <span className="font-bold">{originLabel}</span>
        {data.signal?.timeframe && <span className="pill text-[10px]">{data.signal.timeframe}</span>}
        {data.signal?.signal_type && <span className={`pill text-[10px] ${data.signal.signal_type === 'BUY' ? 'pill-success' : 'pill-danger'}`}>{data.signal.signal_type}</span>}
      </div>

      {/* Scanner snapshot */}
      {data.scanner && (
        <Section title="Why scanner picked it" accent="indigo">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1 font-mono text-[11px]">
            <div>score: <span className="font-bold">{fmtNum(data.scanner.score, 1)}</span></div>
            <div>RVOL: {fmtNum(data.scanner.rvol)}</div>
            <div>RS 20d: {fmtPct(data.scanner.rs_20d)}</div>
            <div>RS 60d: {fmtPct(data.scanner.rs_60d)}</div>
            <div>ADX: {fmtNum(data.scanner.adx, 1)}</div>
            <div>52w-hi: {fmtPct(data.scanner.pct_from_52w_high)}</div>
            <div className="col-span-2">price: ${fmtNum(data.scanner.price)}</div>
          </div>
          {data.scanner.reason && <div className="mt-1 italic app-text-secondary">{data.scanner.reason}</div>}
        </Section>
      )}

      {/* Signal reasoning */}
      {data.signal && (
        <Section title={`Signal — confidence ${fmtNum(data.signal.confidence, 0)}%${data.signal.strategy ? ' · ' + data.signal.strategy : ''}`} accent="emerald">
          {data.signal.reasoning_lines?.length > 0 ? (
            <ul className="space-y-0.5 leading-snug">
              {data.signal.reasoning_lines.map((ln, i) => (
                <li key={i} className="font-mono text-[11px]">{ln}</li>
              ))}
            </ul>
          ) : (
            <div className="italic app-text-muted">no reasoning recorded</div>
          )}
        </Section>
      )}

      {/* Backtest */}
      {data.backtest && (
        <Section title="Best strategy on this ticker (walk-forward)" accent="blue">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1 font-mono text-[11px]">
            <div className="col-span-2">{data.backtest.winning_strategy} <span className="app-text-muted">({data.backtest.winning_direction})</span></div>
            <div>conf: {fmtNum(data.backtest.confidence, 0)}<span className="app-text-muted">%</span></div>
            <div>OOS trades: {data.backtest.oos_trades ?? '—'}</div>
            <div>win rate: {fmtPct(data.backtest.win_rate)}</div>
            <div>avg P/L: {data.backtest.avg_pl == null ? '—' : `${data.backtest.avg_pl >= 0 ? '+' : ''}${data.backtest.avg_pl.toFixed(1)}%`}</div>
          </div>
        </Section>
      )}

      {/* Fundamentals */}
      {data.fundamentals && (
        <Section title={`Fundamentals — quality score ${fmtNum(data.fundamentals.quality_score, 0)}`} accent="purple">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1 font-mono text-[11px]">
            <div>PE: {fmtNum(data.fundamentals.pe_ratio, 1)}</div>
            <div>PEG: {fmtNum(data.fundamentals.peg_ratio)}</div>
            <div>rev YoY: {fmtPct(data.fundamentals.revenue_growth_yoy)}</div>
            <div>EPS YoY: {fmtPct(data.fundamentals.earnings_growth_yoy)}</div>
            <div>profit M: {fmtPct(data.fundamentals.profit_margin)}</div>
            <div>ROE: {fmtPct(data.fundamentals.return_on_equity)}</div>
            <div>D/E: {fmtNum(data.fundamentals.debt_to_equity, 1)}</div>
            <div>current: {fmtNum(data.fundamentals.current_ratio, 1)}</div>
          </div>
          {data.fundamentals.sector && (
            <div className="mt-1 text-[10px] app-text-muted">{data.fundamentals.sector} · {data.fundamentals.industry}</div>
          )}
        </Section>
      )}

      {/* Analyst */}
      {data.analyst && (
        <Section title={`Wall Street — ${data.analyst.key || 'n/a'} (${data.analyst.analyst_count || 0} analysts)`} accent="amber">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1 font-mono text-[11px]">
            <div>mean: {fmtNum(data.analyst.mean)}</div>
            <div>target: ${fmtNum(data.analyst.target_mean)}</div>
            <div>vs entry: {fmtPct(data.analyst.target_premium_vs_entry)}</div>
            <div>range: ${fmtNum(data.analyst.target_low)} – ${fmtNum(data.analyst.target_high)}</div>
          </div>
        </Section>
      )}

      {/* Macro */}
      {data.macro_context?.length > 0 && (
        <Section title="Macro context (±48h)" accent="rose">
          <div className="space-y-0.5 font-mono text-[11px]">
            {data.macro_context.map((ev, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className={`pill text-[9px] ${ev.importance === 'high' ? 'pill-danger' : 'pill-warn'}`}>{ev.importance}</span>
                <span className="font-bold">{ev.event_key}</span>
                <span className="app-text-muted">@ {ev.release_time_utc?.slice(0,16).replace('T',' ')}</span>
                <span className={ev.minutes_relative_to_open >= 0 ? 'text-blue-400' : 'app-text-secondary'}>
                  ({ev.minutes_relative_to_open >= 0 ? '+' : ''}{ev.minutes_relative_to_open}m vs open)
                </span>
                {ev.actual != null && <span>actual {ev.actual}</span>}
              </div>
            ))}
          </div>
        </Section>
      )}

      {!data.signal && !data.scanner && !data.backtest && !data.fundamentals && !data.analyst && !data.macro_context?.length && (
        <div className="italic app-text-muted text-[11px]">No detailed rationale recorded for this trade. (Older trades pre-date this feature.)</div>
      )}
    </div>
  );
}

function TradeNewsContext({ tradeId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!tradeId) return;
    setLoading(true); setErr(null);
    api.get(`/api/news/trade/${tradeId}/context`)
      .then(d => setData(d))
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [tradeId]);

  if (loading) return <div className="text-xs app-text-muted">Loading news context…</div>;
  if (err) return <div className="text-xs text-red-400">Error: {err}</div>;
  if (!data) return null;

  const counts = data.news_counts || {};
  const verdict = data.verdict || '';
  const vColor = verdict === 'aligned' ? 'pill-success' : verdict === 'contrary' ? 'pill-danger' : '';

  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="pill">Avg sentiment (during): <span className="font-mono ml-1">{data.avg_sentiment_during?.toFixed(3) ?? '—'}</span></span>
        <span className="pill">Pre: {counts.pre || 0}</span>
        <span className="pill">During: {counts.during || 0}</span>
        <span className="pill">Post: {counts.post || 0}</span>
        {verdict && <span className={`pill ${vColor}`}>{verdict}</span>}
      </div>
      {['pre_trade', 'during_trade', 'post_trade'].map(bucket => {
        const list = data.articles?.[bucket] || [];
        if (list.length === 0) return null;
        const label = bucket === 'pre_trade' ? 'Before entry' : bucket === 'during_trade' ? 'During trade' : 'After close';
        return (
          <div key={bucket} className="surface-soft rounded-lg p-2">
            <div className="text-[10px] uppercase tracking-wider app-text-muted font-semibold mb-1.5">{label} ({list.length})</div>
            <div className="space-y-1.5">
              {list.slice(0, 5).map(a => (
                <a key={a.id} href={safeHref(a.url)} target="_blank" rel="noopener noreferrer"
                   className="block hover:bg-white/5 rounded px-1 py-0.5">
                  <div className="flex items-start gap-2">
                    <SentimentPill label={a.sentiment_label} score={a.sentiment_score} severity={a.severity} />
                    <div className="flex-1 min-w-0">
                      <div className="app-text-primary leading-snug truncate">{a.headline}</div>
                      <div className="text-[10px] app-text-muted font-mono">{a.source} · {relativeTime(a.published_at)}</div>
                    </div>
                  </div>
                </a>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------- News vs Trades Summary (week-later review) ----------
function NewsAnalysisSummary() {
  const [days, setDays] = useState(7);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback((d) => {
    setLoading(true); setErr(null);
    api.get(`/api/news/analysis/summary?days=${d}`)
      .then(x => setData(x))
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(days); }, [days, load]);

  return (
    <div className="surface rounded-2xl p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-base font-bold flex items-center gap-2">
          <span>📊</span><span>News ↔ Trade Alignment</span>
        </h3>
        <div className="flex items-center gap-2">
          {[3, 7, 14, 30].map(n => (
            <button
              key={n}
              onClick={() => setDays(n)}
              className={`text-xs px-2 py-1 rounded-md ${days === n ? 'bg-blue-600 text-white' : 'surface-soft app-text-secondary'}`}
            >
              {n}d
            </button>
          ))}
        </div>
      </div>
      {loading && <div className="text-xs app-text-muted italic">Loading…</div>}
      {err && <div className="text-xs text-red-400">Error: {err}</div>}
      {data && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <Stat label="Closed trades" value={data.total_trades} />
            <Stat
              label="Alignment rate"
              value={data.alignment_rate_pct != null ? `${data.alignment_rate_pct}%` : '—'}
              hint="news direction ↔ trade outcome"
            />
            <Stat label="Win w/ +news" value={(data.matrix?.positive_sent?.win ?? 0)} />
            <Stat label="Loss w/ −news" value={(data.matrix?.negative_sent?.loss ?? 0)} />
          </div>
          {data.matrix && (
            <div className="surface-soft rounded-xl overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="app-text-muted border-b app-border">
                    <th className="text-left py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Sentiment during trade</th>
                    <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Wins</th>
                    <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Losses</th>
                    <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Flat</th>
                  </tr>
                </thead>
                <tbody>
                  {['positive_sent', 'negative_sent', 'neutral_sent', 'no_news'].map(b => (
                    <tr key={b} className="border-b app-border-soft last:border-0">
                      <td className="py-1.5 px-3 capitalize">{b.replace('_', ' ')}</td>
                      <td className="text-right py-1.5 px-3 font-mono text-emerald-400">{data.matrix[b]?.win ?? 0}</td>
                      <td className="text-right py-1.5 px-3 font-mono text-red-400">{data.matrix[b]?.loss ?? 0}</td>
                      <td className="text-right py-1.5 px-3 font-mono app-text-muted">{data.matrix[b]?.flat ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="text-[11px] app-text-muted leading-relaxed">
            Alignment rate = (positive-news wins + negative-news losses) / (all trades that had news). After a week of data, a meaningfully non-50% alignment rate is the signal that news sentiment is actually predictive — that's your cue to wire news into auto-trader gates in phase 2.
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Analysis View ----------
function AnalysisView({ ticker, reloadToken = 0, liveQuote = null, onAutoTradeChanged = null, theme = 'dark' }) {
  const [analysis, setAnalysis] = useState(null);
  const [timeframe, setTimeframe] = usePersistentState('analysisTF', '1d');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // r49: layered overlay toggles (replaces single hideIndicators).
  // Persisted; default-on for parity. "Clean preset" = all off.
  const [overlay, setOverlay] = usePersistentState('chartOverlay', {
    mas: true, bb: true, sr: true, zones: true, fibs: true, news: true,
  });
  // r53b fix: `hideIndicators` was retired in r49 in favor of layered
  // overlay toggles, but the read sites in StockChart stayed in place
  // AND the value was still being read from localStorage. Anyone whose
  // localStorage cached `hideIndicators: true` from a pre-r49 build had
  // ALL indicators hidden forever with no UI to flip it back. Bypass
  // the persistent read entirely and force it to false; the global
  // toggle is now a no-op preserved only to keep the StockChart prop
  // signature stable.
  const hideIndicators = false;
  const setHideIndicators = () => {};
  // Defensive: clear the stale localStorage key on first mount so it
  // never returns even after a hard refresh.
  React.useEffect(() => {
    ls.remove('hideIndicators');
  }, []);
  // r49: compact mode (smaller chart + collapsed defaults)
  const [compactMode, setCompactMode] = usePersistentState('analysisCompact', false);

  // r49: load my open position for this ticker — drives chart overlay + banner.
  const { data: myPositionsRaw } = useSWR(
    '/api/trading/positions',
    () => api.get('/api/trading/positions'),
    { intervalMs: 30000 }
  );
  const myPosition = useMemo(() => {
    if (!ticker || !myPositionsRaw) return null;
    return (myPositionsRaw || []).find(p => p.symbol === ticker) || null;
  }, [myPositionsRaw, ticker]);

  // r49: load news for this ticker to feed chart markers.
  const { data: newsData } = useSWR(
    ticker ? `/api/news/recent?ticker=${ticker}&limit=20` : null,
    () => ticker ? api.get(`/api/news/recent?ticker=${ticker}&limit=20`) : Promise.resolve({}),
    { intervalMs: 60000, enabled: !!ticker }
  );
  const newsEvents = useMemo(() => (newsData?.events || []).filter(n => (n.severity || 0) >= 35), [newsData]);

  // Track the ticker owned by the most-recent fetch. Stops a slow response
  // for ticker A from clobbering the UI after the user clicked ticker B.
  const inFlightTickerRef = useRef(null);

  const loadAnalysis = useCallback(async (refresh = false) => {
    if (!ticker) return;
    const requestedTicker = ticker;
    inFlightTickerRef.current = requestedTicker;
    setLoading(true); setError(null);
    try {
      const data = await api.get(`/api/analysis/${requestedTicker}${refresh ? '?refresh=true' : ''}`);
      // Drop if the user switched away while we were waiting — prevents the
      // "price of the previous ticker briefly appears" flash.
      if (inFlightTickerRef.current !== requestedTicker) return;
      setAnalysis(data);
    } catch (e) {
      if (inFlightTickerRef.current !== requestedTicker) return;
      setError(String(e));
    } finally {
      if (inFlightTickerRef.current === requestedTicker) setLoading(false);
    }
  }, [ticker]);

  // Clear stale data the INSTANT the ticker changes so the header / signal
  // cards show a skeleton instead of the previous ticker's price.
  useEffect(() => {
    setAnalysis(null);
    setError(null);
    loadAnalysis(false);
  }, [ticker, loadAnalysis, reloadToken]);

  if (!ticker) {
    return <div className="flex-1 flex items-center justify-center text-gray-500">Select a stock from the watchlist to see its analysis</div>;
  }

  const timeframeSignal = analysis?.signals?.find(s => s.timeframe === timeframe);

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      <div className="p-3 sm:p-4 border-b app-border flex items-center justify-between flex-wrap gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
            <h1 className="text-xl sm:text-2xl font-bold">{ticker}</h1>
            {analysis?.name && <span className="app-text-secondary text-sm hidden sm:inline">{analysis.name}</span>}
            {/* Price: prefer loaded analysis, fall back to live WS quote so
                there's always SOMETHING during the ticker-switch fetch. If
                neither is available, show a skeleton shimmer — never a stale
                value from the previously-selected ticker. */}
            {analysis?.current_price ? (
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-semibold">${analysis.current_price.toFixed(2)}</span>
                {analysis.change_pct != null && Number.isFinite(analysis.change_pct) ? (
                  <span className={`text-sm ${analysis.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {analysis.change_pct >= 0 ? '+' : ''}{analysis.change_pct.toFixed(2)}%
                  </span>
                ) : (
                  <span className="text-sm app-text-muted">—</span>
                )}
              </div>
            ) : liveQuote && (liveQuote.last || (liveQuote.bid && liveQuote.ask)) ? (
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-semibold">
                  ${(liveQuote.last || (liveQuote.bid + liveQuote.ask) / 2).toFixed(2)}
                </span>
                <span className="text-xs app-text-muted">live</span>
              </div>
            ) : (
              <div className="skel h-6 w-28" />
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
          <TickerAutoTradeToggle ticker={ticker} onChanged={onAutoTradeChanged} />
          <TimeframeSelector value={timeframe} onChange={setTimeframe} />
          <button onClick={() => loadAnalysis(true)} disabled={loading}
                  className="px-2.5 sm:px-3 py-1.5 rounded-lg text-xs sm:text-sm font-semibold border app-border surface-soft app-text-primary hover:bg-white/5 disabled:opacity-50">
            {loading ? '…' : '↻'}<span className="hidden sm:inline">&nbsp;{loading ? 'Refreshing' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      {error && <div role="alert" className="m-3 sm:m-4 p-3 bg-red-900/30 border border-red-800 rounded text-sm text-red-300">{friendlyError(error)}</div>}

      <div className="p-3 sm:p-4 space-y-3 sm:space-y-4">
        {/* r49: My-position banner — only when holding this ticker */}
        {myPosition && analysis?.current_price && (
          <MyPositionBanner ticker={ticker} position={myPosition} currentPrice={analysis.current_price} signal={analysis?.primary_signal} />
        )}

        <div className="surface rounded-xl overflow-hidden">
          <div className="px-3 py-2 flex items-center justify-between border-b app-border text-xs flex-wrap gap-2">
            <div className="app-text-muted uppercase tracking-widest">{timeframe} chart</div>
            {/* r49: layered overlay toggles + Clean preset + compact mode */}
            <div className="flex items-center gap-1 flex-wrap" role="group" aria-label="Chart overlays">
              {[
                { k: 'mas', label: 'MAs' },
                { k: 'bb', label: 'BB' },
                { k: 'sr', label: 'S/R' },
                { k: 'zones', label: 'Zones' },
                { k: 'fibs', label: 'Fibs' },
                { k: 'news', label: 'News' },
              ].map(t => (
                <button
                  key={t.k}
                  onClick={() => setOverlay(o => ({ ...o, [t.k]: !o[t.k] }))}
                  className={`text-[10px] px-1.5 py-0.5 rounded font-semibold uppercase tracking-wide ${
                    overlay[t.k]
                      ? 'bg-blue-500/20 border border-blue-500/40 text-blue-300'
                      : 'border app-border-soft app-text-muted'
                  }`}
                  aria-pressed={overlay[t.k]}
                >{t.label}</button>
              ))}
              <button
                onClick={() => setOverlay({ mas: false, bb: false, sr: false, zones: false, fibs: false, news: false })}
                className="text-[10px] px-1.5 py-0.5 rounded border app-border-soft app-text-muted"
                title="Clean preset — all overlays off"
              >Clean</button>
              <button
                onClick={() => setOverlay({ mas: true, bb: true, sr: true, zones: true, fibs: true, news: true })}
                className="text-[10px] px-1.5 py-0.5 rounded border app-border-soft app-text-muted"
                title="All overlays on"
              >All</button>
              <button
                onClick={() => setCompactMode(c => !c)}
                className={`text-[10px] px-1.5 py-0.5 rounded ${compactMode ? 'bg-amber-500/20 border border-amber-500/40 text-amber-300' : 'border app-border-soft app-text-muted'}`}
                aria-pressed={compactMode}
                title="Compact mode"
              >⊟</button>
            </div>
          </div>
          <StockChart
            ticker={ticker}
            timeframe={timeframe}
            liveQuote={liveQuote}
            theme={theme}
            hideIndicators={hideIndicators}
            showMAs={overlay?.mas ?? true}
            showBB={overlay?.bb ?? true}
            showSR={overlay?.sr ?? true}
            showZones={overlay?.zones ?? true}
            showFibs={overlay?.fibs ?? true}
            showNews={overlay?.news ?? true}
            signal={timeframeSignal || analysis?.primary_signal}
            position={myPosition}
            newsEvents={newsEvents}
            height={compactMode ? 280 : 460}
          />
        </div>

        {/* r49: TimeframeAlignment moves under chart but only when not compact */}
        {!compactMode && analysis?.timeframe_alignment && <TimeframeAlignment alignment={analysis.timeframe_alignment} signals={analysis.signals} />}

        {/* r49: dedup'd SignalCard — single card, internal TF tabs replace duplicate */}
        {timeframeSignal && (
          <SignalCard
            signal={timeframeSignal}
            currentPrice={analysis?.current_price}
            // pass primary signal as a hint so card can show "↗ Primary: 1d 75 BUY" pill
            primarySignal={analysis?.primary_signal}
          />
        )}

        {/* r49: collapsed-by-default for News/Options/Backtest. Compact mode forces collapsed. */}
        <CollapsibleSection title="News" defaultOpen={!compactMode && newsEvents.length > 0}>
          <NewsPanel ticker={ticker} />
        </CollapsibleSection>

        <CollapsibleSection title="Options" defaultOpen={false}>
          <OptionsPanel ticker={ticker} signal={analysis?.primary_signal} />
        </CollapsibleSection>

        <CollapsibleSection title="Backtest" defaultOpen={false}>
          <BacktestPanel ticker={ticker} />
        </CollapsibleSection>
      </div>
    </div>
  );
}

// ---------- Options Panel ----------
const OPTIONS_PAGE_SIZE = 10;

function OptionsPanel({ ticker, signal }) {
  // `side` overrides the natural BUY→calls / SELL→puts mapping. Tabs let the
  // user peek at the contrary side (e.g. hedge a long with a put) without
  // touching the underlying signal.
  const naturalSide = signal?.signal_type === 'SELL' ? 'puts' : 'calls';
  const [side, setSide] = useState(naturalSide);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [visible, setVisible] = useState(OPTIONS_PAGE_SIZE);

  // Tracks whether we've done at least one chain scan for this ticker. Tab
  // clicks auto-refetch only after the first manual scan, preserving the
  // "Find Contracts" gate so the slow initial scan stays opt-in.
  const fetchedOnceRef = useRef(false);

  // Pin to the primary signal's timeframe so contract direction matches what
  // the user sees. Without this the backend's longest-timeframe heuristic
  // can pick an older opposite-direction signal (e.g. stale 1mo BUY when
  // today's 4h is a SELL), surfacing calls under a SELL header.
  const pinnedTf = signal?.timeframe;
  const load = useCallback(async () => {
    if (!ticker) return;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (pinnedTf) params.set('timeframe', pinnedTf);
      params.set('side', side);
      const d = await api.get(`/api/options/${ticker}?${params.toString()}`);
      setData(d);
      setVisible(OPTIONS_PAGE_SIZE);  // reset paging on every fresh fetch
      fetchedOnceRef.current = true;  // future tab clicks now auto-refetch
    } catch (e) {
      setError(e.message || 'Failed to load options');
    } finally {
      setLoading(false);
    }
  }, [ticker, pinnedTf, side]);

  // Reset everything when the ticker changes; reset to natural side too so the
  // panel doesn't carry a "puts" tab into a fresh BUY-signal stock.
  useEffect(() => {
    setData(null);
    setSide(naturalSide);
    setVisible(OPTIONS_PAGE_SIZE);
    fetchedOnceRef.current = false;
  }, [ticker]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-refetch when the user flips Calls↔Puts AFTER the first manual scan,
  // or when the primary signal's timeframe changes (e.g. live recompute
  // promotes a different TF to primary). Audit fix M7: previously this
  // effect's deps were [side] only, so a tf change while sitting on the
  // same side reused stale `pinnedTf` captured by the first `load`.
  useEffect(() => {
    if (fetchedOnceRef.current) load();
  }, [side, pinnedTf, load]);

  const direction = signal?.signal_type;
  const conf = signal?.confidence;
  const highConf = conf != null && conf >= 70;
  const total = data?.total ?? data?.contracts?.length ?? 0;
  const shown = Math.min(visible, data?.contracts?.length ?? 0);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-lg font-bold">Options Plays</h3>
          {/* Calls/Puts tab strip — `naturalSide` gets a tiny dot to remind which one matches the signal */}
          <div className="inline-flex rounded border border-gray-700 overflow-hidden text-xs">
            {['calls', 'puts'].map((s) => (
              <button
                key={s}
                onClick={() => setSide(s)}
                className={`px-3 py-1 transition-colors ${
                  side === s
                    ? (s === 'calls' ? 'bg-emerald-700/40 text-emerald-200' : 'bg-red-700/40 text-red-200')
                    : 'bg-gray-800/60 text-gray-400 hover:bg-gray-800'
                }`}
                title={s === naturalSide ? `Matches the ${direction || ''} signal direction` : 'Contrary side'}
              >
                {s.toUpperCase()}{s === naturalSide && <span className="ml-1 text-[8px] align-top">●</span>}
              </button>
            ))}
          </div>
          <span className="text-xs text-gray-500">R:R ≥ 3:1</span>
        </div>
        <button
          onClick={load}
          disabled={loading || !highConf}
          className="bg-indigo-700 hover:bg-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed px-3 py-1 rounded text-sm"
          title={!highConf ? 'Needs signal confidence ≥ 70%' : ''}
        >
          {loading ? 'Scanning chain…' : data ? 'Re-scan' : 'Find Contracts'}
        </button>
      </div>

      {!highConf && (
        <div className="text-xs text-gray-400">
          Signal confidence is {conf != null ? `${conf.toFixed(0)}%` : 'unavailable'}. Options suggestions only appear for high-conviction setups (≥70%).
        </div>
      )}

      {error && <div className="text-xs text-red-400 mt-2">{error}</div>}

      {data && data.contracts?.length === 0 && (
        <div className="text-xs text-yellow-300 mt-2">{data.note || 'No contracts met the R:R ≥ 3:1 + liquidity filters.'}</div>
      )}

      {data && data.contracts?.length > 0 && (
        <>
          <div className="text-[11px] text-gray-500 mb-2 flex items-center justify-between gap-2">
            <span>{data.note}</span>
            <span className="text-gray-600">
              Showing <span className="text-gray-300">{shown}</span> of {data.contracts.length}
              {data.total != null && data.total > data.contracts.length && (
                <> (top {data.contracts.length} of {data.total} qualified)</>
              )}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-400 text-left border-b border-gray-800">
                  <th className="py-1 pr-2">Contract</th>
                  <th className="py-1 pr-2">Strike</th>
                  <th className="py-1 pr-2">Exp · DTE</th>
                  <th className="py-1 pr-2">Premium</th>
                  <th className="py-1 pr-2">Breakeven</th>
                  <th className="py-1 pr-2" title="Exit contract at this premium level. Uses whichever is tighter: 50% premium decay OR underlying hitting its stop.">Stop</th>
                  <th className="py-1 pr-2" title="Dollar loss per contract if stop is honoured">Loss @ Stop</th>
                  <th className="py-1 pr-2">R:R T1</th>
                  <th className="py-1 pr-2">R:R T2</th>
                  <th className="py-1 pr-2">R:R T3</th>
                  <th className="py-1 pr-2">Vol/OI</th>
                  <th className="py-1 pr-2">IV</th>
                  <th className="py-1 pr-2">Δ~</th>
                  <th className="py-1 pr-2">Score</th>
                </tr>
              </thead>
              <tbody>
                {data.contracts.slice(0, visible).map((c, i) => (
                  <tr key={c.symbol || i} className="border-b border-gray-800/60 hover:bg-gray-800/30">
                    <td className="py-1.5 pr-2">
                      <span className={`font-semibold ${c.type === 'CALL' ? 'text-emerald-400' : 'text-red-400'}`}>{c.type}</span>
                      {c.in_the_money && <span className="ml-1 text-[10px] text-indigo-300">ITM</span>}
                    </td>
                    <td className="py-1.5 pr-2">${c.strike}</td>
                    <td className="py-1.5 pr-2 text-gray-400">
                      {c.expiration} · {c.dte}d
                      {c.is_weekly && <span className="ml-1 text-[9px] px-1 py-0.5 rounded bg-amber-800/50 border border-amber-700 text-amber-300 uppercase">WKLY</span>}
                    </td>
                    <td className="py-1.5 pr-2">${c.premium}</td>
                    <td className="py-1.5 pr-2">${c.breakeven}</td>
                    <td className="py-1.5 pr-2 text-red-400" title={`Premium stop $${c.premium_stop} (50% decay) · Underlying stop $${c.underlying_stop} → est. premium $${c.est_premium_at_underlying_stop}`}>${c.effective_stop_premium}</td>
                    <td className="py-1.5 pr-2 text-red-400">${c.effective_max_loss}</td>
                    <td className="py-1.5 pr-2 text-emerald-400" title={`Managed R:R (using stop): ${c.rr_t1_managed}:1`}>{c.rr_t1}:1</td>
                    <td className="py-1.5 pr-2 text-emerald-400" title={`Managed R:R: ${c.rr_t2_managed}:1`}>{c.rr_t2}:1</td>
                    <td className="py-1.5 pr-2 text-emerald-400" title={`Managed R:R: ${c.rr_t3_managed}:1`}>{c.rr_t3}:1</td>
                    <td className="py-1.5 pr-2 text-gray-400">{c.volume}/{c.open_interest}</td>
                    <td className="py-1.5 pr-2 text-gray-400">{c.iv}%</td>
                    <td className="py-1.5 pr-2 text-gray-400">{c.delta_estimate}</td>
                    <td className="py-1.5 pr-2 font-semibold">{c.score}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {visible < data.contracts.length && (
            <div className="flex items-center justify-center gap-3 mt-3">
              <button
                onClick={() => setVisible((v) => Math.min(v + OPTIONS_PAGE_SIZE, data.contracts.length))}
                className="bg-gray-800 hover:bg-gray-700 text-gray-200 px-3 py-1 rounded text-xs"
              >
                Show {Math.min(OPTIONS_PAGE_SIZE, data.contracts.length - visible)} more
              </button>
              <button
                onClick={() => setVisible(data.contracts.length)}
                className="text-xs text-gray-400 hover:text-gray-200 underline underline-offset-2"
              >
                Show all {data.contracts.length}
              </button>
            </div>
          )}
          <div className="text-[11px] text-gray-500 mt-3">
            <strong>Reward</strong> = intrinsic value at each underlying target − premium paid. <strong>Stop</strong> = exit premium level, set to whichever triggers first: (a) 50% premium decay, or (b) underlying hitting the stock signal's stop (delta-projected). <strong>Loss @ Stop</strong> = dollar loss per contract if you honour the stop; absolute max loss is still premium × 100. Hover R:R cells to see managed R:R (reward ÷ stopped loss). Delta and IV are approximations — verify on your broker before trading.
          </div>
        </>
      )}
    </div>
  );
}

// ---------- Trade-from-signal: pre-fills entry/SL/TP into a bracket order ----------
function TradeFromSignal({ signal }) {
  const [open, setOpen] = useState(false);
  const [qty, setQty] = useState(10);
  const [target, setTarget] = useState('target1');
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [err, setErr] = useState(null);
  // r42 fix #2.22: Escape closes the modal.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === 'Escape') { setOpen(false); setResult(null); setErr(null); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);

  const tp = signal[target];
  // r42 fix #1.15 + #2.17: validate qty + apply ×100 contract multiplier on
  // option signals. Audit fix M9: empty input → Number(qty)=NaN.
  const qtyNum = Number(qty);
  const qtyValid = Number.isFinite(qtyNum) && qtyNum >= 1 && qtyNum <= 10000;
  const safeQty = qtyValid ? qtyNum : 0;
  // Heuristic: if the signal carries an option asset_type or has an OCC-style
  // symbol, treat each unit as 100 shares. r42 fix #1.15.
  const isOption = signal.asset_type === 'option'
    || /^[A-Z]+\d{6}[CP]\d{8}$/.test(String(signal.symbol || ''));
  const contractMultiplier = isOption ? 100 : 1;
  const cost = (signal.entry * safeQty * contractMultiplier).toFixed(2);
  const risk = signal.stop_loss != null
    ? (Math.abs(signal.entry - signal.stop_loss) * safeQty * contractMultiplier).toFixed(2)
    : null;
  const reward = tp != null
    ? (Math.abs(tp - signal.entry) * safeQty * contractMultiplier).toFixed(2)
    : null;

  const submit = async () => {
    if (!qtyValid) { setErr('Quantity must be ≥ 1'); return; }
    setSubmitting(true); setErr(null); setResult(null);
    try {
      const res = await api.post('/api/trading/order', {
        symbol: signal.ticker,
        qty: qtyNum,
        side: signal.signal_type === 'BUY' ? 'buy' : 'sell',
        entry_type: 'market',
        take_profit: tp,
        stop_loss: signal.stop_loss,
        time_in_force: 'gtc',
      });
      setResult(res);
    } catch (e) {
      setErr(typeof e === 'string' ? e : (e.detail || JSON.stringify(e)));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mt-3">
      {!open ? (
        <button
          onClick={() => setOpen(true)}
          className={`px-3 py-1.5 text-xs rounded font-semibold ${signal.signal_type === 'BUY' ? 'bg-emerald-700 hover:bg-emerald-600' : 'bg-red-700 hover:bg-red-600'}`}
        >
          📝 Paper-trade this {signal.signal_type} signal
        </button>
      ) : (
        <div className="border border-gray-700 rounded p-3 bg-gray-950/50 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs text-gray-400 uppercase tracking-wider">Bracket order — paper account</div>
            <button onClick={() => { setOpen(false); setResult(null); setErr(null); }} className="text-xs text-gray-500 hover:text-gray-300">close</button>
          </div>
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div>
              <label className="text-gray-500 block mb-1">Quantity</label>
              <input
                type="number"
                min="1"
                max="10000"
                step="1"
                value={qty}
                onChange={e => setQty(e.target.value)}
                aria-label={`${isOption ? 'Contracts' : 'Shares'} to buy`}
                className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm" />
            </div>
            <div>
              <label className="text-gray-500 block mb-1">Take-profit</label>
              <select value={target} onChange={e => setTarget(e.target.value)}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm">
                <option value="target1">T1 ${signal.target1?.toFixed(2)}</option>
                <option value="target2">T2 ${signal.target2?.toFixed(2)}</option>
                <option value="target3">T3 ${signal.target3?.toFixed(2)}</option>
              </select>
            </div>
            <div>
              <label className="text-gray-500 block mb-1">Stop-loss</label>
              <div className="px-2 py-1 text-sm bg-gray-900 border border-gray-700 rounded">${signal.stop_loss?.toFixed(2)}</div>
            </div>
          </div>
          <div className="text-xs text-gray-400">
            Notional ≈ <span className="text-white">${cost}</span> ·
            Max risk ≈ <span className="text-red-400">${risk}</span> ·
            Max reward ≈ <span className="text-emerald-400">${reward}</span>
          </div>
          {err && <div className="text-xs text-red-400 bg-red-950/40 border border-red-800 rounded p-2">⚠ {err}</div>}
          {result && (
            <div className="text-xs text-emerald-300 bg-emerald-950/30 border border-emerald-800 rounded p-2">
              ✅ Order #{result.id?.slice(0, 8)} submitted ({result.status}). Check the Paper Account panel below for fills.
            </div>
          )}
          <button
            onClick={submit}
            disabled={submitting || !!result || !qtyValid}
            className="w-full py-2 text-sm font-semibold rounded bg-blue-700 hover:bg-blue-600 disabled:bg-gray-700 disabled:text-gray-500"
          >
            {submitting ? 'Submitting…' : result ? 'Submitted ✓' : !qtyValid ? 'Enter qty ≥ 1' : `Submit ${signal.signal_type === 'BUY' ? 'BUY' : 'SELL'} bracket order`}
          </button>
        </div>
      )}
    </div>
  );
}

// ---------- Universe Scanner / Candidate Pool Panel ----------
// Shows the current top-N tickers identified by the universe scanner.
// Auto-trader picks from this pool when `cfg.use_universe_scanner=true`.
// Collapsible, scrollable; manual "Rescan" button triggers the scheduler
// job on demand (takes 30-60s for ~500 tickers).
function CandidatePoolPanel() {
  const [rows, setRows] = useState(null);
  const [loading, setLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const d = await api.get('/api/trading/auto/candidate-pool?limit=50');
      setRows(d || []);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 60000);   // refresh every minute
    return () => clearInterval(iv);
  }, [load]);

  const rescan = useCallback(async () => {
    setScanning(true); setErr(null);
    try {
      await api.post('/api/trading/auto/universe-scan');
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setScanning(false);
    }
  }, [load]);

  const count = Array.isArray(rows) ? rows.length : 0;
  const header = (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-base font-bold">🎯 Candidate Pool</span>
        <span className="pill text-[10px]">{count}</span>
        {rows?.[0]?.generated_at && (
          <span className="text-[11px] app-text-muted font-mono">
            · updated {relativeTime(rows[0].generated_at)}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={(e) => { e.stopPropagation(); rescan(); }}
          disabled={scanning}
          className="text-[11px] px-2.5 py-1 rounded-md surface-soft border app-border hover:bg-white/5 disabled:opacity-50"
        >
          {scanning ? 'Scanning…' : '↻ Rescan'}
        </button>
      </div>
    </div>
  );

  return (
    <div className="surface rounded-2xl p-4 shadow-xl">
      <button onClick={() => setOpen(o => !o)} className="w-full text-left">
        {header}
      </button>
      <div className="text-[11px] app-text-muted leading-relaxed mt-1">
        Top {count} setups ranked across ~500 liquid US equities. Auto-trader picks from this pool when <code className="app-text-primary">use_universe_scanner</code> is enabled. Scans 4× per day at market-sensitive UTC slots (12:00, 14:30, 17:00, 19:30).
      </div>
      {open && (
        <div className="mt-3 overflow-x-auto">
          {loading && !rows && <div className="text-xs app-text-muted italic">Loading…</div>}
          {err && <div className="text-xs text-red-400">Error: {err}</div>}
          {Array.isArray(rows) && rows.length === 0 && !loading && (
            <div className="text-sm app-text-muted italic py-4 text-center">
              Pool is empty. Click <strong>Rescan</strong> above to seed it, or wait for the next scheduled scan.
            </div>
          )}
          {Array.isArray(rows) && rows.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="app-text-muted border-b app-border">
                  <th className="text-left py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">#</th>
                  <th className="text-left py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">Ticker</th>
                  <th className="text-right py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">Score</th>
                  <th className="text-right py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">Price</th>
                  <th className="text-right py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">RVOL</th>
                  <th className="text-right py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">RS 20d</th>
                  <th className="text-right py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">ADX</th>
                  <th className="text-right py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">% 52wH</th>
                  <th className="text-left py-2 px-2 font-semibold uppercase tracking-wider text-[10px]">Setup</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={r.ticker} className="border-b app-border-soft last:border-0 hover:bg-white/3">
                    <td className="py-1.5 px-2 app-text-muted font-mono">{i + 1}</td>
                    <td className="py-1.5 px-2 font-semibold font-mono">{r.ticker}</td>
                    <td className="text-right py-1.5 px-2 font-mono font-semibold">{r.score?.toFixed(1)}</td>
                    <td className="text-right py-1.5 px-2 font-mono">${r.price?.toFixed(2)}</td>
                    <td className={`text-right py-1.5 px-2 font-mono ${r.rvol >= 1.5 ? 'text-emerald-400' : r.rvol < 0.7 ? 'text-red-400' : ''}`}>
                      {r.rvol?.toFixed(2)}
                    </td>
                    <td className={`text-right py-1.5 px-2 font-mono ${r.rs_20d >= 0.05 ? 'text-emerald-400' : r.rs_20d <= -0.05 ? 'text-red-400' : ''}`}>
                      {r.rs_20d >= 0 ? '+' : ''}{((r.rs_20d || 0) * 100).toFixed(1)}%
                    </td>
                    <td className="text-right py-1.5 px-2 font-mono">{r.adx?.toFixed(0)}</td>
                    <td className={`text-right py-1.5 px-2 font-mono ${(r.pct_from_52w_high || 0) >= -0.03 ? 'text-emerald-400' : ''}`}>
                      {((r.pct_from_52w_high || 0) * 100).toFixed(1)}%
                    </td>
                    <td className="py-1.5 px-2 text-[11px] app-text-secondary">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- Auto-Trader Panel: enable, configure, monitor automated trades ----------
function AutoTraderPanel({ reloadToken }) {
  const [status, setStatus] = useState(null);
  const [trades, setTrades] = useState([]);
  // putsWatch was eagerly fetched here (blocked first paint for ~1.5s); now
  // PutsWatchSection lazily fetches on expand.
  const [busy, setBusy] = useState(false);
  const [showCfg, setShowCfg] = useState(false);
  const [expanded, setExpanded] = useState(null); // trade.id whose post-mortem is open
  const [newsExpanded, setNewsExpanded] = useState(null); // trade.id whose news context is open
  const [rationaleExpanded, setRationaleExpanded] = useState(null); // trade.id whose rationale is open

  const [error, setError] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [savedToast, setSavedToast] = useState(null);
  const inFlight = useRef(false);
  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const results = await Promise.allSettled([
        api.get('/api/trading/auto/status'),
        api.get('/api/trading/auto/trades?limit=20'),
      ]);
      const [sr, tr] = results;
      let anyOk = false;
      if (sr.status === 'fulfilled') { setStatus(sr.value); anyOk = true; }
      if (tr.status === 'fulfilled') { setTrades(tr.value || []); anyOk = true; }
      // r42 fix #1.22 / #1.30: distinguish "the request failed" from "no
      // data yet". When BOTH fetches reject, surface a banner so the
      // operator doesn't read an empty panel as "all clear".
      setLoadFailed(!anyOk);
      setError(anyOk ? null : (sr.status === 'rejected' ? (sr.reason?.detail || sr.reason?.message || 'Failed to load auto-trader status') : null));
    } finally {
      inFlight.current = false;
    }
  }, []);

  // r42 fix #1.13 + #1.20: refresh on push events (trade_opened, trade_closed,
  // app:resync) so we don't wait for the 30s poll. Polling moves to 30s
  // since the WS is now the primary update channel.
  useEffect(() => {
    load();
    const iv = setInterval(() => {
      // r42 fix #2.28: skip polling when tab is hidden — push will
      // catch us up on visibilitychange.
      if (document.visibilityState !== 'visible') return;
      load();
    }, 30000);
    const onPush = () => load();
    window.addEventListener('app:trade_opened', onPush);
    window.addEventListener('app:trade_closed', onPush);
    window.addEventListener('app:resync', onPush);
    return () => {
      clearInterval(iv);
      window.removeEventListener('app:trade_opened', onPush);
      window.removeEventListener('app:trade_closed', onPush);
      window.removeEventListener('app:resync', onPush);
    };
  }, [load, reloadToken]);

  const showToast = useCallback((msg, kind = 'success') => {
    setSavedToast({ msg, kind });
    setTimeout(() => setSavedToast(null), 2000);
  }, []);

  const toggle = async () => {
    if (!status) return;
    if (status.enabled && !confirm('Pause auto-trader?\nNo new entries will open. Existing positions continue to be managed (trailing stops still trail).')) return;
    setBusy(true); setError(null);
    try {
      // r42 fix #1.14: VERIFY the response before optimistic UI update.
      const r = await api.post('/api/trading/auto/config', { enabled: !status.enabled });
      // The status endpoint returns the new config — read back authoritatively.
      await load();
      showToast(r?.enabled === !status.enabled ? 'Updated' : 'Saved', 'success');
    } catch (e) {
      setError('Toggle failed: ' + (e?.detail || e?.message || e));
    } finally { setBusy(false); }
  };

  const updateCfg = async (patch) => {
    setBusy(true); setError(null);
    try {
      await api.post('/api/trading/auto/config', patch);
      await load();
      showToast('Saved', 'success');
    } catch (e) {
      setError('Update failed: ' + (e?.detail || e?.message || e));
    } finally { setBusy(false); }
  };

  if (!status) {
    // Skeleton — paints immediately so the panel slot isn't blank.
    return (
      <div className="surface rounded-2xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div className="skel h-7 w-48" />
          <div className="skel h-7 w-24" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {[0,1,2,3].map(i => <div key={i} className="skel h-20" />)}
        </div>
        <div className="skel h-2 w-full" />
        <div className="skel h-2 w-full" />
      </div>
    );
  }
  if (!status.broker_connected) {
    return (
      <div className="surface rounded-2xl p-5 text-sm app-text-muted">
        Auto-trader unavailable: broker not connected.
      </div>
    );
  }

  const pct = (used, budget) => budget > 0 ? Math.min(100, (used / budget) * 100) : 0;
  const cfg = status.config;
  const stockPct = pct(status.stock_used, status.stock_budget);
  const optionPct = pct(status.option_used, status.option_budget);
  const totalPct = status.total_cap > 0 ? (status.deployed / status.total_cap) * 100 : 0;
  const liveCount = trades.filter(t => t.status === 'open' || t.status === 'pending').length;

  return (
    <div className="surface rounded-2xl p-5 shadow-xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="text-xl font-bold flex items-center gap-2">
            <span>🤖</span>
            <span>Auto-Trader</span>
          </h3>
          <span className={`pill ${status.enabled ? 'pill-success' : ''}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${status.enabled ? 'bg-emerald-400 live-dot' : 'bg-gray-500'}`}></span>
            {status.enabled ? 'Running' : 'Paused'}
          </span>
          {cfg.trade_options && <span className="pill">Puts ON</span>}
          {cfg.trade_calls && <span className="pill">Calls ON</span>}
          {cfg.aggressive_options_mode && <span className="pill pill-warn">🚀 Aggressive</span>}
          {cfg.use_universe_scanner && <span className="pill pill-success">🎯 Universe ON</span>}
          {cfg.dry_run && <span className="pill pill-warn">Dry run</span>}
          <span className="pill">Conf ≥ {cfg.confidence_threshold}%</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowCfg(s => !s)}
            className="text-xs app-text-secondary hover:app-text-primary flex items-center gap-1 px-2.5 py-1.5 rounded-lg hover:bg-white/5 border app-border-soft"
          >
            <span>⚙</span><span>Config</span>
          </button>
          <button
            onClick={toggle}
            disabled={busy}
            className={`px-3.5 py-1.5 text-xs rounded-lg font-semibold transition ${
              status.enabled
                ? 'bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/40'
                : 'bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 border border-emerald-500/40'
            } disabled:opacity-50`}
          >
            {status.enabled ? 'Pause' : 'Start'}
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/40 bg-red-500/10 text-red-200 text-xs px-3 py-2 mb-3">
          {error}{' '}
          <button onClick={() => { setError(null); load(); }} className="underline">Retry</button>
        </div>
      )}
      {savedToast && (
        <div role="status" className={`rounded-lg border text-xs px-3 py-2 mb-3 ${savedToast.kind === 'success' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200' : 'border-amber-500/40 bg-amber-500/10 text-amber-200'}`}>
          {savedToast.msg}
        </div>
      )}

      {/* Hero stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <Stat
          label="Equity"
          value={`$${Number(status.equity).toLocaleString(undefined, {maximumFractionDigits: 0})}`}
          hint={`Cap $${Number(status.total_cap).toLocaleString(undefined, {maximumFractionDigits: 0})}`}
        />
        <Stat
          label="Deployed"
          value={`$${Number(status.deployed).toLocaleString(undefined, {maximumFractionDigits: 0})}`}
          hint={`${totalPct.toFixed(1)}% of cap`}
        />
        <Stat
          label="Open Trades"
          value={liveCount}
          hint={`${trades.length} total`}
        />
        {(() => {
          // r42 fix #1.19: compare same-day in the operator's LOCAL timezone
          // (matches "today" mental model) and label the hint accordingly.
          // Memoized in a closure so we compute it once.
          const todayLocal = new Date().toDateString();
          const todayClosed = trades.filter(t => {
            if (!t.closed_at) return false;
            const d = parseServerDate(t.closed_at);
            return d && d.toDateString() === todayLocal;
          });
          const pl = todayClosed.reduce((a, t) => a + (t.realized_pl || 0), 0);
          return (
            <Stat
              label="Today P/L"
              value={`${pl >= 0 ? '+' : ''}$${pl.toFixed(2)}`}
              positive={pl > 0}
              negative={pl < 0}
              hint={`Local today, ${todayClosed.length} closed`}
            />
          );
        })()}
      </div>

      {/* Budget gauges */}
      <div className="space-y-3 mb-4">
        <BudgetBar
          label="Stock allocation"
          used={status.stock_used} budget={status.stock_budget} pct={stockPct}
          color="bg-gradient-to-r from-blue-500 to-indigo-500"
        />
        <BudgetBar
          label="Option allocation"
          used={status.option_used} budget={status.option_budget} pct={optionPct}
          color="bg-gradient-to-r from-purple-500 to-fuchsia-500"
        />
      </div>

      {/* Config drawer */}
      {showCfg && (
        <div className="surface-soft rounded-xl p-4 mb-4">
          <div className="text-xs app-text-muted uppercase tracking-[0.14em] font-semibold mb-3">Configuration</div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
            <CfgField label="Confidence threshold" value={cfg.confidence_threshold} suffix="%"
                      onCommit={v => updateCfg({ confidence_threshold: Number(v) })} />
            <CfgField label="Risk per trade" value={cfg.max_risk_per_trade_pct * 100} suffix="% equity"
                      onCommit={v => updateCfg({ max_risk_per_trade_pct: Number(v) / 100 })} />
            <CfgField label="Stock bucket" value={cfg.stock_pct_of_equity * 100} suffix="% equity"
                      onCommit={v => updateCfg({ stock_pct_of_equity: Number(v) / 100 })} />
            <CfgField label="Option bucket" value={cfg.option_pct_of_equity * 100} suffix="% equity"
                      onCommit={v => updateCfg({ option_pct_of_equity: Number(v) / 100 })} />
          </div>
          <div className="mt-3 pt-3 border-t app-border-soft space-y-2">
            <label className="flex items-center gap-2 cursor-pointer text-sm">
              <input type="checkbox" checked={!!cfg.trade_options}
                     onChange={e => updateCfg({ trade_options: e.target.checked })}
                     className="accent-blue-500" />
              <span>Auto-buy <strong>PUTs</strong> on bearish setups</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer text-sm">
              <input type="checkbox" checked={!!cfg.trade_calls}
                     disabled={!cfg.trade_options}
                     onChange={e => updateCfg({ trade_calls: e.target.checked })}
                     className="accent-blue-500" />
              <span className={!cfg.trade_options ? 'opacity-50' : ''}>
                Auto-buy <strong>CALLs</strong> on bullish setups
                {!cfg.trade_options && <span className="app-text-muted"> (requires PUTs enabled)</span>}
              </span>
            </label>
            <div className="pt-2 border-t app-border-soft">
              <label className="flex items-start gap-2 cursor-pointer text-sm">
                <input type="checkbox" checked={!!cfg.aggressive_options_mode}
                       disabled={!cfg.trade_options || !cfg.trade_calls}
                       onChange={e => updateCfg({ aggressive_options_mode: e.target.checked })}
                       className="accent-orange-500 mt-0.5" />
                <div className={(!cfg.trade_options || !cfg.trade_calls) ? 'opacity-50' : ''}>
                  <div className="font-semibold">🚀 Aggressive options mode</div>
                  <div className="text-[11px] app-text-muted leading-relaxed">
                    Treat options as the <em>primary</em> growth vehicle. Liberalizes call/put
                    triggers (45% thesis floor), drops contract score gate to 55,
                    raises per-ticker option cap to 50%, and removes the concentration
                    guard so a call can stack on an existing stock long. Pair with a
                    30/70 stock/option budget split.
                    {!(cfg.trade_options && cfg.trade_calls) && <span className="block mt-1 text-amber-400">Requires both PUTs and CALLs enabled.</span>}
                  </div>
                </div>
              </label>
              {cfg.aggressive_options_mode && (
                <div className="mt-2 flex items-center gap-2 flex-wrap">
                  <span className="pill pill-warn">⚠ Higher risk — premium decay + leverage</span>
                  {(Math.abs(cfg.stock_pct_of_equity - 0.30) > 0.05 || Math.abs(cfg.option_pct_of_equity - 0.70) > 0.05) && (
                    <button
                      onClick={() => updateCfg({ stock_pct_of_equity: 0.30, option_pct_of_equity: 0.70 })}
                      className="text-[11px] px-2 py-1 rounded-md bg-orange-500/20 hover:bg-orange-500/30 text-orange-400 border border-orange-500/40 font-semibold"
                    >
                      Apply 30/70 stock/option split
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
          {/* Universe scanner + entry order type */}
          <div className="mt-3 pt-3 border-t app-border-soft space-y-2">
            <label className="flex items-start gap-2 cursor-pointer text-sm">
              <input type="checkbox" checked={!!cfg.use_universe_scanner}
                     onChange={e => updateCfg({ use_universe_scanner: e.target.checked })}
                     className="accent-blue-500 mt-0.5" />
              <div>
                <div className="font-semibold">🎯 Universe scanner (top-N ranked)</div>
                <div className="text-[11px] app-text-muted leading-relaxed">
                  Trade from the daily top-{cfg.universe_top_n ?? 30} setups across ~500 liquid US equities instead of just the watchlist. Scan runs 4× per day at UTC 12:00 / 14:30 / 17:00 / 19:30.
                </div>
              </div>
            </label>
            <label className="flex items-center gap-2 cursor-pointer text-sm">
              <input type="checkbox" checked={cfg.entry_order_type === 'limit_at_mid'}
                     onChange={e => updateCfg({ entry_order_type: e.target.checked ? 'limit_at_mid' : 'market' })}
                     className="accent-blue-500" />
              <span>
                <strong>Limit-at-mid entries</strong>
                <span className="text-[11px] app-text-muted ml-2">captures ~half the bid-ask spread; falls back to market on illiquid quotes</span>
              </span>
            </label>
          </div>
          {/* Position reconciliation + PDT enforcement */}
          <div className="mt-3 pt-3 border-t app-border-soft space-y-2">
            <div className="text-[11px] uppercase tracking-wide app-text-muted font-semibold mb-1">Reconciliation &amp; safety</div>
            <label className="flex items-start gap-2 cursor-pointer text-sm">
              <input type="checkbox" checked={!!cfg.auto_promote_adopted}
                     onChange={e => updateCfg({ auto_promote_adopted: e.target.checked })}
                     className="accent-blue-500 mt-0.5" />
              <div>
                <div className="font-semibold">🔁 Auto-promote external positions</div>
                <div className="text-[11px] app-text-muted leading-relaxed">
                  Hourly reconcile job sees an Alpaca position the bot didn't open (option assignment, manual dashboard trade, missed bracket fill) → automatically adopts it AND promotes to bot-managed: computes stop/T1/T2/T3 from current price + 1.5×ATR, submits a real broker stop-loss, the manage loop trails / partial-exits / stops it like any other auto-trade. When OFF (default), the job only alerts; you reconcile manually via <code>POST /api/admin/sync-positions</code> + <code>/api/admin/promote-adopted/{`{ticker}`}</code>.
                </div>
              </div>
            </label>
            <label className="flex items-start gap-2 cursor-pointer text-sm">
              <input type="checkbox" checked={!!cfg.pdt_enforce}
                     onChange={e => updateCfg({ pdt_enforce: e.target.checked })}
                     className="accent-rose-500 mt-0.5" />
              <div>
                <div className="font-semibold">🛡 PDT enforcement (live margin &lt; $25k)</div>
                <div className="text-[11px] app-text-muted leading-relaxed">
                  Hard-blocks new entries when ≥3 day-trades have occurred in the trailing 5 business days. Prevents the 4th from triggering a 90-day Pattern Day Trader lock. <strong>Must enable before going live with margin &lt; $25k.</strong> Defaulted off because Alpaca paper isn't PDT-restricted.
                </div>
              </div>
            </label>
          </div>
          <div className="mt-3 text-[11px] app-text-muted leading-relaxed">
            Strategy: long stock on BUY ≥ threshold with bracket stop. Soft-BE at T1 · BE at T2 · recompute + chandelier past T3. Puts use a synthesized bear thesis; exits on T1/T2 hit, 50% premium decay, or underlying broke stop.
          </div>
        </div>
      )}

      {/* Auto-trade list — collapsible with scrolling body */}
      <div className="mb-4">
        <CollapsibleSection
          title="Auto-Trades"
          count={trades.length}
          subtitle={`${liveCount} live · ${trades.length} total`}
          defaultOpen={false}
          maxHeight={460}
        >
        {trades.length === 0 ? (
          <div className="text-center text-sm app-text-muted italic py-4">
            {status.enabled ? 'Waiting for the next strong BUY signal…' : 'Enable auto-trading to let signals open positions automatically.'}
          </div>
        ) : (
          <div className="space-y-2">
            {trades.map(t => {
              const losingStop = t.status === 'closed_stop' && (t.realized_pl ?? 0) < 0;
              const isPmOpen = expanded === t.id;
              const isClosed = !!t.closed_at;
              const isNewsOpen = newsExpanded === t.id;
              const isRatOpen = rationaleExpanded === t.id;
              const statusPill =
                t.status === 'open' ? { cls: 'pill-success', label: 'live' } :
                t.status === 'pending' ? { cls: 'pill-warn', label: 'pending' } :
                t.status === 'adopted' ? { cls: 'pill-warn', label: 'adopted' } :
                t.status === 'closed_target' ? { cls: 'pill-success', label: 'target' } :
                t.status === 'closed_stop' ? { cls: 'pill-danger', label: 'stopped' } :
                t.status?.startsWith('closed_') ? { cls: '', label: t.status.replace('closed_', '') } :
                { cls: '', label: t.status };
              // r42 fix #1.24: extract the LAST EXIT: tag from `note` so the
              // exit reason (news_exit, reverse_signal, theta_stop, ...) is
              // visible without expanding the row.
              const exitMatch = isClosed && typeof t.note === 'string'
                ? t.note.match(/EXIT:\s*([^|]+)/)
                : null;
              const exitReasonShort = exitMatch ? exitMatch[1].trim().slice(0, 60) : null;
              const plColor = t.realized_pl == null ? 'app-text-muted'
                            : t.realized_pl >= 0 ? 'text-emerald-400'
                            : 'text-red-400';
              return (
                <div key={t.id} className="surface-soft rounded-xl p-3 lift">
                  {/* Top row: ticker + pills + P/L */}
                  <div className="flex items-start justify-between gap-3 mb-2 flex-wrap">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold text-base font-mono">{t.ticker}</span>
                      <span className="text-[10px] app-text-muted uppercase">{t.asset_type}</span>
                      <span className={`pill ${statusPill.cls}`}>{statusPill.label}</span>
                      {(t.level_index ?? 0) > 0 && (
                        <span
                          title={`Trail level ${t.level_index}${t.targets_history?.length ? ` · ${t.targets_history.length} recalc` : ''}`}
                          className="pill pill-warn"
                        >
                          L{t.level_index}{t.targets_history?.length ? `·R${t.targets_history.length}` : ''}
                        </span>
                      )}
                    </div>
                    <div className={`font-mono text-sm font-bold ${plColor}`}>
                      {/* r42 fix #2.24: prepend +/− sign so colorblind users
                          can distinguish wins from losses without color. */}
                      {t.realized_pl == null
                        ? <span className="app-text-muted">—</span>
                        : `${t.realized_pl > 0 ? '▲ +' : t.realized_pl < 0 ? '▼ ' : ''}$${t.realized_pl.toFixed(2)}`}
                    </div>
                  </div>

                  {exitReasonShort && (
                    <div className="text-[10px] app-text-muted mb-2 italic" title={exitReasonShort}>
                      Exit: {exitReasonShort}
                    </div>
                  )}

                  {/* Middle row: qty, entry, stop, targets */}
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                    <div>
                      <div className="text-[10px] uppercase tracking-wider app-text-muted">Qty</div>
                      <div className="font-mono">{t.qty}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wider app-text-muted">Entry</div>
                      <div className="font-mono">
                        {t.entry_price ? `$${t.entry_price.toFixed(2)}` : <span className="app-text-muted">~${t.requested_entry?.toFixed(2)}</span>}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wider app-text-muted">Stop</div>
                      <div className="font-mono text-red-400">{t.current_stop != null ? `$${t.current_stop.toFixed(2)}` : '—'}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wider app-text-muted">Targets</div>
                      <div className="font-mono text-emerald-400 text-[11px]">
                        {[t.target1, t.target2, t.target3].filter(x => x != null).map(x => `$${x.toFixed(2)}`).join(' / ')}
                      </div>
                    </div>
                  </div>

                  {/* Actions row — rationale always available; post-mortem + news as before */}
                  <div className="mt-2 pt-2 border-t app-border-soft flex gap-2 flex-wrap">
                    <button
                      onClick={() => setRationaleExpanded(isRatOpen ? null : t.id)}
                      className="text-[10px] px-2 py-1 rounded-md bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 border border-emerald-500/30 font-semibold"
                    >
                      📊 {isRatOpen ? 'Hide rationale' : 'Why this trade?'}
                    </button>
                    {losingStop && (
                      <button
                        onClick={() => setExpanded(isPmOpen ? null : t.id)}
                        className="text-[10px] px-2 py-1 rounded-md bg-red-500/15 hover:bg-red-500/25 text-red-400 border border-red-500/30 font-semibold"
                      >
                        🔍 {isPmOpen ? 'Hide post-mortem' : 'Why did this lose?'}
                      </button>
                    )}
                    {isClosed && (
                      <button
                        onClick={() => setNewsExpanded(isNewsOpen ? null : t.id)}
                        className="text-[10px] px-2 py-1 rounded-md bg-blue-500/15 hover:bg-blue-500/25 text-blue-400 border border-blue-500/30 font-semibold"
                      >
                        📰 {isNewsOpen ? 'Hide news' : 'News during trade'}
                      </button>
                    )}
                  </div>

                  {/* Expanded sections */}
                  {isRatOpen && (
                    <div className="mt-2 p-2 rounded-lg bg-emerald-500/5 border border-emerald-500/20">
                      <TradeRationale tradeId={t.id} />
                    </div>
                  )}
                  {isPmOpen && (
                    <div className="mt-2 p-2 rounded-lg bg-red-500/5 border border-red-500/20">
                      <PostMortem trade={t} onRegen={async () => {
                        try {
                          const fresh = await api.post(`/api/trading/auto/postmortem/${t.id}`);
                          setTrades(ts => ts.map(x => x.id === t.id ? { ...x, post_mortem: fresh, has_post_mortem: true } : x));
                        } catch (e) { alert('Regen failed: ' + (e.detail || e)); }
                      }} />
                    </div>
                  )}
                  {isNewsOpen && (
                    <div className="mt-2 p-2 rounded-lg bg-blue-500/5 border border-blue-500/20">
                      <TradeNewsContext tradeId={t.id} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        </CollapsibleSection>
      </div>

      {/* Options watch — both sides, lazy-loaded on expand */}
      <CallsWatchSection canTrade={!!cfg.trade_calls} />
      <PutsWatchSection canTrade={!!cfg.trade_options} />
    </div>
  );
}

// Generic lazy options-watch section — rendered once for calls + once for puts.
// Cached for the session so re-opening doesn't refetch.
function OptionsWatchSection({ kind, endpoint, label, icon, cardBorder, canTrade }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const fetchData = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const d = await api.get(endpoint);
      setData(d);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [endpoint]);

  const toggleOpen = () => {
    const next = !open;
    setOpen(next);
    if (next && !data && !loading) fetchData();
  };

  const sugg = data?.suggestions || [];
  const pillCls = kind === 'call' ? 'pill-success' : 'pill-danger';
  const confPill = kind === 'call' ? 'pill-success' : 'pill-danger';
  const confLabel = kind === 'call' ? 'BULL' : 'BEAR';
  const strikeColor = kind === 'call' ? 'text-emerald-400' : 'text-red-400';

  return (
    <div className="mt-2 pt-3 border-t app-border-soft">
      <button
        onClick={toggleOpen}
        className="w-full flex items-center justify-between app-text-secondary hover:app-text-primary py-1 rounded"
      >
        <div className="flex items-center gap-2">
          <span className="text-xs">{open ? '▼' : '▸'}</span>
          <span className="text-xs uppercase tracking-wider font-semibold">{icon} {label}</span>
          {data && <span className="pill">{sugg.length}</span>}
          {!canTrade && <span className="pill pill-warn">manual only</span>}
        </div>
        {open && (
          <span
            className="text-[11px] app-text-muted hover:app-text-primary cursor-pointer px-2 py-0.5 rounded hover:bg-white/5"
            onClick={e => { e.stopPropagation(); fetchData(); }}
          >
            ↻ Rescan
          </span>
        )}
      </button>
      {open && (
        <div className="mt-3">
          {loading && <div className="text-xs app-text-muted italic">Scanning chain across watchlist…</div>}
          {err && <div className="text-xs text-red-400">Error: {err}</div>}
          {!loading && !err && sugg.length === 0 && (
            <div className="text-xs app-text-muted italic">
              No {kind === 'call' ? 'bullish call' : 'bearish put'}-plays found. (Weak conviction or illiquid chains are skipped.)
            </div>
          )}
          {sugg.length > 0 && (
            <div className="space-y-2">
              {sugg.map(s => (
                <div key={s.ticker} className={`surface-soft rounded-xl p-3 border ${cardBorder}`}>
                  <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold text-base font-mono">{s.ticker}</span>
                      <span className="text-xs app-text-muted">{s.name}</span>
                      <span className={`pill ${confPill}`}>{confLabel} {s.thesis.confidence}%</span>
                    </div>
                    <div className="text-[11px] app-text-muted font-mono">
                      Entry ${s.thesis.entry?.toFixed(2)} · Stop ${s.thesis.stop_loss?.toFixed(2)} · T1 ${s.thesis.target1?.toFixed(2)} · T2 ${s.thesis.target2?.toFixed(2)}
                    </div>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                    {s.top_contracts.slice(0, 3).map((c, i) => (
                      <div key={i} className="app-bg-surface-solid rounded-lg p-2 border app-border-soft">
                        <div className={`font-semibold ${strikeColor} text-xs`}>
                          ${c.strike} {kind === 'call' ? 'CALL' : 'PUT'} · {c.expiration} ({c.dte}d)
                          {c.is_weekly && <span className="ml-1 pill pill-warn text-[9px]">WKLY</span>}
                        </div>
                        <div className="text-[11px] app-text-secondary font-mono mt-0.5">
                          ${c.premium} · BE ${c.breakeven} · R:R {c.rr_t1}/{c.rr_t2}/{c.rr_t3} · <span className="text-emerald-400 font-bold">{c.score}</span>
                        </div>
                        <div className="text-[10px] app-text-muted font-mono">vol {c.volume} · OI {c.open_interest} · IV {c.iv}% · Δ {c.delta_estimate}</div>
                      </div>
                    ))}
                  </div>
                  {s.thesis.reasoning && (
                    <div className="text-[11px] app-text-muted mt-2 reasoning-text line-clamp-3">
                      {s.thesis.reasoning}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CallsWatchSection({ canTrade }) {
  return (
    <OptionsWatchSection
      kind="call"
      endpoint="/api/options/calls-watch"
      label="Call-Play Watch"
      icon="📈"
      cardBorder="border-emerald-500/20"
      canTrade={canTrade}
    />
  );
}

// Lazy puts-watch — only hits the expensive endpoint when the user expands
// this section. Cached for the session so re-opening doesn't refetch.
function PutsWatchSection({ canTrade }) {
  return (
    <OptionsWatchSection
      kind="put"
      endpoint="/api/options/puts-watch"
      label="Put-Play Watch"
      icon="📉"
      cardBorder="border-purple-500/20"
      canTrade={canTrade}
    />
  );
}

function BudgetBar({ label, used, budget, pct, color }) {
  // r42 Tier 3 polish: clamp the rendered bar width to [0, 100] so over-
  // deployment (e.g. external positions pushing past cap) doesn't draw
  // outside the track. Numeric pct in the label still shows the true value.
  const safePct = Math.max(0, Math.min(100, Number.isFinite(pct) ? pct : 0));
  const overflowed = pct > 100;
  return (
    <div>
      <div className="flex justify-between text-xs mb-1.5">
        <span className="app-text-secondary font-semibold">{label}</span>
        <span className="font-mono app-text-primary">
          ${Number(used).toLocaleString(undefined, {maximumFractionDigits: 0})}
          <span className="app-text-muted"> / ${Number(budget).toLocaleString(undefined, {maximumFractionDigits: 0})}</span>
          <span className={`ml-1.5 ${overflowed ? 'text-amber-400' : 'app-text-muted'}`}>({Number(pct).toFixed(1)}%{overflowed ? ' over' : ''})</span>
        </span>
      </div>
      <div
        className="h-2 rounded-full overflow-hidden"
        style={{ background: 'var(--surface-border)' }}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(safePct)}
        aria-label={`${label} ${safePct.toFixed(0)}%`}
      >
        <div className={`h-full ${overflowed ? 'bg-amber-500' : color} transition-all`} style={{ width: `${safePct}%` }} />
      </div>
    </div>
  );
}

function CfgField({ label, value, suffix, onCommit }) {
  const [v, setV] = useState(value);
  const focusedRef = useRef(false);
  // Only sync prop → local state when the user isn't actively editing.
  // Without this guard, a polling refresh that re-fetches `value` mid-typing
  // would clobber the in-progress edit on the very next render.
  useEffect(() => {
    if (!focusedRef.current) setV(value);
  }, [value]);
  return (
    <div>
      <label className="text-gray-500 block mb-1">{label}</label>
      <div className="flex items-center gap-1">
        <input
          type="number" step="0.1" value={v}
          onFocus={() => { focusedRef.current = true; }}
          onChange={e => setV(e.target.value)}
          onBlur={() => {
            focusedRef.current = false;
            if (Number(v) !== Number(value)) onCommit(v);
          }}
          className="w-20 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm"
        />
        <span className="text-gray-500">{suffix}</span>
      </div>
    </div>
  );
}

function PostMortem({ trade, onRegen }) {
  const pm = trade.post_mortem;
  if (!pm) {
    return (
      <div className="text-xs text-gray-400">
        No post-mortem yet for this trade.
        <button onClick={onRegen} className="ml-2 px-2 py-0.5 rounded bg-blue-700 hover:bg-blue-600 text-white">Generate now</button>
      </div>
    );
  }
  const sevColor = (s) => s === 'high' ? 'text-red-300 border-red-700 bg-red-950/40'
                       : s === 'med'  ? 'text-amber-300 border-amber-700 bg-amber-950/40'
                       : 'text-gray-300 border-gray-700 bg-gray-900/40';
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-red-300">Loss post-mortem · {pm.timeframe_used} bars</div>
          <div className="font-semibold text-white text-sm">{pm.verdict}</div>
          <div className="text-gray-300">{pm.summary}</div>
        </div>
        <button onClick={onRegen} className="text-[10px] px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 whitespace-nowrap">↻ Re-run</button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {pm.findings?.map((f, i) => (
          <div key={i} className={`border rounded p-2 ${sevColor(f.severity)}`}>
            <div className="font-semibold mb-0.5">{f.title}</div>
            <div className="reasoning-text text-[11px] opacity-90">{f.body}</div>
          </div>
        ))}
      </div>
      {pm.lessons?.length > 0 && (
        <div className="bg-blue-950/30 border border-blue-800 rounded p-2">
          <div className="text-[10px] uppercase tracking-wider text-blue-300 mb-1">Lessons for next time</div>
          <ul className="list-disc list-inside text-blue-100 space-y-0.5">
            {pm.lessons.map((l, i) => <li key={i}>{l}</li>)}
          </ul>
        </div>
      )}
      <div className="text-[10px] text-gray-500">
        Entry ${pm.entry_price?.toFixed(2)} · Stop ${pm.stop_price?.toFixed(2)} · T1 ${pm.target1?.toFixed(2)} · generated {pm.generated_at?.slice(0, 19)}Z
      </div>
    </div>
  );
}

// r52e: P/L reconciliation widget — single-source-of-truth accounting.
// Hits /api/trading/pnl-reconciliation which does the math server-side
// (Alpaca account equity + portfolio_history.base_value + bot's
// closed-trade ledger + open unrealized + reconciliation gap).
function PnLReconciliationPanel() {
  const { data, error, refresh } = useSWR(
    '/api/trading/pnl-reconciliation',
    () => api.get('/api/trading/pnl-reconciliation'),
    { intervalMs: 60000 }
  );
  if (error && !data) {
    return (
      <div className="surface rounded-2xl p-4 shadow-xl text-xs text-amber-300">
        P/L reconciliation unavailable: {friendlyError(error)}
      </div>
    );
  }
  if (!data) {
    return <div className="surface rounded-2xl p-4 shadow-xl skel h-32" />;
  }
  // r53 fix (Tier-1 #9): guard against null/missing starting_equity (empty
  // Alpaca portfolio_history). Prior code crashed inside `.toLocaleString()`
  // on null and unmounted the panel without a banner — operator saw blank
  // space and assumed P/L was healthy.
  if (data.starting_equity == null || !Number.isFinite(data.starting_equity)) {
    return (
      <div className="surface rounded-2xl p-4 shadow-xl text-xs app-text-muted">
        P/L reconciliation unavailable: no `starting_equity` baseline from
        Alpaca (portfolio_history may be empty for fresh accounts).
      </div>
    );
  }
  const totalDriftPct = data.starting_equity > 0
    ? (data.total_drift / data.starting_equity) * 100
    : 0;
  const todayDrift = data.today_drift;
  const fmt$ = (n) => `${n >= 0 ? '+' : ''}$${Math.abs(n).toLocaleString(undefined, {maximumFractionDigits: 2})}${n < 0 ? '' : ''}`.replace('$-', '−$').replace('+$', '+$');
  const driftClass = (n) => n > 0 ? 'text-emerald-400' : n < 0 ? 'text-red-400' : 'app-text-muted';
  return (
    <div className="surface rounded-2xl p-4 shadow-xl mb-4" data-r49-card>
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-sm font-bold uppercase tracking-wider app-text-secondary flex items-center gap-2">
          <span>📊</span><span>P/L Reconciliation</span>
        </h3>
        <button onClick={refresh} className="text-[10px] app-text-muted hover:app-text-primary">↻ Refresh</button>
      </div>

      {/* Top row: account-level truth (Alpaca equity) */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <div className="surface-soft rounded-lg p-2.5">
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Starting</div>
          <div className="font-mono text-base font-bold">${data.starting_equity.toLocaleString(undefined,{maximumFractionDigits:0})}</div>
        </div>
        <div className="surface-soft rounded-lg p-2.5">
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Current Equity</div>
          <div className="font-mono text-base font-bold">${data.current_equity.toLocaleString(undefined,{maximumFractionDigits:0})}</div>
        </div>
        <div className="surface-soft rounded-lg p-2.5">
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Total Drift</div>
          <div className={`font-mono text-base font-bold ${driftClass(data.total_drift)}`}>
            {fmt$(data.total_drift)} <span className="text-[10px]">({totalDriftPct.toFixed(2)}%)</span>
          </div>
        </div>
        <div className="surface-soft rounded-lg p-2.5">
          <div className="text-[9px] uppercase tracking-wider app-text-muted">Today</div>
          <div className={`font-mono text-base font-bold ${driftClass(todayDrift || 0)}`}>
            {todayDrift != null ? fmt$(todayDrift) : '—'}
          </div>
        </div>
      </div>

      {/* Breakdown of total drift */}
      <div className="mb-2 text-[10px] uppercase tracking-wider app-text-muted">
        Where did the {data.total_drift >= 0 ? 'gain' : 'loss'} come from?
      </div>
      <div className="space-y-1.5 mb-3">
        <ReconRow
          label="Realized (closed trades)"
          value={data.realized_total}
          hint={`${data.n_closed} closed`}
          fmt={fmt$}
          driftClass={driftClass}
        />
        <ReconRow
          label="Unrealized (open positions)"
          value={data.unrealized_total}
          hint={`${data.n_open} open  ·  stocks ${fmt$(data.unrealized_stocks)}  ·  options ${fmt$(data.unrealized_options)}`}
          fmt={fmt$}
          driftClass={driftClass}
        />
        <ReconRow
          label="Reconciliation gap"
          value={data.reconciliation_gap}
          hint="Alpaca-side closes the bot didn't capture (closed_reconciled / closed_external / pre-adoption fills)"
          fmt={fmt$}
          driftClass={driftClass}
          warn={Math.abs(data.reconciliation_gap) > 100}
        />
      </div>

      {/* Realized by status */}
      {data.realized_by_status && Object.keys(data.realized_by_status).length > 0 && (
        <details className="mb-2">
          <summary className="text-[10px] uppercase tracking-wider app-text-muted cursor-pointer hover:app-text-primary">
            Realized P/L by close status
          </summary>
          <div className="mt-2 space-y-1">
            {Object.entries(data.realized_by_status)
              .sort((a, b) => a[1].pl - b[1].pl)
              .map(([st, info]) => (
                <div key={st} className="flex items-center justify-between text-[11px] font-mono">
                  <span className="app-text-secondary">{st} <span className="app-text-muted">({info.count})</span></span>
                  <span className={driftClass(info.pl)}>{fmt$(info.pl)}</span>
                </div>
              ))}
          </div>
        </details>
      )}

      {/* Top losers / winners */}
      {(data.top_losers?.length > 0 || data.top_winners?.length > 0) && (
        <details>
          <summary className="text-[10px] uppercase tracking-wider app-text-muted cursor-pointer hover:app-text-primary">
            Top losers / winners (closed)
          </summary>
          <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-3">
            {data.top_losers?.length > 0 && (
              <div>
                <div className="text-[10px] text-red-400 uppercase tracking-wider mb-1">Losers</div>
                <div className="space-y-1">
                  {data.top_losers.map(t => (
                    <div key={t.id} className="text-[11px] font-mono flex items-center justify-between">
                      <span className="truncate" title={`${t.ticker} ${t.asset_type} · ${t.status} · ${t.closed_at?.slice(0,10)}`}>
                        {t.ticker} <span className="app-text-muted">{t.asset_type}</span>
                      </span>
                      <span className="text-red-400">{fmt$(t.realized_pl)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {data.top_winners?.length > 0 && (
              <div>
                <div className="text-[10px] text-emerald-400 uppercase tracking-wider mb-1">Winners</div>
                <div className="space-y-1">
                  {data.top_winners.map(t => (
                    <div key={t.id} className="text-[11px] font-mono flex items-center justify-between">
                      <span className="truncate" title={`${t.ticker} ${t.asset_type} · ${t.status} · ${t.closed_at?.slice(0,10)}`}>
                        {t.ticker} <span className="app-text-muted">{t.asset_type}</span>
                      </span>
                      <span className="text-emerald-400">{fmt$(t.realized_pl)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

function ReconRow({ label, value, hint, fmt, driftClass, warn }) {
  return (
    <div className={`flex items-baseline justify-between gap-3 text-xs py-1 ${warn ? 'text-amber-300' : ''}`}>
      <div className="min-w-0">
        <div className="font-semibold">{label}</div>
        {hint && <div className="text-[10px] app-text-muted">{hint}</div>}
      </div>
      <div className={`font-mono text-sm font-bold shrink-0 ${driftClass(value)}`}>{fmt(value)}</div>
    </div>
  );
}

// ---------- Position card (r52d split: stock vs option) ----------
// Renders one position with R-progress / distance-to-stop / time-held.
// For options, all underlying-vs-stop calcs use `underlying_price` from
// the backend join (not the option premium) — prevents the mixed-units
// "1237% distance" bug. The premium % (Alpaca's unrealized_plpc) is
// shown alongside, clearly labeled.
function PositionCard({ p, ticker, busy, closePos }) {
  const isOption = (p.asset_type || '').toLowerCase() === 'option'
    || (p.asset_class || '').toUpperCase().includes('OPTION');
  const isWin = p.unrealized_pl >= 0;
  const rowHighlight = (p.symbol === ticker || p.ticker === ticker) ? 'ring-1 ring-blue-500/50' : '';
  const side = (p.side || (p.qty < 0 ? 'sell' : 'buy')).toLowerCase().replace(/^positionside\./, '');
  const isLong = side === 'buy' || side === 'long';
  const direction = isLong ? 'BUY' : 'SELL';
  const stop = p.current_stop || p.stop_loss;
  const t1 = p.target1, t2 = p.target2, t3 = p.target3;
  // r53f: ticker for the chart-open button. Stocks: position symbol IS
  // the ticker. Options: extract underlying from the OCC symbol — the
  // first run of A-Z letters (e.g. "RMBS260515C00150000" → "RMBS"). The
  // backend also surfaces `p.underlying_symbol` / `p.ticker` for
  // bot-managed options; prefer those when present.
  const chartTicker = (() => {
    if (!isOption) return p.symbol;
    const fromBackend = p.underlying_symbol || p.ticker;
    if (fromBackend) return String(fromBackend).toUpperCase();
    const m = String(p.symbol || '').match(/^[A-Z]+/);
    return m ? m[0] : null;
  })();
  const openChart = () => {
    if (!chartTicker) return;
    window.dispatchEvent(new CustomEvent('app:open-chart', { detail: { ticker: chartTicker } }));
  };
  // For options, stop/targets are denominated in UNDERLYING price, so we
  // must compute distance-to-stop and R against the underlying spot —
  // NOT against the option premium.
  const refPrice = isOption ? p.underlying_price : p.current_price;
  const refEntry = isOption ? p.underlying_entry_price : p.avg_entry_price;
  // r53 fix (Tier-1 #9): explicit "data unavailable" signal when refPrice
  // is missing — prior code rendered "—" in the R/Δ cells, indistinguishable
  // from "trade is at break-even".
  const refPriceMissing = isOption && (refPrice == null || !Number.isFinite(refPrice));
  const r = (stop && refEntry && refPrice)
    ? (refPrice - refEntry) / Math.max(1e-9, Math.abs(refEntry - stop)) * (isLong ? 1 : -1)
    : null;
  const distToStop = (stop && refPrice) ? Math.abs(refPrice - stop) : null;
  const distToStopPct = (distToStop && refPrice) ? (distToStop / refPrice) * 100 : null;
  const opened = parseServerDate(p.opened_at) || parseServerDate(p.created_at);
  const heldMin = opened ? Math.max(0, (Date.now() - opened.getTime()) / 60000) : null;
  const heldFmt = heldMin == null ? '—' :
    heldMin < 60 ? `${heldMin.toFixed(0)}m` :
    heldMin < 1440 ? `${(heldMin / 60).toFixed(1)}h` :
    `${(heldMin / 1440).toFixed(1)}d`;
  // Display label: stocks show ticker; options show ticker + strike/exp short form
  const displaySym = p.symbol;
  return (
    <div data-r49-card className={`app-bg-surface-solid rounded-xl p-3 lift border app-border-soft ${rowHighlight}`}>
      <div className="flex items-start justify-between mb-2">
        <div className="min-w-0">
          <div className="font-bold text-base flex items-center gap-2 flex-wrap">
            <span className="truncate" title={displaySym}>{displaySym}</span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold uppercase tracking-wider ${isLong ? 'bg-emerald-500/20 text-emerald-300' : 'bg-red-500/20 text-red-300'}`}>
              <DirIcon dir={direction} /> {direction}
            </span>
            {isOption && p.underlying_symbol && (
              <span className="text-[10px] app-text-muted font-mono">on {p.underlying_symbol}</span>
            )}
          </div>
          <div className="text-[10px] app-text-muted font-mono">
            Qty {Math.abs(p.qty)} @ ${p.avg_entry_price?.toFixed(2)} · held {heldFmt}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {chartTicker && (
            <button
              aria-label={`Open ${chartTicker} chart`}
              onClick={openChart}
              title={isOption ? `Open chart for underlying ${chartTicker}` : 'Open chart'}
              className="text-[10px] px-2 py-1 rounded-md bg-blue-500/20 hover:bg-blue-500/30 text-blue-300 border border-blue-500/30 font-semibold">
              📈 Chart
            </button>
          )}
          <button
            disabled={!!busy[`close:${p.symbol}`]}
            aria-label={`Close ${p.symbol} position`}
            onClick={() => closePos(p.symbol, p)}
            className="text-[10px] px-2 py-1 rounded-md bg-red-500/20 hover:bg-red-500/30 text-red-400 disabled:opacity-50 border border-red-500/30 font-semibold">
            {busy[`close:${p.symbol}`] ? 'Closing…' : 'Close'}
          </button>
        </div>
      </div>
      <div className="flex items-baseline justify-between mb-2">
        <div>
          <div className="text-[9px] uppercase tracking-wider app-text-muted">{isOption ? 'Premium' : 'Last'}</div>
          <div className="font-mono text-base font-bold">{p.current_price ? `$${p.current_price.toFixed(2)}` : '—'}</div>
          {isOption && p.underlying_price != null && (
            <div className="text-[9px] app-text-muted font-mono">underlying ${p.underlying_price.toFixed(2)}</div>
          )}
          {refPriceMissing && (
            <div className="text-[9px] text-amber-400 font-semibold mt-0.5">
              ⚠ underlying price unavailable
            </div>
          )}
        </div>
        <div className="text-right">
          <div className={`font-mono text-lg font-bold ${isWin ? 'text-emerald-400' : 'text-red-400'}`}>
            <DirIcon dir={isWin ? 'up' : 'down'} className="text-xs mr-1" />
            {isWin ? '+' : ''}${p.unrealized_pl?.toFixed(2)}
          </div>
          <div className={`text-[10px] font-mono ${isWin ? 'text-emerald-400' : 'text-red-400'}`}>
            {isWin ? '+' : ''}{p.unrealized_plpc?.toFixed(2)}%
            {isOption ? <span className="app-text-muted"> premium</span> : null}
            {r != null ? ` · ${r >= 0 ? '+' : ''}${r.toFixed(2)}R` : ''}
          </div>
        </div>
      </div>
      {refEntry && stop && (
        <RProgressBar
          entry={refEntry} stop={stop}
          target1={t1} target2={t2} target3={t3}
          current={refPrice} side={direction}
        />
      )}
      <div className="grid grid-cols-3 gap-1 mt-2 text-[9px] font-mono">
        <div className="text-red-400">stop ${stop?.toFixed(2) ?? '—'}</div>
        <div className="text-amber-400 text-center">
          {distToStop != null ? `Δ$${distToStop.toFixed(2)} (${distToStopPct?.toFixed(1)}%)` : '—'}
        </div>
        <div className="text-emerald-400 text-right">T1 ${t1?.toFixed(2) ?? '—'}</div>
      </div>
      {isOption && (
        <div className="text-[9px] app-text-muted mt-1 italic">
          stop / targets are underlying prices
        </div>
      )}
    </div>
  );
}

// r52d: split positions into Stocks and Options sections. Each section has
// its own collapsible header with count + total unrealized P/L.
function PositionsSections({ positions, ticker, error, busy, closePos }) {
  const isOption = (p) => (p.asset_type || '').toLowerCase() === 'option'
    || (p.asset_class || '').toUpperCase().includes('OPTION');
  const stocks = (positions || []).filter(p => !isOption(p));
  const options = (positions || []).filter(p => isOption(p));
  // Put the analyzed-ticker positions first within each section
  const sortByTicker = (arr) => [
    ...arr.filter(p => p.symbol === ticker || p.ticker === ticker),
    ...arr.filter(p => p.symbol !== ticker && p.ticker !== ticker),
  ];
  const stocksSorted = sortByTicker(stocks);
  const optionsSorted = sortByTicker(options);
  const sumPL = (arr) => arr.reduce((a, p) => a + (p.unrealized_pl || 0), 0);
  const stocksPL = sumPL(stocks);
  const optionsPL = sumPL(options);

  if (positions.length === 0) {
    return (
      <div className="mb-4 mt-3">
        <CollapsibleSection title="Open Positions" count={0} defaultOpen={false}>
          <div className="text-center text-sm app-text-muted italic py-4">
            {error ? <span className="text-amber-300">Couldn't load positions: {friendlyError(error)}</span> : 'No open positions.'}
          </div>
        </CollapsibleSection>
      </div>
    );
  }

  const renderSection = (title, arr, totalPL) => {
    if (arr.length === 0) return null;
    const titleNode = (
      <span className="flex items-center gap-2">
        <span>{title}</span>
        <span className={`text-[10px] font-mono ${totalPL >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          {totalPL >= 0 ? '+' : ''}${totalPL.toFixed(2)}
        </span>
      </span>
    );
    return (
      <div className="mb-3 mt-3">
        <CollapsibleSection title={titleNode} count={arr.length} defaultOpen={true} maxHeight={520}>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {arr.map(p => (
              <PositionCard key={p.symbol} p={p} ticker={ticker} busy={busy} closePos={closePos} />
            ))}
          </div>
        </CollapsibleSection>
      </div>
    );
  };

  return (
    <>
      {renderSection('Stock Positions', stocksSorted, stocksPL)}
      {renderSection('Option Positions', optionsSorted, optionsPL)}
    </>
  );
}

// ---------- Per-ticker auto-trade toggle ----------
// Shown in the analysis-view header. Hits PATCH /api/watchlist/{ticker}/auto-trade
// and re-fetches overview so the WatchlistPanel badge updates in lockstep.
function TickerAutoTradeToggle({ ticker, onChanged }) {
  const [enabled, setEnabled] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!ticker) return;
    setLoaded(false);
    api.get('/api/watchlist').then(rows => {
      const row = rows.find(r => r.ticker === ticker);
      if (row) setEnabled(row.auto_trade_enabled !== false);
      setLoaded(true);
    }).catch(() => setLoaded(true));
  }, [ticker]);

  if (!ticker || !loaded) return null;

  // r42 fix #1.14: VERIFY the response before flipping UI state. Previously
  // the UI flipped optimistically on ANY response — silent 4xx desync.
  const toggle = async () => {
    if (busy) return;
    setBusy(true); setErr(null);
    const next = !enabled;
    try {
      const r = await api.patch(`/api/watchlist/${ticker}/auto-trade`, { enabled: next });
      // Backend is expected to echo the new state. Trust the server's value
      // rather than our optimistic guess.
      const serverEnabled = r && typeof r.auto_trade_enabled === 'boolean' ? r.auto_trade_enabled : next;
      setEnabled(serverEnabled);
      if (onChanged) onChanged();
    } catch (e) {
      setErr(e?.detail || e?.message || 'Toggle failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={toggle}
        disabled={busy}
        aria-pressed={enabled}
        aria-label={`Auto-trade ${enabled ? 'on' : 'off'} for ${ticker}`}
        title={enabled
          ? `Auto-trade is ON for ${ticker}. Click to pause new auto-trades for this ticker.`
          : `Auto-trade is OFF for ${ticker}. Click to allow new auto-trades.`}
        className={`text-xs px-2 py-1 rounded border transition-colors disabled:opacity-50 ${
          enabled
            ? 'bg-emerald-900/40 border-emerald-700 text-emerald-300 hover:bg-emerald-900/60'
            : 'bg-gray-800 border-gray-700 text-gray-400 hover:bg-gray-700'
        }`}
      >
        🤖 Auto-trade: {enabled ? 'ON' : 'OFF'}
      </button>
      {err && <span className="text-[10px] text-red-400" role="alert">{err}</span>}
    </div>
  );
}


// ---------- Trading Panel: account snapshot + positions + recent orders ----------
function TradingPanel({ ticker, reloadToken }) {
  const [account, setAccount] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [actionInfo, setActionInfo] = useState(null);
  const [busy, setBusy] = useState({});  // r42 fix #1.17: per-key in-flight guard
  const [loadFailed, setLoadFailed] = useState(false);

  const load = useCallback(async () => {
    // r42 fix #1.22: distinguish "no data" (empty arrays) from "fetch failed"
    // by tracking each leg independently and surfacing a banner when
    // every leg failed.
    const results = await Promise.allSettled([
      api.get('/api/trading/account'),
      api.get('/api/trading/positions'),
      api.get('/api/trading/orders?status=all&limit=100'),
    ]);
    const [a, p, o] = results;
    if (a.status === 'fulfilled') setAccount(a.value);
    if (p.status === 'fulfilled') setPositions(p.value || []);
    if (o.status === 'fulfilled') setOrders(o.value || []);
    const allFailed = results.every(r => r.status === 'rejected');
    setLoadFailed(allFailed);
    if (allFailed) {
      setError(a.reason?.detail || a.reason?.message || 'Trading API unavailable');
    } else {
      setError(null);
    }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      load();
    }, 30000);
    const onPush = () => load();
    window.addEventListener('app:trade_closed', onPush);
    window.addEventListener('app:trade_opened', onPush);
    window.addEventListener('app:resync', onPush);
    return () => {
      clearInterval(iv);
      window.removeEventListener('app:trade_closed', onPush);
      window.removeEventListener('app:trade_opened', onPush);
      window.removeEventListener('app:resync', onPush);
    };
  }, [load, reloadToken]);

  const setBusyKey = (k, v) => setBusy(b => ({ ...b, [k]: v }));

  // r49 UX-P0: replace native confirm() with undoable toast — staged action,
  // 4s undo window, friendly error feedback. No more "did I mean to click that?"
  const closePos = (sym, p) => {
    if (busy[`close:${sym}`]) return;
    const notional = p?.qty != null && p?.current_price != null ? Math.abs(p.qty * p.current_price) : null;
    const label = `Closing ${Math.abs(p?.qty || 0)} ${sym}${notional ? ` ≈ $${notional.toFixed(0)}` : ''} — undo within 4s`;
    stageAction({
      label, delayMs: 4000, kind: 'warn',
      onConfirm: async () => {
        setBusyKey(`close:${sym}`, true); setActionError(null); setActionInfo(null);
        try {
          await api.post(`/api/trading/close/${sym}`);
          toast({ msg: `Close submitted for ${sym}`, kind: 'success', duration: 3000 });
          pushNotification({ severity: 'info', category: 'manual_close', message: `Manually closed ${sym}` });
          await load();
        } catch (e) {
          setActionError(`Close ${sym} failed: ${friendlyError(e)}`);
          toast({ msg: `Close ${sym} failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
        } finally { setBusyKey(`close:${sym}`, false); }
      },
    });
  };
  const cancelOrd = (id, label) => {
    if (busy[`cxl:${id}`]) return;
    stageAction({
      label: `Cancelling order ${label || id} — undo within 3s`,
      delayMs: 3000, kind: 'warn',
      onConfirm: async () => {
        setBusyKey(`cxl:${id}`, true); setActionError(null); setActionInfo(null);
        try {
          // r53b: backend now polls Alpaca for terminal status before returning.
          // Use the actual final status to message the operator clearly — the
          // prior code unconditionally toasted "cancelled" even when Alpaca
          // still had the order in `pending_cancel`, leaving the operator
          // confused when the order didn't disappear from the Working tab.
          const res = await api.delete(`/api/trading/orders/${id}`);
          const status = String(res?.status || 'cancelled').toLowerCase();
          if (status === 'canceled' || status === 'cancelled') {
            toast({ msg: 'Order cancelled', kind: 'success', duration: 3000 });
          } else if (status === 'filled') {
            toast({ msg: `Order filled before cancel could process`, kind: 'warn', duration: 5000 });
          } else if (status === 'pending_cancel') {
            toast({
              msg: 'Cancel sent — Alpaca still processing (refresh in a moment)',
              kind: 'warn', duration: 5000,
            });
          } else if (status === 'already_terminal') {
            toast({ msg: 'Order was already cancelled or filled', kind: 'success', duration: 3000 });
          } else {
            toast({ msg: `Cancel: status=${status}`, kind: 'info', duration: 4000 });
          }
          // Optimistic UI: drop the order from the displayed list immediately
          // so the user sees instant feedback even if Alpaca's GET /orders
          // is still serving the pre-cancel snapshot.
          setOrders((prev) => (prev || []).filter((o) => o.id !== id));
          // Then reconcile via load() and again 1.5s later to catch Alpaca's
          // settlement window for stubborn orders.
          await load();
          setTimeout(() => { try { load(); } catch (_) {} }, 1500);
        } catch (e) {
          setActionError(`Cancel failed: ${friendlyError(e)}`);
          toast({ msg: `Cancel failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
        } finally { setBusyKey(`cxl:${id}`, false); }
      },
    });
  };

  // r53g: Close-All button — flatten every open position via the
  // existing /api/trading/close-all backend (which routes bot-managed
  // rows through force_close_trade for correct lifecycle handling).
  // Uses the same staged-undo pattern as single Close so a misclick
  // can be aborted within the 4s window.
  const closeAll = () => {
    if (busy['close:ALL']) return;
    if (!positions || positions.length === 0) {
      toast({ msg: 'No open positions to close', kind: 'info', duration: 2500 });
      return;
    }
    const totalNotional = positions.reduce(
      (a, p) => a + Math.abs((p.qty || 0) * (p.current_price || p.avg_entry_price || 0)),
      0,
    );
    const label = `Closing ALL ${positions.length} positions ≈ $${totalNotional.toFixed(0)} — undo within 4s`;
    stageAction({
      label, delayMs: 4000, kind: 'warn',
      onConfirm: async () => {
        setBusyKey('close:ALL', true); setActionError(null); setActionInfo(null);
        try {
          const res = await api.post('/api/trading/close-all');
          const closed = res?.closed_managed ?? 0;
          const errs = (res?.summary?.errors || []).length + (res?.summary?.broker_errors || []).length;
          if (errs > 0) {
            toast({
              msg: `Close-all: ${closed} ok, ${errs} error(s) — see alerts`,
              kind: 'warn', duration: 6000,
            });
          } else {
            toast({ msg: `Close-all: flattened ${closed} positions`, kind: 'success', duration: 4000 });
          }
          pushNotification({ severity: 'info', category: 'manual_close_all', message: `Manually flattened ${closed} positions` });
          await load();
          setTimeout(() => { try { load(); } catch (_) {} }, 1500);
        } catch (e) {
          setActionError(`Close-all failed: ${friendlyError(e)}`);
          toast({ msg: `Close-all failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
        } finally { setBusyKey('close:ALL', false); }
      },
    });
  };

  if (!account && !error) {
    return null; // Trading not configured — hide silently
  }
  if (loadFailed && !account) {
    return (
      <div role="alert" className="bg-red-500/10 border border-red-500/40 rounded-lg p-4 text-xs text-red-200">
        Paper trading unavailable: {error}{' '}
        <button onClick={load} className="underline">Retry</button>
      </div>
    );
  }

  const tickerPositions = positions.filter(p => p.symbol === ticker);
  const otherPositions = positions.filter(p => p.symbol !== ticker);

  const totalUnrealized = positions.reduce((a, p) => a + (p.unrealized_pl || 0), 0);
  const totalCost = positions.reduce((a, p) => a + (p.avg_entry_price * Math.abs(p.qty)), 0);
  const totalUnrealizedPct = totalCost > 0 ? (totalUnrealized / totalCost) * 100 : 0;
  const cashPct = account?.equity > 0 ? (account.cash / account.equity) * 100 : 0;
  const deployedPct = 100 - cashPct;

  return (
    <div className="surface rounded-2xl p-5 shadow-xl">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="text-xl font-bold flex items-center gap-2">
            <span>📒</span>
            <span>Paper Trading</span>
          </h3>
          {account?.paper && <span className="pill pill-warn">Paper</span>}
          {account?.status && <span className="pill">{account.status.replace('AccountStatus.', '')}</span>}
          {positions.length > 0 && (
            <span className={`pill ${totalUnrealized >= 0 ? 'pill-success' : 'pill-danger'}`}>
              Unrealized {totalUnrealized >= 0 ? '+' : ''}${totalUnrealized.toFixed(2)} ({totalUnrealizedPct.toFixed(2)}%)
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* r53g: flatten everything in one click — staged-undo 4s window */}
          {positions.length > 0 && (
            <button
              disabled={!!busy['close:ALL']}
              onClick={closeAll}
              title={`Flatten all ${positions.length} open positions`}
              className="text-xs px-2.5 py-1 rounded-md bg-red-500/20 hover:bg-red-500/30 text-red-300 border border-red-500/40 font-semibold disabled:opacity-50">
              {busy['close:ALL'] ? 'Closing all…' : `Close All (${positions.length})`}
            </button>
          )}
          <button onClick={load} className="text-xs app-text-secondary hover:app-text-primary flex items-center gap-1 px-2 py-1 rounded-md hover:bg-white/5">
            <span>↻</span><span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Hero stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <Stat
          label="Equity"
          value={`$${account?.equity?.toLocaleString(undefined, {maximumFractionDigits: 0})}`}
          hint={`Portfolio $${account?.portfolio_value?.toLocaleString(undefined, {maximumFractionDigits: 0})}`}
        />
        <Stat
          label="Cash"
          value={`$${account?.cash?.toLocaleString(undefined, {maximumFractionDigits: 0})}`}
          hint={`${cashPct.toFixed(1)}% of equity`}
        />
        <Stat
          label="Buying Power"
          value={`$${account?.buying_power?.toLocaleString(undefined, {maximumFractionDigits: 0})}`}
          hint={`${deployedPct.toFixed(1)}% deployed`}
        />
        <Stat
          label="Open Positions"
          value={positions.length}
          hint={`${positions.filter(p => p.unrealized_pl > 0).length} winners · ${positions.filter(p => p.unrealized_pl < 0).length} losers`}
        />
      </div>

      {/* Equity deployment progress */}
      <div className="mb-5">
        <div className="flex items-center justify-between text-[11px] app-text-secondary mb-1.5">
          <span className="uppercase tracking-wider">Capital deployment</span>
          <span className="font-mono">{deployedPct.toFixed(1)}% of equity in positions</span>
        </div>
        <div className="h-2 rounded-full overflow-hidden" style={{background: 'var(--surface-border)'}}>
          <div
            className="h-full bg-gradient-to-r from-blue-500 to-indigo-500 transition-all"
            style={{width: `${Math.min(100, deployedPct)}%`}}
          />
        </div>
      </div>

      {/* r52e: P/L reconciliation — single-source-of-truth accounting */}
      <PnLReconciliationPanel />

      {/* r49 UX-P0: Sector exposure widget when N≥2 positions */}
      <SectorExposureWidget positions={positions} />

      {/* r52d: split into Stocks and Options sections. For options, the
          stop/targets/R-multiple/distance-to-stop are denominated in the
          UNDERLYING price (not the option premium) — the prior single
          card mixed units and produced absurd "1237%" distance figures
          on long calls. */}
      <PositionsSections
        positions={positions}
        ticker={ticker}
        error={error}
        busy={busy}
        closePos={closePos}
      />

      {/* r49 UX-P0: Recent orders — filter chips + group-by-parent */}
      <OrdersTable
        orders={orders}
        actionError={actionError}
        actionInfo={actionInfo}
        busy={busy}
        onCancel={cancelOrd}
      />
    </div>
  );
}

// r49: orders table with filter chips + group-by-parent_order_id
function OrdersTable({ orders, actionError, actionInfo, busy, onCancel }) {
  const [filter, setFilter] = usePersistentState('ordersFilter', 'working');
  const [grouped, setGrouped] = usePersistentState('ordersGrouped', true);
  const [tickerFilter, setTickerFilter] = useState('');

  const normStatus = (s) => String(s || '').replace(/^OrderStatus\./, '').toLowerCase();
  // r52c: pending_cancel is in-flight (still holds qty as held_for_orders),
  // so it belongs in "Working" — not Cancelled. Same for held / done_for_day
  // / accepted_for_bidding (rare states that still tie up qty).
  // r53 fix (Tier-1 #9): include `stopped`, `suspended`, `calculated` —
  // Alpaca surfaces these and they all tie up qty as held_for_orders. Prior
  // version classified them as neither working/filled/cancelled, so the
  // orders silently disappeared from every tab.
  const isWorking = (s) => ['new', 'accepted', 'pending_new', 'partially_filled', 'pending_replace', 'replaced', 'pending_cancel', 'held', 'done_for_day', 'accepted_for_bidding', 'stopped', 'suspended', 'calculated'].includes(s);
  const isFilled = (s) => s === 'filled' || s === 'partially_filled';
  const isCancelled = (s) => ['canceled', 'cancelled', 'rejected', 'expired'].includes(s);

  const filteredOrders = useMemo(() => {
    let arr = orders || [];
    if (filter === 'working') arr = arr.filter(o => isWorking(normStatus(o.status)));
    else if (filter === 'filled') arr = arr.filter(o => isFilled(normStatus(o.status)));
    else if (filter === 'cancelled') arr = arr.filter(o => isCancelled(normStatus(o.status)));
    if (tickerFilter) arr = arr.filter(o => String(o.symbol || '').toLowerCase().includes(tickerFilter.toLowerCase()));
    return arr;
  }, [orders, filter, tickerFilter]);

  // Group by parent_order_id (legs roll up under their bracket parent)
  const groups = useMemo(() => {
    if (!grouped) return null;
    const byId = new Map();
    const out = [];
    filteredOrders.forEach(o => {
      const parentId = o.legs ? o.id : (o.parent_order_id || null);
      if (parentId && parentId === o.id) {
        // this is the parent
        if (!byId.has(o.id)) byId.set(o.id, { parent: o, children: [] });
        else byId.get(o.id).parent = o;
        out.push(byId.get(o.id));
      } else if (parentId) {
        if (!byId.has(parentId)) byId.set(parentId, { parent: null, children: [], parentId });
        byId.get(parentId).children.push(o);
        if (!out.find(g => g.parentId === parentId || g.parent?.id === parentId)) out.push(byId.get(parentId));
      } else {
        out.push({ parent: o, children: [], parentId: o.id });
      }
    });
    return out;
  }, [filteredOrders, grouped]);

  const counts = {
    all: orders?.length || 0,
    working: (orders || []).filter(o => isWorking(normStatus(o.status))).length,
    filled: (orders || []).filter(o => isFilled(normStatus(o.status))).length,
    cancelled: (orders || []).filter(o => isCancelled(normStatus(o.status))).length,
  };

  const renderRow = (o, isChild = false) => {
    const side = String(o.side || '').replace(/^OrderSide\./, '').toUpperCase();
    const isBuy = side === 'BUY';
    const status = normStatus(o.status);
    const cancellable = isWorking(status);
    const submitted = parseServerDate(o.submitted_at);
    // r53b: include date alongside time. Same-day orders get "HH:MM:SS";
    // older orders show "MMM D HH:MM" so the operator can tell at a
    // glance which 6-day-old order they're looking at.
    const submittedLabel = (() => {
      if (!submitted) return '—';
      const now = new Date();
      const sameDay = (
        submitted.getFullYear() === now.getFullYear()
        && submitted.getMonth() === now.getMonth()
        && submitted.getDate() === now.getDate()
      );
      if (sameDay) {
        return submitted.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      }
      const dt = submitted.toLocaleDateString([], { month: 'short', day: 'numeric' });
      const tm = submitted.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      return `${dt} ${tm}`;
    })();
    // r53b: surface the order's working price (limit, stop, or filled-avg).
    // Working stops show "stop $X.XX"; working limits show "limit $X.XX";
    // stop-limit shows both. Filled orders show their fill price.
    const typeUpper = String(o.type || '').replace(/^OrderType\./, '').toUpperCase();
    const fmtPx = (n) => (n != null && Number.isFinite(Number(n)))
      ? `$${Number(n).toFixed(2)}` : null;
    const limitTxt = fmtPx(o.limit_price);
    const stopTxt = fmtPx(o.stop_price);
    const filledTxt = fmtPx(o.filled_avg_price);
    let priceCell = '—';
    if (typeUpper.includes('STOP_LIMIT') && stopTxt && limitTxt) {
      priceCell = `stop ${stopTxt} → lim ${limitTxt}`;
    } else if (typeUpper.includes('LIMIT') && limitTxt) {
      priceCell = `limit ${limitTxt}`;
    } else if (typeUpper.includes('STOP') && stopTxt) {
      priceCell = `stop ${stopTxt}`;
    } else if (filledTxt) {
      priceCell = `@ ${filledTxt}`;
    } else if (typeUpper === 'MARKET') {
      priceCell = 'market';
    }
    return (
      <tr key={o.id} className={`border-b app-border-soft last:border-0 hover:bg-white/3 ${isChild ? 'app-text-muted' : ''}`}>
        <td className="py-2 px-3 font-semibold font-mono">
          {isChild && <span className="opacity-50">↳ </span>}
          {/* r53f: click symbol → open underlying chart. Stocks: symbol
              IS the ticker. Options: extract leading letters from OCC. */}
          {(() => {
            const s = o.symbol || '';
            const m = String(s).match(/^[A-Z]+/);
            const isOcc = s.length >= 13 && /\d/.test(s);
            const ticker = isOcc ? (m ? m[0] : null) : s;
            if (!ticker) return s;
            return (
              <button
                onClick={() => window.dispatchEvent(new CustomEvent('app:open-chart', { detail: { ticker } }))}
                title={isOcc ? `Open chart for underlying ${ticker}` : 'Open chart'}
                className="hover:text-blue-300 hover:underline cursor-pointer text-left"
              >{s}</button>
            );
          })()}
        </td>
        <td className={`text-right py-2 px-3 font-semibold ${isBuy ? 'text-emerald-400' : 'text-red-400'}`}>
          <DirIcon dir={side} className="text-xs mr-0.5" />{side || '—'}
        </td>
        <td className="text-right py-2 px-3 font-mono">{o.qty}</td>
        <td className="text-right py-2 px-3 app-text-muted">{typeUpper}</td>
        <td className="text-right py-2 px-3 font-mono app-text-primary">{priceCell}</td>
        <td className="text-right py-2 px-3 font-mono app-text-secondary">
          {/* r53e: BUY → cost-to-buy ($notional). SELL → realized P/L
              (or expected P/L on working bracket legs). */}
          {(() => {
            const notional = o.notional_usd;
            const pl = o.pl_usd;
            const filled = isFilled(status);
            if (isBuy) {
              if (notional == null) return '—';
              return (
                <span className={filled ? 'app-text-primary' : 'app-text-muted'}>
                  {filled ? '' : '~'}${Math.round(notional).toLocaleString()}
                </span>
              );
            }
            // SELL
            if (pl == null) {
              return notional != null
                ? <span className="app-text-muted">~${Math.round(notional).toLocaleString()}</span>
                : '—';
            }
            const cls = pl >= 0 ? 'text-emerald-400' : 'text-red-400';
            const sign = pl >= 0 ? '+' : '−';
            return (
              <span className={cls} title={o.pl_basis_entry ? `entry $${o.pl_basis_entry}` : ''}>
                {filled ? '' : '~'}{sign}${Math.abs(pl).toFixed(2)}
              </span>
            );
          })()}
        </td>
        <td className="text-right py-2 px-3 app-text-secondary">{status.replace(/_/g, ' ')}</td>
        <td className="text-right py-2 px-3 app-text-muted font-mono" title={submitted ? submitted.toLocaleString() : ''}>{submittedLabel}</td>
        <td className="text-right py-2 px-3">
          {cancellable ? (
            <button
              disabled={!!busy[`cxl:${o.id}`]}
              aria-label={`Cancel order ${o.symbol}`}
              onClick={() => onCancel(o.id, `${o.symbol} ${side}`)}
              className="text-[10px] px-2 py-0.5 rounded-md bg-white/5 hover:bg-white/10 app-text-secondary disabled:opacity-50 border app-border">
              {busy[`cxl:${o.id}`] ? '…' : 'Cancel'}
            </button>
          ) : null}
        </td>
      </tr>
    );
  };

  return (
    <CollapsibleSection
      title="Recent Orders"
      count={(orders || []).length}
      defaultOpen={counts.working > 0}
      maxHeight={460}
    >
      {(actionError || actionInfo) && (
        <div role={actionError ? 'alert' : 'status'} className={`mb-2 text-xs px-2 py-1 rounded-md ${actionError ? 'bg-red-500/10 border border-red-500/40 text-red-200' : 'bg-emerald-500/10 border border-emerald-500/40 text-emerald-200'}`}>
          {actionError || actionInfo}
        </div>
      )}
      <div className="flex items-center gap-1 mb-2 flex-wrap">
        {[
          { k: 'working', label: `Working (${counts.working})` },
          { k: 'filled', label: `Filled (${counts.filled})` },
          { k: 'cancelled', label: `Cancelled (${counts.cancelled})` },
          { k: 'all', label: `All (${counts.all})` },
        ].map(t => (
          <button
            key={t.k}
            onClick={() => setFilter(t.k)}
            aria-pressed={filter === t.k}
            className={`text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider ${
              filter === t.k
                ? 'bg-blue-500/25 border border-blue-500/50 text-blue-200'
                : 'border app-border-soft app-text-muted hover:app-text-primary'
            }`}
          >{t.label}</button>
        ))}
        <input
          value={tickerFilter}
          onChange={e => setTickerFilter(e.target.value)}
          placeholder="Filter by ticker"
          className="ml-2 bg-transparent border app-border-soft rounded px-2 py-0.5 text-[10px] w-32"
          aria-label="Filter orders by ticker"
        />
        <label className="ml-auto flex items-center gap-1 text-[10px] app-text-muted cursor-pointer">
          <input type="checkbox" checked={grouped} onChange={e => setGrouped(e.target.checked)} className="accent-blue-500" />
          Group by bracket
        </label>
      </div>
      {filteredOrders.length === 0 ? (
        <div className="text-center text-sm app-text-muted italic py-4">No matching orders.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-xs">
            <thead className="sticky top-0 app-bg-surface-solid z-10">
              <tr className="app-text-muted border-b app-border">
                <th className="text-left py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Symbol</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Side</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Qty</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Type</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Price</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]" title="BUY: cost (qty × price). SELL: realized P/L (or expected on working)">Cost / P&amp;L</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Status</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Submitted</th>
                <th className="py-2 px-3"></th>
              </tr>
            </thead>
            <tbody>
              {grouped && groups
                ? groups.map(g => (
                    <React.Fragment key={g.parent?.id || g.parentId}>
                      {g.parent && renderRow(g.parent, false)}
                      {g.children.map(c => renderRow(c, true))}
                    </React.Fragment>
                  ))
                : filteredOrders.map(o => renderRow(o, false))}
            </tbody>
          </table>
        </div>
      )}
    </CollapsibleSection>
  );
}

// ---------- Login screen ----------
// Shown when the backend requires APP_API_KEY and localStorage doesn't have
// one. On submit, we probe /api/health with the key — it always requires
// auth when auth is configured, so a 200 means the key is valid.
function LoginScreen({ onSuccess }) {
  const [key, setKey] = useState('');
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const submit = async (e) => {
    e?.preventDefault();
    if (!key.trim()) return;
    setBusy(true); setErr(null);
    try {
      // r42 fix #2.30: probe /api/health (cheap) instead of overview (scans the
      // whole watchlist). Also doubles as a liveness check.
      const r = await fetch(`${API_BASE}/api/health`, {
        headers: { 'X-API-Key': key.trim() },
      });
      if (r.status === 401) { setErr('Invalid key'); setBusy(false); return; }
      if (!r.ok) { setErr(`Server ${r.status}`); setBusy(false); return; }
      setApiKey(key.trim(), remember);
      onSuccess();
    } catch (e) {
      setErr('Network error');
      setBusy(false);
    }
  };
  return (
    <div className="h-screen flex items-center justify-center p-6">
      <form onSubmit={submit} className="surface rounded-2xl p-7 w-full max-w-sm shadow-2xl">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-blue-500/20">📈</div>
          <div className="text-base font-bold">StockTA</div>
        </div>
        <div className="text-xs app-text-secondary uppercase tracking-[0.14em] font-semibold mb-1.5">Access key</div>
        <input
          type="password"
          value={key}
          onChange={e => setKey(e.target.value)}
          placeholder="Paste your API key"
          autoFocus
          aria-label="API access key"
          className="w-full bg-gray-900/60 border border-white/10 rounded-lg px-3 py-2.5 text-sm placeholder-gray-500 focus:border-blue-500/70 font-mono"
        />
        <label className="mt-3 flex items-center gap-2 text-[11px] app-text-secondary select-none cursor-pointer">
          <input
            type="checkbox"
            checked={remember}
            onChange={e => setRemember(e.target.checked)}
            className="accent-blue-500"
          />
          Remember on this device (less secure — survives tab close)
        </label>
        {err && <div className="mt-2 text-xs text-red-400">{err}</div>}
        <button
          type="submit"
          disabled={busy || !key.trim()}
          className="mt-4 w-full py-2.5 rounded-lg bg-gradient-to-b from-blue-500 to-blue-600 hover:from-blue-400 hover:to-blue-500 disabled:opacity-50 text-sm font-semibold shadow-lg shadow-blue-500/20"
        >
          {busy ? 'Verifying…' : 'Unlock'}
        </button>
        <div className="mt-4 text-[10px] app-text-muted leading-relaxed">
          By default your key is held in this tab's sessionStorage and is cleared when the tab closes. Tick "Remember" to persist it to localStorage across sessions.
        </div>
      </form>
    </div>
  );
}

// ---------- Main App ----------
// Thin dispatcher: gate on auth and only mount the real UI once authenticated.
// Keeping hooks in two components avoids the "rendered more hooks than
// previous render" crash when the auth guard is in the same component.
function TargetHitToasts() {
  const [toasts, setToasts] = useState([]);

  // Request browser-notification permission once per session (lazy — only
  // after the first target_hit event arrives, which is user-intent-adjacent).
  const requestedRef = useRef(false);

  useEffect(() => {
    const onHit = (ev) => {
      const d = ev.detail || {};
      const id = `${d.trade_id}-${d.level}-${Date.now()}`;
      const toast = {
        id, ticker: d.ticker, level: d.level,
        price: d.price, newStop: d.new_stop,
        asset: d.asset_type,
      };
      setToasts(ts => [...ts, toast]);
      // Auto-dismiss after 8s
      setTimeout(() => setToasts(ts => ts.filter(t => t.id !== id)), 8000);

      // Browser notification (only when tab is backgrounded — avoids noise)
      if (document.visibilityState === 'hidden') {
        if (!requestedRef.current && 'Notification' in window && Notification.permission === 'default') {
          try { Notification.requestPermission(); } catch (_) {}
          requestedRef.current = true;
        }
        if ('Notification' in window && Notification.permission === 'granted') {
          try {
            new Notification(`${d.ticker} ${d.level} hit @ $${d.price}`, {
              body: d.new_stop ? `Stop trailed to $${d.new_stop}` : undefined,
              tag: `target-${d.trade_id}-${d.level}`,
            });
          } catch (_) {}
        }
      }
    };
    const onClose = (ev) => {
      const d = ev.detail || {};
      const id = `${d.trade_id}-close-${Date.now()}`;
      const pl = Number(d.realized_pl || 0);
      const win = pl > 0;
      const toast = {
        id,
        kind: 'close',
        ticker: d.ticker,
        status: d.status,        // closed_target | closed_stop | closed_reverse | closed_stale | closed_manual
        reason: d.reason,
        pl,
        win,
        asset: d.asset_type,
      };
      setToasts(ts => [...ts, toast]);
      // Closes hang around longer than trails — operator wants to read why
      setTimeout(() => setToasts(ts => ts.filter(t => t.id !== id)), 12000);

      if (document.visibilityState === 'hidden') {
        if (!requestedRef.current && 'Notification' in window && Notification.permission === 'default') {
          try { Notification.requestPermission(); } catch (_) {}
          requestedRef.current = true;
        }
        if ('Notification' in window && Notification.permission === 'granted') {
          try {
            const sign = win ? '+' : '';
            new Notification(`${d.ticker} closed (${d.status}) · ${sign}$${pl.toFixed(2)}`, {
              body: d.reason ? String(d.reason).slice(0, 120) : undefined,
              tag: `close-${d.trade_id}`,
            });
          } catch (_) {}
        }
      }
    };
    window.addEventListener('app:target_hit', onHit);
    window.addEventListener('app:trade_closed', onClose);
    return () => {
      window.removeEventListener('app:target_hit', onHit);
      window.removeEventListener('app:trade_closed', onClose);
    };
  }, []);

  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-xs">
      {toasts.map(t => {
        if (t.kind === 'close') {
          const borderClass = t.win ? 'border-emerald-500/40' : 'border-rose-500/40';
          const pillClass = t.win ? 'pill-success' : 'pill-danger';
          const sign = t.win ? '+' : '';
          return (
            <div key={t.id} className={`surface rounded-lg p-3 border ${borderClass} shadow-xl animate-in`}>
              <div className="flex items-center gap-2 mb-1">
                <span className="text-lg">{t.win ? '✅' : '🛑'}</span>
                <span className="font-bold">{t.ticker}</span>
                <span className={`pill ${pillClass} text-[10px]`}>{t.status}</span>
                <span className="text-[10px] app-text-muted uppercase">{t.asset}</span>
              </div>
              <div className="text-xs font-mono mb-1">
                P/L {sign}${(t.pl || 0).toFixed(2)}
              </div>
              {t.reason ? (
                <div className="text-[11px] app-text-muted">{String(t.reason).slice(0, 120)}</div>
              ) : null}
            </div>
          );
        }
        return (
          <div key={t.id} className="surface rounded-lg p-3 border border-emerald-500/40 shadow-xl animate-in">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-lg">🎯</span>
              <span className="font-bold">{t.ticker}</span>
              <span className="pill pill-success text-[10px]">{t.level}</span>
              <span className="text-[10px] app-text-muted uppercase">{t.asset}</span>
            </div>
            <div className="text-xs font-mono">
              hit @ ${t.price}{t.newStop ? ` · stop→$${t.newStop}` : ''}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState([]);  // [{role, content}]
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [configured, setConfigured] = useState(null);  // null=unknown, true/false
  const scrollRef = useRef(null);

  // One-shot: check whether ANTHROPIC_API_KEY is set on the server.
  useEffect(() => {
    if (!open || configured !== null) return;
    api.get('/api/chat/status').then(d => setConfigured(!!d.configured))
       .catch(() => setConfigured(false));
  }, [open, configured]);

  // r42 fix #2.22: Escape closes chat widget.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, busy]);

  async function send() {
    const trimmed = input.trim();
    if (!trimmed || busy) return;
    setErr('');
    const next = [...messages, { role: 'user', content: trimmed }];
    setMessages(next);
    setInput('');
    setBusy(true);
    // Add an empty assistant bubble we stream into.
    setMessages(m => [...m, { role: 'assistant', content: '' }]);

    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ messages: next }),
      });
      if (res.status === 401) { on401(); throw new Error('unauthorized'); }
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        throw new Error(txt || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          const line = chunk.split('\n').find(l => l.startsWith('data:'));
          if (!line) continue;
          try {
            const evt = JSON.parse(line.slice(5).trim());
            if (evt.error) { setErr(evt.error); break; }
            if (evt.delta) {
              setMessages(m => {
                const copy = m.slice();
                const last = copy[copy.length - 1];
                if (last && last.role === 'assistant') copy[copy.length - 1] = { ...last, content: last.content + evt.delta };
                return copy;
              });
            }
          } catch (_) { /* ignore malformed SSE line */ }
        }
      }
    } catch (e) {
      setErr(String(e && e.message || e));
    } finally {
      setBusy(false);
    }
  }

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  }

  return (
    <>
      {!open && (
        <button
          onClick={() => setOpen(true)}
          className="fixed bottom-5 right-5 z-40 w-12 h-12 rounded-full app-surface app-border border shadow-xl flex items-center justify-center text-xl hover:scale-105 transition"
          title="Ask Claude about trades & config"
          aria-label="Open chat"
        >💬</button>
      )}
      {open && (
        <div className="fixed bottom-5 right-5 z-40 w-[min(420px,calc(100vw-1.5rem))] h-[min(620px,calc(100vh-2rem))] app-surface app-border border rounded-2xl shadow-2xl flex flex-col overflow-hidden">
          <div className="flex items-center justify-between px-3 py-2 app-border border-b">
            <div className="flex items-center gap-2">
              <div className="w-6 h-6 rounded bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-xs">🤖</div>
              <div className="text-sm font-semibold">Ask Claude</div>
              {configured === false && <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/15 text-red-400">not configured</span>}
            </div>
            <div className="flex gap-1">
              {messages.length > 0 && <button onClick={() => { setMessages([]); setErr(''); }} className="text-xs app-text-secondary hover:app-text px-2 py-1" title="New chat">↻</button>}
              <button onClick={() => setOpen(false)} className="text-xs app-text-secondary hover:app-text px-2 py-1" aria-label="Close">✕</button>
            </div>
          </div>
          <div ref={scrollRef} className="flex-1 overflow-y-auto scrollbar-thin px-3 py-2 space-y-2 text-sm">
            {messages.length === 0 && (
              <div className="app-text-secondary text-xs leading-relaxed">
                <div className="mb-2">Ask anything about your bot:</div>
                <ul className="space-y-1 list-disc ml-4">
                  <li>"Why did trade #29 close as reverse?"</li>
                  <li>"What's my current config for options?"</li>
                  <li>"Which tickers are in my candidate pool today?"</li>
                  <li>"Summarize my last 5 losing trades."</li>
                </ul>
                {configured === false && (
                  <div className="mt-3 p-2 rounded bg-yellow-500/10 border border-yellow-500/30 text-yellow-300">
                    Set <code>ANTHROPIC_API_KEY</code> in the Cloud Run env vars to enable chat.
                  </div>
                )}
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
                <div className={`max-w-[85%] px-3 py-2 rounded-lg whitespace-pre-wrap ${m.role === 'user' ? 'bg-blue-500/15 border border-blue-500/30' : 'app-surface-alt app-border border'}`}>
                  {m.content || (busy && i === messages.length - 1 ? <span className="app-text-secondary">…</span> : '')}
                </div>
              </div>
            ))}
            {err && <div className="text-xs text-red-400 px-2">Error: {err}</div>}
          </div>
          <div className="app-border border-t p-2 flex gap-2">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={onKey}
              placeholder={configured === false ? 'Chat disabled — set ANTHROPIC_API_KEY' : 'Ask about trades, config, positions…'}
              disabled={busy || configured === false}
              rows={1}
              className="flex-1 resize-none app-surface-alt app-border border rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500/40"
            />
            <button
              onClick={send}
              disabled={busy || !input.trim() || configured === false}
              className="px-3 py-1.5 rounded-lg bg-blue-500 text-white text-sm disabled:opacity-40"
            >{busy ? '…' : 'Send'}</button>
          </div>
        </div>
      )}
    </>
  );
}

// ---------- Safety banner ----------
// r42 fix #0.6/0.7: surface freeze/kill/broker-down/BP-breaker/PDT state
// at the top of the dashboard. Backend exposes them on
// /api/trading/auto/status; we poll every 30s and on `app:resync`.
function SafetyBanner() {
  const [s, setS] = useState(null);
  const [acct, setAcct] = useState(null);
  const refresh = useCallback(async () => {
    try {
      const [stat, account] = await Promise.all([
        api.get('/api/trading/auto/status'),
        api.get('/api/trading/account').catch(() => null),
      ]);
      setS(stat); setAcct(account);
    } catch (e) { /* silent — banner is best-effort */ }
  }, []);
  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 30000);
    const onSync = () => refresh();
    const onAlert = () => refresh();
    window.addEventListener('app:resync', onSync);
    window.addEventListener('app:alert', onAlert);
    return () => {
      clearInterval(iv);
      window.removeEventListener('app:resync', onSync);
      window.removeEventListener('app:alert', onAlert);
    };
  }, [refresh]);
  if (!s) return null;
  const banners = [];
  if (s.kill_switch) banners.push({ kind: 'critical', label: 'KILL SWITCH ENGAGED', detail: s.kill_reason || 'auto-trader manually killed; positions flattened' });
  if (s.freeze_reason) banners.push({ kind: 'critical', label: 'TRADING FROZEN', detail: s.freeze_reason });
  if (s.broker_down) banners.push({ kind: 'critical', label: 'BROKER UNAVAILABLE', detail: `Alpaca returned 5xx; new entries paused${s.broker_down_until ? ` until ${parseServerDate(s.broker_down_until)?.toLocaleTimeString() || s.broker_down_until}` : ''}` });
  if (s.bp_breaker_active) banners.push({ kind: 'warning', label: 'BUYING POWER EXHAUSTED', detail: `BP circuit breaker tripped${s.bp_breaker_until ? ` until ${parseServerDate(s.bp_breaker_until)?.toLocaleTimeString() || s.bp_breaker_until}` : ''}` });
  // PDT — only flag for live margin <$25k, where it actually blocks entries.
  const isLive = acct && acct.account_blocked === false && (acct.pattern_day_trader === false || acct.pattern_day_trader === undefined);
  const isPaper = acct && (acct.id || '').toString().toLowerCase().includes('paper');
  const equity = acct ? Number(acct.equity || 0) : 0;
  const cfg = s.config || {};
  // Show PDT warning when not on paper, equity < $25k, and pdt_enforce is OFF.
  if (!isPaper && equity > 0 && equity < 25000 && cfg.pdt_enforce === false) {
    banners.push({
      kind: 'critical',
      label: 'PDT GATE DISABLED ON LIVE MARGIN ACCOUNT',
      detail: `Equity $${equity.toFixed(0)} < $25k. Enable "PDT enforce" in Auto-Trader Config or you risk a 90-day PDT lockout. (current day-trades in trailing 5d: ${s.pdt_count})`,
    });
  } else if (s.pdt_would_block) {
    banners.push({
      kind: 'warning',
      label: 'PDT THRESHOLD REACHED',
      detail: `${s.pdt_count} day-trades in trailing 5d ≥ 4. New same-day round-trips will be blocked when PDT enforce is on.`,
    });
  }
  if (s.adopted_count > 0 && cfg.auto_promote_adopted === false) {
    banners.push({
      kind: 'warning',
      label: `${s.adopted_count} ADOPTED POSITION${s.adopted_count > 1 ? 'S' : ''} UNMANAGED`,
      detail: 'External positions exist as "adopted" but the bot is not managing them (no SL/TP). Promote each in the Adopted panel below, or enable "Auto-promote adopted" in Auto-Trader Config.',
    });
  }
  if (banners.length === 0) return null;
  return (
    <div className="space-y-1.5 px-2 sm:px-4 pt-2">
      {banners.map((b, i) => (
        <div
          key={i}
          role="alert"
          className={`rounded-xl border px-3 py-2 flex items-start gap-2 ${
            b.kind === 'critical'
              ? 'bg-red-500/15 border-red-500/40 text-red-100'
              : 'bg-amber-500/15 border-amber-500/40 text-amber-100'
          }`}
        >
          <span aria-hidden className="text-base leading-none mt-0.5">{b.kind === 'critical' ? '⛔' : '⚠️'}</span>
          <div className="flex-1 min-w-0">
            <div className="text-[11px] font-bold uppercase tracking-wider">{b.label}</div>
            <div className="text-xs mt-0.5 break-words">{b.detail}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------- Alerts panel ----------
// r42 fix #0.5: surfaces /api/alerts in a dedicated drawer. Previously
// the alert log was completely invisible in the UI.
function AlertsPanel() {
  const [alerts, setAlerts] = useState([]);
  const [unacked, setUnacked] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(false);
  const [showAcked, setShowAcked] = useState(false);
  const refresh = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const listUrl = showAcked
        ? '/api/alerts?limit=100'
        : '/api/alerts?limit=100&only_unacked=true';
      const [list, count] = await Promise.all([
        api.get(listUrl),
        api.get('/api/alerts/count?since_hours=24'),
      ]);
      setAlerts(Array.isArray(list) ? list : []);
      setUnacked(count?.unacked || 0);
    } catch (e) {
      setErr(e?.detail || e?.message || 'Failed to load alerts');
    } finally { setLoading(false); }
  }, [showAcked]);
  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 30000);
    const onAlert = () => refresh();
    const onSync = () => refresh();
    window.addEventListener('app:alert', onAlert);
    window.addEventListener('app:resync', onSync);
    return () => {
      clearInterval(iv);
      window.removeEventListener('app:alert', onAlert);
      window.removeEventListener('app:resync', onSync);
    };
  }, [refresh]);
  const ackAll = async () => {
    try { await api.post('/api/alerts/ack-all', {}); refresh(); }
    catch (e) { setErr(e?.detail || e?.message || 'Ack failed'); }
  };
  const ackOne = async (id) => {
    try { await api.post(`/api/alerts/${id}/ack`, {}); refresh(); }
    catch (e) { setErr(e?.detail || e?.message || 'Ack failed'); }
  };
  return (
    <div className="surface rounded-2xl border app-border">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-white/3 rounded-2xl"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Alerts</span>
          {unacked > 0 && (
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-red-500 text-white">
              {unacked}
            </span>
          )}
          <span className="text-[10px] app-text-muted uppercase tracking-wider">
            {showAcked ? `${alerts.length} recent` : `${unacked} unacked`}
          </span>
        </div>
        <span className="text-xs app-text-muted">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="px-4 pb-4">
          <div className="flex items-center justify-end gap-2 mb-2">
            <label className="text-[10px] app-text-muted flex items-center gap-1 cursor-pointer mr-auto">
              <input type="checkbox" checked={showAcked} onChange={e => setShowAcked(e.target.checked)} className="accent-blue-500" />
              Show acked
            </label>
            <button onClick={refresh} className="text-[10px] px-2 py-1 rounded surface-soft app-text-secondary">Refresh</button>
            <button onClick={ackAll} disabled={unacked === 0} className="text-[10px] px-2 py-1 rounded bg-blue-500/20 border border-blue-500/40 text-blue-300 disabled:opacity-40">Ack all</button>
          </div>
          {err && <div className="text-xs text-red-400 mb-2">{err}</div>}
          {loading ? (
            <div className="text-xs app-text-muted italic py-4 text-center">Loading…</div>
          ) : alerts.length === 0 ? (
            <div className="text-xs app-text-muted italic py-4 text-center">No alerts in the last 24h.</div>
          ) : (
            <div className="space-y-1 max-h-96 overflow-y-auto scrollbar-thin">
              {alerts.map(a => {
                const sevColor = a.severity === 'critical' || a.severity === 'error'
                  ? 'border-red-500/40 bg-red-500/10 text-red-200'
                  : a.severity === 'warning'
                  ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
                  : 'border-white/10 bg-white/3 app-text-secondary';
                const when = parseServerDate(a.created_at);
                return (
                  <div key={a.id} className={`rounded-lg border px-2 py-1.5 ${sevColor} ${a.acked ? 'opacity-60' : ''}`}>
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-[9px] uppercase font-bold tracking-wider shrink-0">{a.severity}</span>
                        <span className="text-[10px] app-text-muted shrink-0">{a.kind}</span>
                        <span className="text-[10px] app-text-muted shrink-0">{when ? when.toLocaleString() : ''}</span>
                      </div>
                      {!a.acked && (
                        <button onClick={() => ackOne(a.id)} className="text-[9px] px-1.5 py-0.5 rounded surface-soft app-text-muted shrink-0">Ack</button>
                      )}
                    </div>
                    <div className="text-xs mt-1 break-words">{a.message}</div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- Rejected signals panel ----------
// r42 fix #1.25: surfaces /api/trading/auto/skip-counts so the operator
// can see WHY the bot is sitting idle (PDT-blocked, regime gate, sector
// cap, AI veto, etc.). Counters are cumulative since process start; the
// panel shows the most-common reasons sorted by count.
function RejectedSignalsPanel() {
  const [data, setData] = useState({ skips: {}, events: {} });
  const [open, setOpen] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const r = await api.get('/api/trading/auto/skip-counts');
      setData(r || { skips: {}, events: {} });
    } catch (_) {}
  }, []);
  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 60000);
    const onSync = () => refresh();
    window.addEventListener('app:resync', onSync);
    return () => { clearInterval(iv); window.removeEventListener('app:resync', onSync); };
  }, [refresh]);
  const skips = Object.entries(data.skips || {}).sort((a, b) => b[1] - a[1]);
  const events = data.events || {};
  const total = skips.reduce((a, [, v]) => a + v, 0);
  const opened = (events.opened || 0) + (events.opened_call || 0) + (events.opened_put || 0);
  return (
    <div className="surface rounded-2xl border app-border">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full px-4 py-3 flex items-center justify-between text-left hover:bg-white/3 rounded-2xl"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Why isn't the bot trading?</span>
          <span className="text-[10px] app-text-muted uppercase tracking-wider">{opened} opened · {total} signals rejected</span>
        </div>
        <span className="text-xs app-text-muted">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="px-4 pb-4">
          <div className="text-[11px] app-text-muted mb-2 leading-relaxed">
            Cumulative since process start. Counts the gates inside <span className="font-mono">consider_signal</span>;
            top reasons here suggest where to relax thresholds (or where the safety net is doing its job).
          </div>
          {skips.length === 0 ? (
            <div className="text-xs app-text-muted italic py-3 text-center">No rejected signals recorded.</div>
          ) : (
            <ul className="space-y-1 text-xs">
              {skips.slice(0, 25).map(([reason, count]) => (
                <li key={reason} className="flex justify-between items-center px-2 py-1 rounded surface-soft">
                  <span className="font-mono app-text-secondary">{reason.replace(/_/g, ' ')}</span>
                  <span className="font-mono app-text-primary">{count}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- Adopted positions panel ----------
// r42 fix #1.20/1.21: list adopted positions with one-click promote, plus
// the manual sync button. Previously the operator had to run curl.
function AdoptedPanel() {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState({});
  const [err, setErr] = useState(null);
  const [info, setInfo] = useState(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const all = await api.get('/api/trading/auto/trades?limit=200');
      setTrades((all || []).filter(t => t.status === 'adopted'));
    } catch (e) { setErr(e?.detail || e?.message || 'Failed to load trades'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 60000);
    const onTradeOpened = () => refresh();
    const onTradeClosed = () => refresh();
    const onSync = () => refresh();
    window.addEventListener('app:trade_opened', onTradeOpened);
    window.addEventListener('app:trade_closed', onTradeClosed);
    window.addEventListener('app:resync', onSync);
    return () => {
      clearInterval(iv);
      window.removeEventListener('app:trade_opened', onTradeOpened);
      window.removeEventListener('app:trade_closed', onTradeClosed);
      window.removeEventListener('app:resync', onSync);
    };
  }, [refresh]);
  const setRow = (k, v) => setBusy(b => ({ ...b, [k]: v }));
  const sync = async () => {
    if (!confirm('Sync positions with Alpaca?\nThis will adopt any external positions and close-out any DB rows that no longer exist on the broker. No new orders are submitted.')) return;
    setRow('__sync', true); setErr(null); setInfo(null);
    try {
      const r = await api.post('/api/admin/sync-positions', {});
      const a = (r?.adopted || []).length;
      const c = (r?.closed_external || []).length;
      setInfo(`Sync done: ${a} adopted, ${c} closed-external`);
      refresh();
    } catch (e) {
      setErr(e?.detail || e?.message || 'Sync failed');
    } finally { setRow('__sync', false); }
  };
  const promote = async (ticker) => {
    if (!confirm(`Promote ${ticker} to bot-managed?\nThe bot will compute fresh stop/target levels and submit a stop-loss order to Alpaca. The position will be managed exactly as if the bot had opened it.`)) return;
    setRow(ticker, true); setErr(null); setInfo(null);
    try {
      const r = await api.post(`/api/admin/promote-adopted/${ticker}`, {});
      setInfo(`Promoted ${ticker}: ${r?.note || 'managed by bot'}`);
      refresh();
    } catch (e) {
      setErr(`Promote ${ticker} failed: ${e?.detail || e?.message || 'unknown'}`);
    } finally { setRow(ticker, false); }
  };
  return (
    <div className="surface rounded-2xl border app-border p-4">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Adopted Positions</span>
          <span className="text-[10px] app-text-muted uppercase tracking-wider">
            {trades.length} unmanaged
          </span>
        </div>
        <button
          onClick={sync}
          disabled={!!busy.__sync}
          className="text-[11px] px-2.5 py-1 rounded-md bg-blue-500/20 hover:bg-blue-500/30 text-blue-200 border border-blue-500/40 disabled:opacity-50"
        >
          {busy.__sync ? 'Syncing…' : 'Sync with Alpaca'}
        </button>
      </div>
      <div className="text-[11px] app-text-muted mb-2 leading-relaxed">
        Adopted = external positions that exist on Alpaca but were not opened by the bot. The bot does NOT manage them (no stop, no trail) until promoted. Promote to apply bot-computed stop and target levels.
      </div>
      {err && <div className="text-xs text-red-400 mb-2">{err}</div>}
      {info && <div className="text-xs text-emerald-400 mb-2">{info}</div>}
      {loading ? (
        <div className="text-xs app-text-muted italic py-3 text-center">Loading…</div>
      ) : trades.length === 0 ? (
        <div className="text-xs app-text-muted italic py-3 text-center">No adopted positions.</div>
      ) : (
        <div className="space-y-1.5">
          {trades.map(t => (
            <div key={t.id} className="flex items-center justify-between rounded-lg border app-border-soft px-3 py-2 surface-soft">
              <div className="min-w-0">
                <div className="font-semibold text-sm">{t.ticker}</div>
                <div className="text-[11px] app-text-muted">
                  qty {t.qty} @ ${(t.entry_price ?? 0).toFixed(2)}
                </div>
              </div>
              <button
                onClick={() => promote(t.ticker)}
                disabled={!!busy[t.ticker]}
                className="text-[11px] px-2.5 py-1 rounded-md bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 border border-emerald-500/40 disabled:opacity-50"
              >
                {busy[t.ticker] ? 'Promoting…' : 'Promote'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function App() {
  const [authed, setAuthed] = useState(!!getApiKey());
  useEffect(() => {
    const onUnauth = () => setAuthed(false);
    window.addEventListener('app:unauthorized', onUnauth);
    return () => window.removeEventListener('app:unauthorized', onUnauth);
  }, []);
  if (!authed) return <LoginScreen onSuccess={() => setAuthed(true)} />;
  return <AuthedApp onLogout={() => { setApiKey(''); setAuthed(false); }} />;
}

function AuthedApp({ onLogout }) {
  const [overview, setOverview] = useState([]);
  const [selected, setSelected] = usePersistentState('lastTicker', null);
  const [reloadToken, setReloadToken] = useState(0);
  const [view, setView] = usePersistentState('view', 'charts'); // 'charts' | 'trading'
  const [theme, toggleTheme] = useTheme();
  const [density, setDensity] = useDensity();
  const [mobileWatchOpen, setMobileWatchOpen] = useState(false);
  const [tradingTab, setTradingTab] = usePersistentState('tradingTab', 'overview');
  // r49: ⌘K quick-trade palette
  const [quickOpen, setQuickOpen] = useState(false);

  // Stabilise loadOverview by reading `selected` from a ref instead of a dep.
  // Previously [selected] in the dep array re-created loadOverview on every
  // selection, which (a) re-created onSignalUpdate, (b) re-fired the
  // useLiveQuotes effect, leaking subscribers, and (c) re-installed the
  // setInterval below, leaking polling timers. Ref pattern decouples them.
  const selectedRef = useRef(selected);
  useEffect(() => { selectedRef.current = selected; }, [selected]);

  const inFlightRef = useRef(false);
  const loadOverview = useCallback(async () => {
    if (inFlightRef.current) return;  // dogpile guard — drop tick if previous still pending
    inFlightRef.current = true;
    try {
      const data = await api.get('/api/analysis/overview');
      setOverview(data);
      if (!selectedRef.current && data.length > 0) setSelected(data[0].ticker);
    } catch (e) {
      console.error('Failed to load overview', e);
    } finally {
      inFlightRef.current = false;
    }
  }, []);  // stable identity

  // Live quotes: overlay WS prices on overview; when server re-runs signals,
  // bump reloadToken to nudge the analysis view / overview to refetch.
  // Stable callback (deps: empty) so the WS subscription doesn't churn.
  const onSignalUpdate = useCallback((ticker) => {
    setReloadToken(t => t + 1);
    loadOverview();
  }, [loadOverview]);
  const { quotes: liveQuotes, connected: liveConnected } = useLiveQuotes(onSignalUpdate);

  // Merge live last/bid/ask prices into overview rows (without clobbering change_pct
  // — that's computed against prior close by the server's snapshot).
  // r42 fix #0.4: only tag a row `live: true` when the underlying quote is
  // FRESH and the WS is currently connected. Stale or background-throttled
  // quotes are returned dimmed (`stale: true`) but never as live, so
  // operator never makes a close decision against frozen prices.
  const overviewWithLive = useMemo(() => overview.map(row => {
    const q = liveQuotes[row.ticker];
    if (!q) return row;
    const livePx = q.last || (q.bid && q.ask ? (q.bid + q.ask) / 2 : null);
    if (!livePx) return row;
    const ageMs = q._localTs ? (Date.now() - q._localTs) : Infinity;
    const fresh = liveConnected && ageMs <= QUOTE_STALE_MS;
    return {
      ...row,
      price: Math.round(livePx * 100) / 100,
      live: fresh,
      stale: !fresh,
      quote_age_ms: ageMs,
    };
  }), [overview, liveQuotes, liveConnected]);

  // r42 fix #2.7: visibility-gated polling.
  useEffect(() => {
    loadOverview();
    const iv = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      loadOverview();
    }, 60000);
    const onVis = () => {
      if (document.visibilityState === 'visible') loadOverview();
    };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      clearInterval(iv);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [loadOverview]);

  // r53f: open-chart navigation via window event. Fired by Position
  // cards (Stocks + Options sections) and Recent Orders rows. The
  // option flow extracts the underlying ticker from the OCC symbol
  // ("RMBS260515C00150000" → "RMBS") so a click on an option card
  // opens the underlying's chart, same view the watchlist would.
  React.useEffect(() => {
    const onOpenChart = (e) => {
      const t = (e?.detail?.ticker || '').toUpperCase();
      if (!t) return;
      setSelected(t);
      setView('charts');
      // If the ticker isn't in the watchlist yet, the analysis endpoint
      // will fetch it on-demand; the watchlist sidebar simply won't
      // highlight a row, which is fine.
    };
    window.addEventListener('app:open-chart', onOpenChart);
    return () => window.removeEventListener('app:open-chart', onOpenChart);
  }, [setSelected, setView]);

  // r42 fix #2.19: in-flight guard so a fast double-click doesn't fire two
  // POSTs / DELETEs against the watchlist API.
  const handleAdd = useCallback(async (ticker) => {
    try {
      await api.post('/api/watchlist', { ticker });
      await loadOverview();
      setSelected(ticker);
      toast({ msg: `Added ${ticker} to watchlist`, kind: 'success', duration: 2500 });
    } catch (e) {
      toast({ msg: `Add ${ticker} failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
    }
  }, [loadOverview]);

  const handleRemove = useCallback((ticker) => {
    // r49: replace confirm() with undoable toast
    stageAction({
      label: `Removing ${ticker} from watchlist — undo within 4s`,
      delayMs: 4000, kind: 'warn',
      onConfirm: async () => {
        try {
          await api.delete(`/api/watchlist/${ticker}`);
          await loadOverview();
          if (selectedRef.current === ticker) setSelected(null);
          toast({ msg: `Removed ${ticker}`, kind: 'success', duration: 2000 });
        } catch (e) {
          toast({ msg: `Remove ${ticker} failed: ${friendlyError(e)}`, kind: 'error', duration: 6000 });
        }
      },
    });
  }, [loadOverview]);

  // r49: keyboard shortcuts
  useKeyboard(({ key, cmd, isInput }) => {
    if (cmd && key === 'k' && !isInput) {
      // ⌘K — quick trade palette (always allow even from input via cmd)
      setQuickOpen(o => !o);
      return;
    }
    if (cmd && key === 'k' && isInput) {
      // Allow ⌘K even when typing
      setQuickOpen(o => !o);
      return;
    }
    if (isInput) return;
    if (key === 'g') {
      // 'g' then 'c'/'t' — view toggle
      // Simple version: just toggle on bare g
      setView(v => v === 'charts' ? 'trading' : 'charts');
      return;
    }
    if (key === '?') {
      toast({ msg: '⌘K quick ticket · ⌘I alerts · g toggle view · / focus filter · Esc close', kind: 'info', duration: 5000 });
      return;
    }
    if (key === 'r' && !cmd) {
      loadOverview();
      toast({ msg: 'Refreshed', kind: 'info', duration: 1200 });
    }
  }, [setView, loadOverview]);

  return (
    <ErrorBoundary>
    <div className="h-screen flex flex-col">
      <SkipLink />
      <ToastHost />
      {quickOpen && <QuickTradePalette onClose={() => setQuickOpen(false)} />}
      <header className="surface sticky top-0 z-20 px-3 sm:px-5 py-2 sm:py-2.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 sm:gap-5 min-w-0">
          {/* Mobile watchlist toggle — visible only on small screens, only in charts view */}
          {view === 'charts' && (
            <button
              onClick={() => setMobileWatchOpen(o => !o)}
              className="md:hidden w-9 h-9 rounded-lg surface-soft border app-border flex items-center justify-center app-text-primary"
              aria-label="Open watchlist"
            >
              ☰
            </button>
          )}
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-sm shadow-lg shadow-blue-500/20 shrink-0">📈</div>
            <div className="text-[15px] font-bold tracking-tight app-brand hidden sm:block">StockTA</div>
          </div>
          <nav className="flex surface-soft rounded-xl p-1 text-xs shrink-0">
            {[
              { id: 'charts', label: 'Charts', full: 'Charts & Analysis' },
              { id: 'trading', label: '📒', full: '📒 Trading' },
            ].map(t => (
              <button
                key={t.id}
                onClick={() => setView(t.id)}
                className={`px-2.5 sm:px-3.5 py-1.5 rounded-lg font-medium ${view === t.id ? 'bg-gradient-to-b from-blue-500 to-blue-600 text-white glow-blue' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
              >
                <span className="sm:hidden">{t.label}</span>
                <span className="hidden sm:inline">{t.full}</span>
              </button>
            ))}
          </nav>
        </div>
        <div className="text-xs flex items-center gap-2 sm:gap-3 shrink-0">
          {/* r49: per-feed freshness strip */}
          <div className="hidden lg:block"><FreshnessStrip liveConnected={liveConnected} /></div>
          <span className={`inline-flex items-center gap-1.5 px-2 sm:px-2.5 py-1 rounded-full surface-soft ${liveConnected ? 'text-emerald-400' : 'app-text-muted'}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${liveConnected ? 'bg-emerald-400 live-dot' : 'bg-gray-500'}`}></span>
            <span className="font-semibold tracking-wide text-[10px] sm:text-[11px] uppercase">{liveConnected ? 'Live' : 'Offline'}</span>
          </span>
          {/* r49: ⌘K quick-trade button */}
          <button
            onClick={() => setQuickOpen(true)}
            title="⌘K — quick trade ticket"
            aria-label="Open quick trade palette (⌘K)"
            className="hidden sm:flex items-center gap-1 text-[10px] px-2 py-1 rounded-md surface-soft border app-border-soft app-text-secondary hover:app-text-primary"
          >
            <span>⌘K</span>
            <span className="hidden lg:inline">Quick ticket</span>
          </button>
          {/* r49: alert inbox */}
          <AlertInbox />
          {/* r49: density toggle */}
          <button
            onClick={() => setDensity(density === 'compact' ? 'regular' : 'compact')}
            title={density === 'compact' ? 'Switch to regular density' : 'Switch to compact density'}
            aria-label="Toggle density"
            className="text-[11px] px-2 py-1 rounded-md surface-soft app-text-secondary hover:app-text-primary"
          >
            {density === 'compact' ? '⊞' : '⊟'}
          </button>
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <button
            onClick={onLogout}
            title="Clear saved API key and log out"
            className="text-[10px] sm:text-[11px] px-2 sm:px-2.5 py-1 rounded-full surface-soft app-text-secondary hover:app-text-primary font-semibold uppercase tracking-wider"
          >
            <span className="hidden sm:inline">Log out</span>
            <span className="sm:hidden">⏻</span>
          </button>
        </div>
      </header>
      <SafetyBanner />
      <div className="flex-1 flex overflow-hidden relative">
        {view === 'charts' && (
          <>
            {/* Mobile scrim backdrop — tap to close watchlist drawer */}
            {mobileWatchOpen && (
              <div
                className="md:hidden fixed inset-0 z-30 bg-black/50"
                onClick={() => setMobileWatchOpen(false)}
              />
            )}
            {/* Watchlist: sidebar on desktop, slide-in drawer on mobile */}
            <div className={`
              ${mobileWatchOpen ? 'translate-x-0' : '-translate-x-full'}
              md:translate-x-0
              fixed md:static inset-y-0 left-0 z-40 md:z-auto
              w-[80vw] max-w-xs md:w-72
              transition-transform duration-200 ease-in-out
              flex
            `}>
              <WatchlistPanel
                overview={overviewWithLive}
                selected={selected}
                onSelect={(t) => { setSelected(t); setMobileWatchOpen(false); }}
                onAdd={handleAdd}
                onRemove={handleRemove}
                onRefresh={loadOverview}
                onCloseMobile={() => setMobileWatchOpen(false)}
              />
            </div>
            <AnalysisView
              ticker={selected}
              reloadToken={reloadToken}
              liveQuote={selected ? liveQuotes[selected] : null}
              onAutoTradeChanged={loadOverview}
              theme={theme}
            />
          </>
        )}
        {view === 'trading' && (
          <div id="r49-main" className="flex-1 overflow-y-auto scrollbar-thin p-2 sm:p-4 space-y-3 sm:space-y-4 pb-20 md:pb-4">
            {/* r49: sticky command bar */}
            <div className="sticky top-0 z-10 -mx-2 sm:-mx-4 px-2 sm:px-4 pb-2" style={{ background: 'var(--bg-0)' }}>
              <CommandBar />
            </div>
            {/* r49: internal nav */}
            <TradingNav active={tradingTab} onChange={setTradingTab} />
            <ErrorBoundary>
              {tradingTab === 'overview' && (
                <>
                  <EquityCurvePanel lookbackDays={30} />
                  <DailyLossProgress />
                  <AlertsPanel />
                </>
              )}
              {tradingTab === 'positions' && <TradingPanel reloadToken={reloadToken} />}
              {tradingTab === 'autotrader' && <AutoTraderPanel reloadToken={reloadToken} />}
              {tradingTab === 'universe' && <CandidatePoolPanel />}
              {tradingTab === 'adopted' && <AdoptedPanel />}
              {tradingTab === 'rejected' && <RejectedSignalsPanel />}
              {tradingTab === 'news' && <NewsAnalysisSummary />}
            </ErrorBoundary>
            {/* r49: mobile bottom-tab for trading view */}
            <TradingMobileBottomNav active={tradingTab} onChange={setTradingTab} />
          </div>
        )}
        {view === 'charts' && <span id="r49-main" />}
      </div>
      <TargetHitToasts />
      <ChatWidget />
    </div>
    </ErrorBoundary>
  );
}

// r49: trading view internal nav (tabs)
function TradingNav({ active, onChange }) {
  const tabs = [
    { k: 'overview', label: 'Overview', icon: '📊' },
    { k: 'positions', label: 'Positions', icon: '💼' },
    { k: 'autotrader', label: 'Auto-trader', icon: '🤖' },
    { k: 'universe', label: 'Universe', icon: '🎯' },
    { k: 'adopted', label: 'Adopted', icon: '📥' },
    { k: 'rejected', label: 'Rejected', icon: '🚫' },
    { k: 'news', label: 'News audit', icon: '📰' },
  ];
  return (
    <nav role="tablist" aria-label="Trading sections" className="hidden md:flex flex-wrap gap-1 surface-soft rounded-xl p-1 text-xs">
      {tabs.map(t => (
        <button
          key={t.k}
          role="tab"
          aria-selected={active === t.k}
          onClick={() => onChange(t.k)}
          className={`px-3 py-1.5 rounded-lg font-medium ${
            active === t.k
              ? 'bg-gradient-to-b from-blue-500 to-blue-600 text-white glow-blue'
              : 'text-gray-400 hover:text-white hover:bg-white/5'
          }`}
        >
          <span className="mr-1">{t.icon}</span>{t.label}
        </button>
      ))}
    </nav>
  );
}

// r49: bottom-tab nav for mobile trading view
function TradingMobileBottomNav({ active, onChange }) {
  const tabs = [
    { k: 'overview', label: 'Home', icon: '📊' },
    { k: 'positions', label: 'Pos', icon: '💼' },
    { k: 'autotrader', label: 'Bot', icon: '🤖' },
    { k: 'universe', label: 'Univ', icon: '🎯' },
    { k: 'rejected', label: 'Skip', icon: '🚫' },
  ];
  return (
    <nav
      role="tablist"
      aria-label="Trading sections (mobile)"
      className="md:hidden fixed bottom-0 left-0 right-0 z-30 surface border-t app-border flex"
      style={{ paddingBottom: 'env(safe-area-inset-bottom, 0)' }}
    >
      {tabs.map(t => (
        <button
          key={t.k}
          role="tab"
          aria-selected={active === t.k}
          onClick={() => onChange(t.k)}
          className={`flex-1 flex flex-col items-center justify-center py-2 ${
            active === t.k ? 'text-blue-400' : 'app-text-muted'
          }`}
        >
          <span className="text-base">{t.icon}</span>
          <span className="text-[9px] font-semibold uppercase tracking-wider mt-0.5">{t.label}</span>
        </button>
      ))}
    </nav>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
