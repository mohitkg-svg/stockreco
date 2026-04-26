const { useState, useEffect, useRef, useCallback } = React;

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

// ---------- API key (shared secret, stored in localStorage) ----------
// The backend gates every /api/* endpoint with `X-API-Key`. The browser
// can't set headers on WebSockets, so we also append `?token=` to the WS
// URL. The login screen in <App/> populates this on first visit.
const API_KEY_STORAGE = 'app_api_key';
function getApiKey() {
  try { return localStorage.getItem(API_KEY_STORAGE) || ''; } catch (_) { return ''; }
}
function setApiKey(k) {
  try {
    if (k) localStorage.setItem(API_KEY_STORAGE, k);
    else   localStorage.removeItem(API_KEY_STORAGE);
  } catch (_) {}
}
function authHeaders() {
  const k = getApiKey();
  return k ? { 'X-API-Key': k } : {};
}

// Global 401 handler — one bad key invalidates the session and forces a
// re-login. Dispatches a custom event the App root listens for.
function on401() {
  setApiKey('');
  window.dispatchEvent(new CustomEvent('app:unauthorized'));
}

const api = {
  get: (path) => fetch(`${API_BASE}${path}`, { headers: authHeaders() })
    .then(r => {
      if (r.status === 401) { on401(); return Promise.reject('unauthorized'); }
      return r.ok ? r.json() : Promise.reject(r.statusText);
    }),
  post: (path, body) => fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: body ? JSON.stringify(body) : undefined,
  }).then(r => {
    if (r.status === 401) { on401(); return Promise.reject('unauthorized'); }
    return r.ok ? r.json() : r.json().then(e => Promise.reject(e.detail || r.statusText));
  }),
  delete: (path) => fetch(`${API_BASE}${path}`, { method: 'DELETE', headers: authHeaders() })
    .then(r => {
      if (r.status === 401) { on401(); return Promise.reject('unauthorized'); }
      return r.ok ? r.json() : Promise.reject(r.statusText);
    }),
};

// ---------- Live Quotes WebSocket ----------
// Connects to /ws/quotes and maintains {SYMBOL: {bid, ask, last, ts}} in state.
// `onSignalUpdate(ticker)` fires when the server live-recomputes signals so the UI can refetch.
function useLiveQuotes(onSignalUpdate) {
  const [quotes, setQuotes] = useState({});
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    let closed = false;
    let reconnectTimer = null;

    const connect = () => {
      // Browsers can't set headers on WebSockets; token auth via query param.
      // The backend verifies it with the same constant-time compare.
      const key = getApiKey();
      const q = key ? `?token=${encodeURIComponent(key)}` : '';
      const wsUrl = API_BASE.replace(/^http/, 'ws') + '/ws/quotes' + q;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) reconnectTimer = setTimeout(connect, 3000);
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
        if (msg.type === 'snapshot') {
          setQuotes(msg.stocks && typeof msg.stocks === 'object' ? msg.stocks : {});
        } else if (msg.type === 'stock_trade' || msg.type === 'stock_quote') {
          if (typeof msg.symbol !== 'string' || !msg.symbol) {
            console.warn('ws: missing symbol on', msg.type);
            return;
          }
          setQuotes(prev => ({
            ...prev,
            [msg.symbol]: { ...(prev[msg.symbol] || {}), ...msg },
          }));
        } else if (msg.type === 'signals_updated' && onSignalUpdate && typeof msg.symbol === 'string') {
          onSignalUpdate(msg.symbol);
        } else if (msg.type === 'target_hit') {
          // Fire a toast + browser notification for T1/T2/T3 hits.
          try { window.dispatchEvent(new CustomEvent('app:target_hit', { detail: msg })); } catch (_) {}
        } else if (msg.type === 'trade_closed') {
          // Same fan-out — close-side toast + notification.
          try { window.dispatchEvent(new CustomEvent('app:trade_closed', { detail: msg })); } catch (_) {}
        }
      };
    };

    connect();
    return () => {
      closed = true;
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
  return (
    <div className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-bold tracking-wide border ${klass}`}>
      {isNew && <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />}
      {type}
      {confidence && <span className="opacity-70">{Math.round(confidence)}%</span>}
    </div>
  );
}

// ---------- Watchlist Panel ----------
function WatchlistPanel({ overview, selected, onSelect, onAdd, onRemove, onRefresh, onCloseMobile }) {
  const [newTicker, setNewTicker] = useState('');
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState(null);

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
        {error && <div className="text-xs text-red-400 mt-1">{error}</div>}
      </div>
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {overview.length === 0 && <div className="p-4 text-sm text-gray-500 text-center">No stocks yet. Add one above.</div>}
        {overview.map((item) => (
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
                <span className="text-sm font-mono font-semibold tabular-nums">${item.price.toFixed(2)}</span>
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
}

// ---------- Stock Chart ----------
function StockChart({ ticker, timeframe, liveQuote = null, theme = 'dark', hideIndicators = false }) {
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
      height: 460,
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
  }, [theme]);

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
          data.indicators.forEach(ind => {
            const lineSeries = chartRef.current.addLineSeries({
              color: ind.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
            });
            lineSeries.setData(ind.values.filter(v => v.value != null));
            seriesRef.current.indicators[ind.name] = lineSeries;
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
        data.support_resistance.forEach(lvl => {
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
        const zones = data.supply_demand_zones || {};
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
        const fib = data.fibonacci;
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
  }, [ticker, timeframe, hideIndicators, theme]);

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

    const tickSec = Math.floor((liveQuote.ts || Date.now() / 1000));
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
function SignalCard({ signal, currentPrice }) {
  if (!signal) return null;
  const isBuy = signal.signal_type === 'BUY';
  const borderColor = isBuy ? 'border-emerald-600/50' : signal.signal_type === 'SELL' ? 'border-red-600/50' : 'border-gray-600/50';

  const rr = signal.entry && signal.stop_loss && signal.target1
    ? (Math.abs(signal.target1 - signal.entry) / Math.abs(signal.entry - signal.stop_loss)).toFixed(2)
    : null;

  return (
    <div className={`surface border ${borderColor} rounded-2xl p-5 shadow-xl shadow-black/20`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-lg font-bold">Primary Signal</h3>
          <SignalBadge type={signal.signal_type} confidence={signal.confidence} />
          <span className="text-xs text-gray-500 uppercase">{signal.timeframe}</span>
          {signal.strategy && (
            <span className="text-[10px] px-2 py-0.5 rounded bg-indigo-900/40 border border-indigo-700 text-indigo-300 uppercase tracking-wider" title="Strategy used to derive this signal">
              {signal.strategy}
            </span>
          )}
        </div>
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
  const VISIBLE = 6;
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
  const d = new Date(isoStr);
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
              href={it.url || '#'}
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
            <div>conf: {fmtNum(data.backtest.confidence, 0)}</div>
            <div>OOS trades: {data.backtest.oos_trades ?? '—'}</div>
            <div>win rate: {fmtPct(data.backtest.win_rate)}</div>
            <div>avg P/L: {fmtNum(data.backtest.avg_pl, 1)}</div>
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
                <a key={a.id} href={a.url || '#'} target="_blank" rel="noopener noreferrer"
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
  const [timeframe, setTimeframe] = useState('1d');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [hideIndicators, setHideIndicators] = useState(() => {
    try { return localStorage.getItem('hideIndicators') === '1'; } catch (_) { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem('hideIndicators', hideIndicators ? '1' : '0'); } catch (_) {}
  }, [hideIndicators]);

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

      {error && <div className="m-3 sm:m-4 p-3 bg-red-900/30 border border-red-800 rounded text-sm text-red-300">{error}</div>}

      <div className="p-3 sm:p-4 space-y-3 sm:space-y-4">
        <div className="surface rounded-xl overflow-hidden">
          <div className="px-3 py-2 flex items-center justify-between border-b app-border text-xs">
            <div className="app-text-muted uppercase tracking-widest">{timeframe} chart</div>
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={hideIndicators}
                onChange={e => setHideIndicators(e.target.checked)}
                className="accent-blue-500"
              />
              <span className="app-text-secondary">Hide all indicators</span>
            </label>
          </div>
          <StockChart ticker={ticker} timeframe={timeframe} liveQuote={liveQuote} theme={theme} hideIndicators={hideIndicators} />
        </div>

        {analysis?.timeframe_alignment && <TimeframeAlignment alignment={analysis.timeframe_alignment} signals={analysis.signals} />}

        {timeframeSignal && <SignalCard signal={timeframeSignal} currentPrice={analysis?.current_price} />}

        {analysis?.primary_signal && timeframeSignal?.timeframe !== analysis.primary_signal.timeframe && (
          <div>
            <div className="text-xs text-gray-500 mb-2">Strongest signal across all timeframes:</div>
            <SignalCard signal={analysis.primary_signal} currentPrice={analysis?.current_price} />
          </div>
        )}

        <NewsPanel ticker={ticker} />

        <OptionsPanel ticker={ticker} signal={analysis?.primary_signal} />

        <BacktestPanel ticker={ticker} />
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

  const tp = signal[target];
  // Audit fix M9: validate qty input. Empty input → Number(qty)=NaN → server
  // 422 with confusing alert. Clamp + Number-coerce here so the displayed
  // notional/risk are real numbers and submit can be gated.
  const qtyNum = Number(qty);
  const qtyValid = Number.isFinite(qtyNum) && qtyNum >= 1;
  const safeQty = qtyValid ? qtyNum : 0;
  const cost = (signal.entry * safeQty).toFixed(2);
  const risk = signal.stop_loss != null
    ? (Math.abs(signal.entry - signal.stop_loss) * safeQty).toFixed(2)
    : null;
  const reward = tp != null
    ? (Math.abs(tp - signal.entry) * safeQty).toFixed(2)
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
              <input type="number" min="1" value={qty} onChange={e => setQty(e.target.value)}
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
      const r = await api.post('/api/trading/auto/universe-scan');
      console.info('universe scan result', r);
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

  // Perf: puts-watch iterates the full watchlist, synthesises a bear thesis
  // per ticker, and fetches option chains — it was gating the whole panel's
  // first paint. Now: status + trades load fast (they're cheap DB queries);
  // puts-watch is fetched lazily when the user expands that section.
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
      if (sr.status === 'fulfilled') setStatus(sr.value);
      if (tr.status === 'fulfilled') setTrades(tr.value || []);
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, [load, reloadToken]);

  const toggle = async () => {
    if (!status) return;
    setBusy(true);
    try {
      await api.post('/api/trading/auto/config', { enabled: !status.enabled });
      await load();
    } catch (e) { alert('Toggle failed: ' + (e.detail || e)); }
    finally { setBusy(false); }
  };

  const updateCfg = async (patch) => {
    setBusy(true);
    try {
      await api.post('/api/trading/auto/config', patch);
      await load();
    } catch (e) { alert('Update failed: ' + (e.detail || e)); }
    finally { setBusy(false); }
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
        <Stat
          label="Today P/L"
          value={(() => {
            const pl = trades
              .filter(t => t.closed_at && new Date(t.closed_at).toDateString() === new Date().toDateString())
              .reduce((a, t) => a + (t.realized_pl || 0), 0);
            return `${pl >= 0 ? '+' : ''}$${pl.toFixed(2)}`;
          })()}
          positive={trades.filter(t => t.closed_at && new Date(t.closed_at).toDateString() === new Date().toDateString()).reduce((a, t) => a + (t.realized_pl || 0), 0) > 0}
          negative={trades.filter(t => t.closed_at && new Date(t.closed_at).toDateString() === new Date().toDateString()).reduce((a, t) => a + (t.realized_pl || 0), 0) < 0}
          hint="UTC today, closed"
        />
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
                t.status === 'closed_target' ? { cls: 'pill-success', label: 'target' } :
                t.status === 'closed_stop' ? { cls: 'pill-danger', label: 'stopped' } :
                t.status?.startsWith('closed_') ? { cls: '', label: t.status.replace('closed_', '') } :
                { cls: '', label: t.status };
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
                      {t.realized_pl == null
                        ? <span className="app-text-muted">—</span>
                        : `${t.realized_pl >= 0 ? '+' : ''}$${t.realized_pl.toFixed(2)}`}
                    </div>
                  </div>

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
  return (
    <div>
      <div className="flex justify-between text-xs mb-1.5">
        <span className="app-text-secondary font-semibold">{label}</span>
        <span className="font-mono app-text-primary">
          ${Number(used).toLocaleString(undefined, {maximumFractionDigits: 0})}
          <span className="app-text-muted"> / ${Number(budget).toLocaleString(undefined, {maximumFractionDigits: 0})}</span>
          <span className="ml-1.5 app-text-muted">({pct.toFixed(1)}%)</span>
        </span>
      </div>
      <div className="h-2 rounded-full overflow-hidden" style={{ background: 'var(--surface-border)' }}>
        <div className={`h-full ${color} transition-all`} style={{ width: `${pct}%` }} />
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

// ---------- Per-ticker auto-trade toggle ----------
// Shown in the analysis-view header. Hits PATCH /api/watchlist/{ticker}/auto-trade
// and re-fetches overview so the WatchlistPanel badge updates in lockstep.
function TickerAutoTradeToggle({ ticker, onChanged }) {
  const [enabled, setEnabled] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

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

  const toggle = async () => {
    setBusy(true);
    try {
      const next = !enabled;
      await fetch(`${API_BASE}/api/watchlist/${ticker}/auto-trade`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ enabled: next }),
      });
      setEnabled(next);
      if (onChanged) onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      onClick={toggle}
      disabled={busy}
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
  );
}


// ---------- Trading Panel: account snapshot + positions + recent orders ----------
function TradingPanel({ ticker, reloadToken }) {
  const [account, setAccount] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [a, p, o] = await Promise.all([
        api.get('/api/trading/account').catch(() => null),
        api.get('/api/trading/positions').catch(() => []),
        api.get('/api/trading/orders?status=all&limit=20').catch(() => []),
      ]);
      setAccount(a); setPositions(p || []); setOrders(o || []);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, [load, reloadToken]);

  const closePos = async (sym) => {
    if (!confirm(`Close entire ${sym} position at market?`)) return;
    setBusy(true);
    try { await api.post(`/api/trading/close/${sym}`); await load(); }
    catch (e) { alert('Close failed: ' + (e.detail || e)); }
    finally { setBusy(false); }
  };
  const cancelOrd = async (id) => {
    setBusy(true);
    try { await fetch(`${API_BASE}/api/trading/orders/${id}`, { method: 'DELETE', headers: authHeaders() }); await load(); }
    catch (e) {} finally { setBusy(false); }
  };

  if (!account && !error) {
    return null; // Trading not configured — hide silently
  }
  if (error && !account) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 text-xs text-gray-500">
        Paper trading unavailable: {error}
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
        <button onClick={load} className="text-xs app-text-secondary hover:app-text-primary flex items-center gap-1 px-2 py-1 rounded-md hover:bg-white/5">
          <span>↻</span><span>Refresh</span>
        </button>
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

      {/* Positions — collapsible with scrolling body */}
      <div className="mb-4">
        <CollapsibleSection
          title="Open Positions"
          count={positions.length}
          defaultOpen={false}
          maxHeight={460}
        >
          {positions.length === 0 ? (
            <div className="text-center text-sm app-text-muted italic py-4">No open positions.</div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {[...tickerPositions, ...otherPositions].map(p => {
                const isWin = p.unrealized_pl >= 0;
                const rowHighlight = p.symbol === ticker ? 'ring-1 ring-blue-500/50' : '';
                return (
                  <div key={p.symbol} className={`app-bg-surface-solid rounded-xl p-3 lift border app-border-soft ${rowHighlight}`}>
                    <div className="flex items-start justify-between mb-2">
                      <div>
                        <div className="font-bold text-base flex items-center gap-2">
                          <span>{p.symbol}</span>
                          <span className="text-[10px] px-1.5 py-0.5 rounded app-bg-surface-solid app-text-secondary uppercase tracking-wider">{p.side}</span>
                        </div>
                        <div className="text-[11px] app-text-muted font-mono">Qty {p.qty} @ ${p.avg_entry_price.toFixed(2)}</div>
                      </div>
                      <button disabled={busy} onClick={() => closePos(p.symbol)}
                              className="text-[10px] px-2 py-1 rounded-md bg-red-500/20 hover:bg-red-500/30 text-red-400 disabled:opacity-50 border border-red-500/30 font-semibold">
                        Close
                      </button>
                    </div>
                    <div className="flex items-baseline justify-between">
                      <div>
                        <div className="text-[10px] uppercase tracking-wider app-text-muted">Last</div>
                        <div className="font-mono text-base font-semibold">{p.current_price ? `$${p.current_price.toFixed(2)}` : '—'}</div>
                      </div>
                      <div className="text-right">
                        <div className={`font-mono text-lg font-bold ${isWin ? 'text-emerald-400' : 'text-red-400'}`}>
                          {isWin ? '+' : ''}${p.unrealized_pl.toFixed(2)}
                        </div>
                        <div className={`text-xs font-mono ${isWin ? 'text-emerald-400' : 'text-red-400'}`}>
                          {isWin ? '+' : ''}{p.unrealized_plpc.toFixed(2)}%
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CollapsibleSection>
      </div>

      {/* Recent orders — collapsible with scrolling body */}
      <CollapsibleSection
        title="Recent Orders"
        count={orders.length}
        defaultOpen={false}
        maxHeight={420}
      >
        {orders.length === 0 ? (
          <div className="text-center text-sm app-text-muted italic py-4">No orders yet.</div>
        ) : (
          <table className="w-full text-xs">
            <thead className="sticky top-0 app-bg-surface-solid z-10">
              <tr className="app-text-muted border-b app-border">
                <th className="text-left py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Symbol</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Side</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Qty</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Type</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Status</th>
                <th className="text-right py-2 px-3 font-semibold uppercase tracking-wider text-[10px]">Submitted</th>
                <th className="py-2 px-3"></th>
              </tr>
            </thead>
            <tbody>
              {orders.map(o => (
                <tr key={o.id} className="border-b app-border-soft last:border-0 hover:bg-white/3">
                  <td className="py-2 px-3 font-semibold font-mono">{o.symbol}</td>
                  <td className={`text-right py-2 px-3 font-semibold ${o.side.includes('BUY') ? 'text-emerald-400' : 'text-red-400'}`}>{o.side.replace('OrderSide.', '')}</td>
                  <td className="text-right py-2 px-3 font-mono">{o.qty}</td>
                  <td className="text-right py-2 px-3 app-text-muted">{(o.type || '').replace('OrderType.', '')}</td>
                  <td className="text-right py-2 px-3 app-text-secondary">{(o.status || '').replace('OrderStatus.', '')}</td>
                  <td className="text-right py-2 px-3 app-text-muted font-mono">{o.submitted_at?.slice(11, 19) || '—'}</td>
                  <td className="text-right py-2 px-3">
                    {(o.status || '').includes('NEW') || (o.status || '').includes('ACCEPTED') ? (
                      <button disabled={busy} onClick={() => cancelOrd(o.id)}
                              className="text-[10px] px-2 py-0.5 rounded-md bg-white/5 hover:bg-white/10 app-text-secondary disabled:opacity-50 border app-border">
                        Cancel
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CollapsibleSection>
    </div>
  );
}

// ---------- Login screen ----------
// Shown when the backend requires APP_API_KEY and localStorage doesn't have
// one. On submit, we probe /api/health with the key — it always requires
// auth when auth is configured, so a 200 means the key is valid.
function LoginScreen({ onSuccess }) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const submit = async (e) => {
    e?.preventDefault();
    if (!key.trim()) return;
    setBusy(true); setErr(null);
    try {
      // /api/analysis/overview is gated and lightweight — perfect probe.
      const r = await fetch(`${API_BASE}/api/analysis/overview`, {
        headers: { 'X-API-Key': key.trim() },
      });
      if (r.status === 401) { setErr('Invalid key'); setBusy(false); return; }
      if (!r.ok && r.status !== 200) { setErr(`Server ${r.status}`); setBusy(false); return; }
      setApiKey(key.trim());
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
          className="w-full bg-gray-900/60 border border-white/10 rounded-lg px-3 py-2.5 text-sm placeholder-gray-500 focus:border-blue-500/70 font-mono"
        />
        {err && <div className="mt-2 text-xs text-red-400">{err}</div>}
        <button
          type="submit"
          disabled={busy || !key.trim()}
          className="mt-4 w-full py-2.5 rounded-lg bg-gradient-to-b from-blue-500 to-blue-600 hover:from-blue-400 hover:to-blue-500 disabled:opacity-50 text-sm font-semibold shadow-lg shadow-blue-500/20"
        >
          {busy ? 'Verifying…' : 'Unlock'}
        </button>
        <div className="mt-4 text-[10px] app-text-muted leading-relaxed">
          Your key is stored only in this browser's localStorage. It's never sent anywhere other than the API. Log out from the header pill to clear it.
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
  const [selected, setSelected] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [view, setView] = useState('charts'); // 'charts' | 'trading'
  const [theme, toggleTheme] = useTheme();
  const [mobileWatchOpen, setMobileWatchOpen] = useState(false);

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
  const overviewWithLive = overview.map(row => {
    const q = liveQuotes[row.ticker];
    if (!q) return row;
    const livePx = q.last || (q.bid && q.ask ? (q.bid + q.ask) / 2 : null);
    if (!livePx) return row;
    return { ...row, price: Math.round(livePx * 100) / 100, live: true };
  });

  useEffect(() => {
    loadOverview();
    const iv = setInterval(loadOverview, 60000);
    return () => clearInterval(iv);
  }, [loadOverview]);

  const handleAdd = async (ticker) => {
    await api.post('/api/watchlist', { ticker });
    await loadOverview();
    setSelected(ticker);
  };

  const handleRemove = async (ticker) => {
    if (!confirm(`Remove ${ticker} from watchlist?`)) return;
    await api.delete(`/api/watchlist/${ticker}`);
    await loadOverview();
    if (selected === ticker) setSelected(null);
  };

  return (
    <div className="h-screen flex flex-col">
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
        <div className="text-xs flex items-center gap-2 sm:gap-4 shrink-0">
          <span className="hidden lg:inline app-text-muted">Auto-scan 15m · Polling 60s</span>
          <span className={`inline-flex items-center gap-1.5 px-2 sm:px-2.5 py-1 rounded-full surface-soft ${liveConnected ? 'text-emerald-400' : 'app-text-muted'}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${liveConnected ? 'bg-emerald-400 live-dot' : 'bg-gray-500'}`}></span>
            <span className="font-semibold tracking-wide text-[10px] sm:text-[11px] uppercase">{liveConnected ? 'Live' : 'Offline'}</span>
          </span>
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
          <div className="flex-1 overflow-y-auto scrollbar-thin p-2 sm:p-4 space-y-3 sm:space-y-4">
            <AutoTraderPanel reloadToken={reloadToken} />
            <CandidatePoolPanel />
            <NewsAnalysisSummary />
            <TradingPanel reloadToken={reloadToken} />
          </div>
        )}
      </div>
      <TargetHitToasts />
      <ChatWidget />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
