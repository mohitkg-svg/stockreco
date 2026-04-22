const { useState, useEffect, useRef, useCallback } = React;

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

const api = {
  get: (path) => fetch(`${API_BASE}${path}`).then(r => r.ok ? r.json() : Promise.reject(r.statusText)),
  post: (path, body) => fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  }).then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e.detail || r.statusText))),
  delete: (path) => fetch(`${API_BASE}${path}`, { method: 'DELETE' }).then(r => r.ok ? r.json() : Promise.reject(r.statusText)),
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
      const wsUrl = API_BASE.replace(/^http/, 'ws') + '/ws/quotes';
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
function WatchlistPanel({ overview, selected, onSelect, onAdd, onRemove, onRefresh }) {
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
    <div className="w-72 surface border-r border-white/5 flex flex-col h-full">
      <div className="p-3.5 border-b border-white/5">
        <div className="flex items-center justify-between mb-2.5">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-gray-400">Watchlist</h2>
          <button onClick={onRefresh} className="w-6 h-6 rounded-md text-gray-500 hover:text-white hover:bg-white/5 flex items-center justify-center" title="Refresh">⟳</button>
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
function StockChart({ ticker, timeframe, liveQuote = null }) {
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

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = LightweightCharts.createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 460,
      layout: { background: { color: '#0f1419' }, textColor: '#d1d5db' },
      grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#374151' },
      rightPriceScale: { borderColor: '#374151' },
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
  }, []);

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
    fetch(`${API_BASE}/api/analysis/${ticker}/chart?timeframe=${timeframe}`, { signal: ac.signal })
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

        data.indicators.forEach(ind => {
          const lineSeries = chartRef.current.addLineSeries({
            color: ind.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          });
          lineSeries.setData(ind.values.filter(v => v.value != null));
          seriesRef.current.indicators[ind.name] = lineSeries;
        });

        // Support/Resistance price lines on the candle series
        clearPriceLines();
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
  }, [ticker, timeframe]);

  // ----- Live tick: extend the most recent bar with the latest WS price -----
  useEffect(() => {
    if (!liveQuote || !chartRef.current || !seriesRef.current.candle || !lastCandleRef.current) return;
    const px = liveQuote.last || (liveQuote.bid && liveQuote.ask ? (liveQuote.bid + liveQuote.ask) / 2 : null);
    if (!px) return;
    const bar = lastCandleRef.current;
    bar.high = Math.max(bar.high, px);
    bar.low = Math.min(bar.low, px);
    bar.close = px;
    try {
      seriesRef.current.candle.update(bar);
    } catch (e) {
      // Only happens during the brief gap between cleanup and next setData — log
      // for diagnosis instead of swallowing entirely.
      if (chartRef.current) console.debug('chart live-update skipped:', e?.message || e);
    }
  }, [liveQuote?.last, liveQuote?.bid, liveQuote?.ask]);

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
    <div className="flex surface-soft rounded-xl p-1 text-xs">
      {TIMEFRAMES.map(tf => (
        <button
          key={tf}
          onClick={() => onChange(tf)}
          className={`px-3 py-1.5 rounded-lg font-semibold tracking-wide ${value === tf ? 'bg-gradient-to-b from-blue-500 to-blue-600 text-white glow-blue' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
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
        <div className="grid grid-cols-5 gap-2 mb-3 text-sm">
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
      <div className="mt-3">
        <div className="text-xs text-gray-400 mb-1">Reasoning</div>
        <div className="text-sm reasoning-text bg-gray-950/50 rounded p-3 max-h-48 overflow-y-auto scrollbar-thin">
          {signal.reasoning}
        </div>
      </div>
      {signal.entry != null && <LevelMethodology signal={signal} />}
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
      <div className="grid grid-cols-7 gap-2">
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
      layout: { background: { color: '#0f1419' }, textColor: '#d1d5db' },
      grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } },
      timeScale: { timeVisible: false, borderColor: '#374151' },
      rightPriceScale: { borderColor: '#374151' },
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

function Stat({ label, value, positive, negative }) {
  const color = positive ? 'text-emerald-400' : negative ? 'text-red-400' : 'text-white';
  return (
    <div className="bg-gray-800 rounded p-2">
      <div className="text-gray-500 text-[10px] uppercase">{label}</div>
      <div className={`font-semibold ${color}`}>{value}</div>
    </div>
  );
}

// ---------- Analysis View ----------
function AnalysisView({ ticker, reloadToken = 0, liveQuote = null, onAutoTradeChanged = null }) {
  const [analysis, setAnalysis] = useState(null);
  const [timeframe, setTimeframe] = useState('1d');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const loadAnalysis = useCallback(async (refresh = false) => {
    if (!ticker) return;
    setLoading(true); setError(null);
    try {
      const data = await api.get(`/api/analysis/${ticker}${refresh ? '?refresh=true' : ''}`);
      setAnalysis(data);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [ticker]);

  useEffect(() => {
    loadAnalysis(false);
  }, [ticker, loadAnalysis, reloadToken]);

  if (!ticker) {
    return <div className="flex-1 flex items-center justify-center text-gray-500">Select a stock from the watchlist to see its analysis</div>;
  }

  const timeframeSignal = analysis?.signals?.find(s => s.timeframe === timeframe);

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin">
      <div className="p-4 border-b border-gray-800 flex items-center justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">{ticker}</h1>
            {analysis?.name && <span className="text-gray-400 text-sm">{analysis.name}</span>}
            {analysis?.current_price && (
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-semibold">${analysis.current_price.toFixed(2)}</span>
                {/* Audit fix M8 (mirror): null/NaN-safe change_pct render. */}
                {analysis.change_pct != null && Number.isFinite(analysis.change_pct) ? (
                  <span className={`text-sm ${analysis.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {analysis.change_pct >= 0 ? '+' : ''}{analysis.change_pct.toFixed(2)}%
                  </span>
                ) : (
                  <span className="text-sm text-gray-500">—</span>
                )}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <TickerAutoTradeToggle ticker={ticker} onChanged={onAutoTradeChanged} />
          <TimeframeSelector value={timeframe} onChange={setTimeframe} />
          <button onClick={() => loadAnalysis(true)} disabled={loading} className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-3 py-1 rounded text-sm">
            {loading ? 'Refreshing…' : 'Refresh Analysis'}
          </button>
        </div>
      </div>

      {error && <div className="m-4 p-3 bg-red-900/30 border border-red-800 rounded text-sm text-red-300">{error}</div>}

      <div className="p-4 space-y-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <StockChart ticker={ticker} timeframe={timeframe} liveQuote={liveQuote} />
        </div>

        {analysis?.timeframe_alignment && <TimeframeAlignment alignment={analysis.timeframe_alignment} signals={analysis.signals} />}

        {timeframeSignal && <SignalCard signal={timeframeSignal} currentPrice={analysis?.current_price} />}

        {analysis?.primary_signal && timeframeSignal?.timeframe !== analysis.primary_signal.timeframe && (
          <div>
            <div className="text-xs text-gray-500 mb-2">Strongest signal across all timeframes:</div>
            <SignalCard signal={analysis.primary_signal} currentPrice={analysis?.current_price} />
          </div>
        )}

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

// ---------- Auto-Trader Panel: enable, configure, monitor automated trades ----------
function AutoTraderPanel({ reloadToken }) {
  const [status, setStatus] = useState(null);
  const [trades, setTrades] = useState([]);
  const [putsWatch, setPutsWatch] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showCfg, setShowCfg] = useState(false);
  const [expanded, setExpanded] = useState(null); // trade.id whose post-mortem is open

  const inFlight = useRef(false);
  const load = useCallback(async () => {
    if (inFlight.current) return;  // dogpile guard
    inFlight.current = true;
    try {
      // allSettled so a single endpoint failure doesn't blank the whole panel.
      const results = await Promise.allSettled([
        api.get('/api/trading/auto/status'),
        api.get('/api/trading/auto/trades?limit=20'),
        api.get('/api/options/puts-watch'),
      ]);
      const [sr, tr, pwr] = results;
      if (sr.status === 'fulfilled') setStatus(sr.value);
      if (tr.status === 'fulfilled') setTrades(tr.value || []);
      if (pwr.status === 'fulfilled') setPutsWatch(pwr.value);
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

  if (!status) return null;
  if (!status.broker_connected) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 text-xs text-gray-500">
        Auto-trader unavailable: broker not connected.
      </div>
    );
  }

  const pct = (used, budget) => budget > 0 ? Math.min(100, (used / budget) * 100) : 0;
  const cfg = status.config;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-lg font-bold flex items-center gap-2">
          🤖 Auto-Trader
          <span className={`text-[10px] px-2 py-0.5 rounded uppercase font-bold ${status.enabled ? 'bg-emerald-700 text-emerald-100' : 'bg-gray-800 text-gray-400 border border-gray-700'}`}>
            {status.enabled ? 'ON' : 'OFF'}
          </span>
        </h3>
        <div className="flex items-center gap-2">
          <button onClick={() => setShowCfg(s => !s)} className="text-xs text-gray-400 hover:text-white">⚙ Config</button>
          <button
            onClick={toggle}
            disabled={busy}
            className={`px-3 py-1 text-xs rounded font-semibold ${status.enabled ? 'bg-red-700 hover:bg-red-600' : 'bg-emerald-700 hover:bg-emerald-600'}`}
          >
            {status.enabled ? 'Disable' : 'Enable'}
          </button>
        </div>
      </div>

      {/* Budget bars */}
      <div className="space-y-2 mb-3">
        <BudgetBar label="Stocks" used={status.stock_used} budget={status.stock_budget} pct={pct(status.stock_used, status.stock_budget)} color="bg-blue-600" />
        <BudgetBar label="Options" used={status.option_used} budget={status.option_budget} pct={pct(status.option_used, status.option_budget)} color="bg-purple-600" />
        <div className="text-[11px] text-gray-500">
          Total deployed: ${status.deployed.toLocaleString(undefined, {maximumFractionDigits: 0})} / ${status.total_cap.toLocaleString(undefined, {maximumFractionDigits: 0})} cap
          ({(status.total_cap > 0 ? (status.deployed / status.total_cap * 100) : 0).toFixed(1)}% of {(cfg.max_pct_of_equity * 100).toFixed(0)}%-of-equity ceiling)
        </div>
      </div>

      {/* Config panel */}
      {showCfg && (
        <div className="bg-gray-950/50 border border-gray-800 rounded p-3 mb-3 grid grid-cols-2 gap-3 text-xs">
          <CfgField label="Confidence ≥" value={cfg.confidence_threshold} suffix="%"
                    onCommit={v => updateCfg({ confidence_threshold: Number(v) })} />
          <CfgField label="Risk per trade" value={cfg.max_risk_per_trade_pct * 100} suffix="% of equity"
                    onCommit={v => updateCfg({ max_risk_per_trade_pct: Number(v) / 100 })} />
          <CfgField label="Stock budget" value={cfg.stock_pct_of_equity * 100} suffix="% of equity"
                    onCommit={v => updateCfg({ stock_pct_of_equity: Number(v) / 100 })} />
          <CfgField label="Option budget" value={cfg.option_pct_of_equity * 100} suffix="% of equity"
                    onCommit={v => updateCfg({ option_pct_of_equity: Number(v) / 100 })} />
          <div className="col-span-2 flex items-center gap-2 pt-1">
            <label className="flex items-center gap-2 text-gray-300 cursor-pointer">
              <input type="checkbox" checked={!!cfg.trade_options}
                     onChange={e => updateCfg({ trade_options: e.target.checked })} />
              <span>Auto-buy PUT options on bearish non-BUY tickers (uses 10% bucket)</span>
            </label>
          </div>
          <div className="col-span-2 text-[11px] text-gray-500 leading-relaxed">
            Strategy: long stocks on BUY signals at or above the threshold (bracket: stop = signal stop, TP = T2; stop trails to break-even at T1). For watchlist tickers without a strong BUY, a bear thesis is synthesized and the best PUT contract is bought (if options trading is enabled). PUT exits: underlying hits T1/T2, premium decays ≥ 50%, or underlying breaches the bear stop. Max 1 open auto-trade per ticker.
          </div>
        </div>
      )}

      {/* Open auto-trades */}
      <div>
        <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">
          Auto-trades ({trades.filter(t => t.status === 'open' || t.status === 'pending').length} live · {trades.length} total)
        </div>
        {trades.length === 0 ? (
          <div className="text-xs text-gray-500 italic">
            {status.enabled ? 'No auto-trades yet — waiting for the next strong BUY signal.' : 'Enable auto-trading to let signals open positions automatically.'}
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-gray-500 border-b border-gray-800">
              <tr>
                <th className="text-left py-1">Ticker</th>
                <th className="text-right">Qty</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Stop</th>
                <th className="text-right">T1 / T2 / T3</th>
                <th className="text-right">Status</th>
                <th className="text-right">P/L</th>
              </tr>
            </thead>
            <tbody>
              {trades.map(t => {
                const losingStop = t.status === 'closed_stop' && (t.realized_pl ?? 0) < 0;
                const isOpen = expanded === t.id;
                return (
                  <React.Fragment key={t.id}>
                    <tr className="border-b border-gray-800/50">
                      <td className="py-1 font-semibold">
                        {t.ticker}
                        {(t.level_index ?? 0) > 0 && (
                          <span
                            title={`trail level ${t.level_index}${t.targets_history?.length ? ` · ${t.targets_history.length} recalc` : ''}`}
                            className="ml-1 text-[9px] px-1 py-0.5 rounded bg-amber-800/50 border border-amber-700 text-amber-300"
                          >
                            L{t.level_index}{t.targets_history?.length ? `·R${t.targets_history.length}` : ''}
                          </span>
                        )}
                        {losingStop && (
                          <button
                            onClick={() => setExpanded(isOpen ? null : t.id)}
                            title="View loss post-mortem"
                            className="ml-1 text-[9px] px-1 py-0.5 rounded bg-red-900/50 border border-red-700 text-red-200 hover:bg-red-800"
                          >
                            🔍 {isOpen ? 'hide' : 'why?'}
                          </button>
                        )}
                      </td>
                      <td className="text-right">{t.qty}</td>
                      <td className="text-right">{t.entry_price ? `$${t.entry_price.toFixed(2)}` : `~$${t.requested_entry?.toFixed(2)}`}</td>
                      <td className="text-right text-red-300">${t.current_stop?.toFixed(2)}</td>
                      <td className="text-right text-emerald-300">
                        ${t.target1?.toFixed(2)} / ${t.target2?.toFixed(2)}{t.target3 ? ` / $${t.target3.toFixed(2)}` : ''}
                      </td>
                      <td className="text-right">
                        <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                          t.status === 'open' ? 'bg-emerald-900/50 text-emerald-300 border border-emerald-800' :
                          t.status === 'pending' ? 'bg-amber-900/50 text-amber-300 border border-amber-800' :
                          t.status === 'closed_target' ? 'bg-emerald-800 text-white' :
                          t.status === 'closed_stop' ? 'bg-red-800 text-white' :
                          'bg-gray-800 text-gray-400'
                        }`}>{t.status.replace('closed_', '')}</span>
                      </td>
                      <td className={`text-right ${t.realized_pl == null ? 'text-gray-500' : t.realized_pl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {t.realized_pl == null ? '—' : (t.realized_pl >= 0 ? '+' : '') + '$' + t.realized_pl.toFixed(2)}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="bg-red-950/20 border-b border-red-900/30">
                        <td colSpan={7} className="p-3">
                          <PostMortem trade={t} onRegen={async () => {
                            try {
                              const fresh = await api.post(`/api/trading/auto/postmortem/${t.id}`);
                              setTrades(ts => ts.map(x => x.id === t.id ? { ...x, post_mortem: fresh, has_post_mortem: true } : x));
                            } catch (e) { alert('Regen failed: ' + (e.detail || e)); }
                          }} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Put-play watch — non-BUY tickers with viable bear theses */}
      <PutsWatchSection data={putsWatch} canTrade={cfg.trade_options} />
    </div>
  );
}

function PutsWatchSection({ data, canTrade }) {
  const [open, setOpen] = useState(false);
  if (!data) return null;
  const sugg = data.suggestions || [];
  return (
    <div className="mt-4 pt-3 border-t border-gray-800">
      <div className="flex items-center justify-between mb-2">
        <button onClick={() => setOpen(o => !o)} className="text-xs text-gray-400 hover:text-white flex items-center gap-1">
          <span>{open ? '▼' : '▶'}</span>
          <span className="uppercase tracking-wider">📉 Put-Play Watch ({sugg.length})</span>
          {!canTrade && <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-900/50 border border-amber-700 text-amber-300">manual only — enable options auto-buy in Config</span>}
        </button>
      </div>
      {open && (
        sugg.length === 0 ? (
          <div className="text-xs text-gray-500 italic">
            No bearish put-plays found in the watchlist right now. (Tickers with strong BUY signals are excluded; tickers with weak bear conviction or illiquid put chains are skipped.)
          </div>
        ) : (
          <div className="space-y-2">
            {sugg.map(s => {
              const top = s.top_contracts[0];
              return (
                <div key={s.ticker} className="border border-purple-900/50 rounded p-2 bg-purple-950/20 text-xs">
                  <div className="flex items-center justify-between mb-1">
                    <div>
                      <span className="font-bold text-white">{s.ticker}</span>
                      <span className="text-gray-400 ml-2">{s.name}</span>
                      <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-red-900/60 text-red-200 border border-red-800">
                        BEAR {s.thesis.confidence}%
                      </span>
                    </div>
                    <div className="text-right text-gray-400">
                      Entry ${s.thesis.entry?.toFixed(2)} · Stop ${s.thesis.stop_loss?.toFixed(2)} · T1 ${s.thesis.target1?.toFixed(2)} · T2 ${s.thesis.target2?.toFixed(2)}
                    </div>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-1">
                    {s.top_contracts.slice(0, 3).map((c, i) => (
                      <div key={i} className="bg-gray-950/60 border border-gray-800 rounded p-1.5 text-[11px]">
                        <div className="font-semibold text-purple-300">
                          ${c.strike} PUT · {c.expiration} ({c.dte}d) {c.is_weekly && <span className="text-amber-400">WKLY</span>}
                        </div>
                        <div className="text-gray-400">
                          ${c.premium} · BE ${c.breakeven} · R:R {c.rr_t1}/{c.rr_t2}/{c.rr_t3} · score <span className="text-emerald-300 font-semibold">{c.score}</span>
                        </div>
                        <div className="text-gray-500 text-[10px]">vol {c.volume} · OI {c.open_interest} · IV {c.iv}% · Δ {c.delta_estimate}</div>
                      </div>
                    ))}
                  </div>
                  <div className="text-[10px] text-gray-500 mt-1 reasoning-text">
                    {s.thesis.reasoning}
                  </div>
                </div>
              );
            })}
          </div>
        )
      )}
    </div>
  );
}

function BudgetBar({ label, used, budget, pct, color }) {
  return (
    <div>
      <div className="flex justify-between text-[11px] text-gray-400 mb-0.5">
        <span>{label}</span>
        <span>${used.toLocaleString(undefined, {maximumFractionDigits: 0})} / ${budget.toLocaleString(undefined, {maximumFractionDigits: 0})} ({pct.toFixed(0)}%)</span>
      </div>
      <div className="h-1.5 bg-gray-800 rounded overflow-hidden">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
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
        headers: { 'Content-Type': 'application/json' },
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
    try { await fetch(`${API_BASE}/api/trading/orders/${id}`, { method: 'DELETE' }); await load(); }
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

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-lg font-bold">📒 Paper Trading {account?.paper && <span className="text-[10px] px-2 py-0.5 ml-2 rounded bg-amber-900/50 border border-amber-700 text-amber-300 uppercase">Paper</span>}</h3>
        <button onClick={load} className="text-xs text-gray-400 hover:text-white">↻ Refresh</button>
      </div>
      <div className="grid grid-cols-4 gap-2 text-xs mb-4">
        <Stat label="Cash" value={`$${account?.cash?.toLocaleString()}`} />
        <Stat label="Equity" value={`$${account?.equity?.toLocaleString()}`} />
        <Stat label="Buying Power" value={`$${account?.buying_power?.toLocaleString()}`} />
        <Stat label="Status" value={account?.status?.replace('AccountStatus.', '')} />
      </div>

      <div className="mb-3">
        <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">Positions ({positions.length})</div>
        {positions.length === 0 ? (
          <div className="text-xs text-gray-500 italic">No open positions.</div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-gray-500 border-b border-gray-800">
              <tr><th className="text-left py-1">Symbol</th><th className="text-right">Qty</th><th className="text-right">Avg</th><th className="text-right">Last</th><th className="text-right">P/L</th><th className="text-right">P/L %</th><th></th></tr>
            </thead>
            <tbody>
              {[...tickerPositions, ...otherPositions].map(p => (
                <tr key={p.symbol} className={`border-b border-gray-800/50 ${p.symbol === ticker ? 'bg-blue-950/20' : ''}`}>
                  <td className="py-1 font-semibold">{p.symbol} <span className="text-[10px] text-gray-500">{p.side}</span></td>
                  <td className="text-right">{p.qty}</td>
                  <td className="text-right">${p.avg_entry_price.toFixed(2)}</td>
                  <td className="text-right">{p.current_price ? `$${p.current_price.toFixed(2)}` : '—'}</td>
                  <td className={`text-right ${p.unrealized_pl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{p.unrealized_pl >= 0 ? '+' : ''}${p.unrealized_pl.toFixed(2)}</td>
                  <td className={`text-right ${p.unrealized_plpc >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{p.unrealized_plpc >= 0 ? '+' : ''}{p.unrealized_plpc.toFixed(2)}%</td>
                  <td className="text-right">
                    <button disabled={busy} onClick={() => closePos(p.symbol)} className="text-[10px] px-2 py-0.5 rounded bg-red-800/60 hover:bg-red-700 disabled:opacity-50">Close</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">Recent orders</div>
        {orders.length === 0 ? (
          <div className="text-xs text-gray-500 italic">No orders yet.</div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-gray-500 border-b border-gray-800">
              <tr><th className="text-left py-1">Symbol</th><th className="text-right">Side</th><th className="text-right">Qty</th><th className="text-right">Type</th><th className="text-right">Status</th><th className="text-right">Submitted</th><th></th></tr>
            </thead>
            <tbody>
              {orders.slice(0, 12).map(o => (
                <tr key={o.id} className="border-b border-gray-800/50">
                  <td className="py-1 font-semibold">{o.symbol}</td>
                  <td className={`text-right ${o.side.includes('BUY') ? 'text-emerald-400' : 'text-red-400'}`}>{o.side.replace('OrderSide.', '')}</td>
                  <td className="text-right">{o.qty}</td>
                  <td className="text-right text-gray-500">{(o.type || '').replace('OrderType.', '')}</td>
                  <td className="text-right text-gray-400">{(o.status || '').replace('OrderStatus.', '')}</td>
                  <td className="text-right text-gray-500">{o.submitted_at?.slice(11, 19) || '—'}</td>
                  <td className="text-right">
                    {(o.status || '').includes('NEW') || (o.status || '').includes('ACCEPTED') ? (
                      <button disabled={busy} onClick={() => cancelOrd(o.id)} className="text-[10px] px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-50">Cancel</button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------- Main App ----------
function App() {
  const [overview, setOverview] = useState([]);
  const [selected, setSelected] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [view, setView] = useState('charts'); // 'charts' | 'trading'

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
      <header className="surface sticky top-0 z-20 px-5 py-2.5 flex items-center justify-between">
        <div className="flex items-center gap-5">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center text-sm shadow-lg shadow-blue-500/20">📈</div>
            <div className="text-[15px] font-bold tracking-tight bg-gradient-to-r from-white to-blue-200 bg-clip-text text-transparent">StockTA</div>
          </div>
          <nav className="flex surface-soft rounded-xl p-1 text-xs">
            {[
              { id: 'charts', label: 'Charts & Analysis' },
              { id: 'trading', label: '📒 Trading' },
            ].map(t => (
              <button
                key={t.id}
                onClick={() => setView(t.id)}
                className={`px-3.5 py-1.5 rounded-lg font-medium ${view === t.id ? 'bg-gradient-to-b from-blue-500 to-blue-600 text-white glow-blue' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </div>
        <div className="text-xs text-gray-400 flex items-center gap-4">
          <span className="hidden sm:inline text-gray-500">Auto-scan every 15 min · Polling every 60s</span>
          <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full surface-soft ${liveConnected ? 'text-emerald-400' : 'text-gray-500'}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${liveConnected ? 'bg-emerald-400 live-dot' : 'bg-gray-600'}`}></span>
            <span className="font-semibold tracking-wide text-[11px] uppercase">{liveConnected ? 'Live' : 'Offline'}</span>
          </span>
        </div>
      </header>
      <div className="flex-1 flex overflow-hidden">
        {view === 'charts' && (
          <>
            <WatchlistPanel
              overview={overviewWithLive}
              selected={selected}
              onSelect={setSelected}
              onAdd={handleAdd}
              onRemove={handleRemove}
              onRefresh={loadOverview}
            />
            <AnalysisView
              ticker={selected}
              reloadToken={reloadToken}
              liveQuote={selected ? liveQuotes[selected] : null}
              onAutoTradeChanged={loadOverview}
            />
          </>
        )}
        {view === 'trading' && (
          <div className="flex-1 overflow-y-auto scrollbar-thin p-4 space-y-4">
            <AutoTraderPanel reloadToken={reloadToken} />
            <TradingPanel reloadToken={reloadToken} />
          </div>
        )}
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
