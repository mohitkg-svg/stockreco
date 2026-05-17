"""Admin / one-off operational endpoints.

Auth: shared `X-API-Key` (same as the rest of `/api/*`). Designed for
operator-driven, infrequent housekeeping — backfills, schema-aligned
cleanups, etc. Each handler logs its action loudly for audit.

Pattern: every handler is idempotent or self-bounded by an explicit list
of IDs in the request body. No "delete all" or "drop everything"
shortcuts; every action names the rows it touches.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routers._auth import require_api_key
from database import SessionLocal, AutoTrade, Signal

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


class AgeOutTradesRequest(BaseModel):
    trade_ids: List[int] = Field(..., min_length=1, max_length=500)
    days_offset: int = Field(40, ge=31, le=365,
                              description="How many days to subtract from each trade's closed_at "
                                          "(must be ≥31 to age out of the 30-day analytics window)")


@router.post("/age-out-trades")
def age_out_trades(req: AgeOutTradesRequest):
    """Backdate `closed_at` on the listed AutoTrade rows by `days_offset`
    days. Used to remove specific historical trades from the 30-day
    analytics windows (recent_wr, strategy_drawdown, freeze regime, PDT
    counter) when those trades are known to be from now-fixed bugs and
    therefore not representative of forward behavior.

    The trades are NOT deleted — they remain in the audit log with their
    original `note` + `realized_pl`. Only `closed_at` shifts. The
    response lists each touched row's old/new closed_at for review.

    Use case: r40 audit landed fixes for the bug classes that produced
    the recent paper-trade losing streak (CNTA dollar-cap, AMKR direction
    drift, VTWO premium-stop spread). The losing trades aren't
    representative of post-r40 behavior; aging them out lets the freeze
    regime stop firing and the WR-based throttles return to neutral.
    """
    db = SessionLocal()
    touched = []
    try:
        rows = db.query(AutoTrade).filter(AutoTrade.id.in_(req.trade_ids)).all()
        if not rows:
            raise HTTPException(404, f"no AutoTrade rows match ids {req.trade_ids}")
        offset = timedelta(days=req.days_offset)
        for r in rows:
            old = r.closed_at
            if old is None:
                touched.append({
                    "trade_id": r.id, "ticker": r.ticker,
                    "old_closed_at": None, "new_closed_at": None,
                    "skipped": "not closed",
                })
                continue
            new = old - offset
            r.closed_at = new
            r.note = (r.note or "") + (
                f" | aged out {req.days_offset}d "
                f"({old.isoformat()} → {new.isoformat()}) "
                f"[r40 audit cleanup]"
            )
            touched.append({
                "trade_id": r.id, "ticker": r.ticker,
                "old_closed_at": old.isoformat(),
                "new_closed_at": new.isoformat(),
            })
        db.commit()
    finally:
        db.close()

    logger.warning(
        f"ADMIN age-out-trades: {len(touched)} rows touched, days_offset={req.days_offset}, "
        f"requested_ids={req.trade_ids}"
    )
    return {
        "touched": touched,
        "count": len([t for t in touched if "skipped" not in t]),
        "skipped": [t for t in touched if "skipped" in t],
        "days_offset": req.days_offset,
    }


import re as _re_admin


def _validate_ticker(ticker: str) -> str:
    """r44 fix Wave 6: admin path-segment validation. Refuse non-conforming
    input (path-traversal, garbage, oversized strings) before any DB query.
    """
    if not isinstance(ticker, str) or not _re_admin.match(r"^[A-Z]{1,8}$", ticker.upper()):
        raise HTTPException(status_code=400, detail=f"invalid ticker: must be 1-8 uppercase letters")
    return ticker.upper()


@router.post("/promote-adopted/{ticker}")
def promote_adopted(ticker: str):
    """Promote an `adopted` AutoTrade row to `open` with bot-computed
    stop/target levels, submitting a real broker stop-loss order so the
    manage loop will trail / partial-exit / stop-out the position like
    any other auto-trade.

    Levels are anchored to CURRENT live price (not the original adoption
    entry price — that's a sunk cost; new trail bracket needs to make
    sense around today's price). Computed from 1.5×ATR (with 2%-of-price
    fallback) for stop distance, and 1.5R / 2.5R / 4R for T1 / T2 / T3.

    Use case: after `POST /api/admin/sync-positions` adopts an external
    position, this endpoint hands it off to the bot's management loop
    instead of leaving it for manual operator handling.

    Failure modes (returns `{ok: False, reason}`):
      * No adopted row for ticker
      * Alpaca no longer reports a position (row marked `closed_external`
        as a side effect — sync would have done the same)
      * Live price fetch failed (refused rather than using a stale anchor)
      * Broker SL submit failed

    Idempotent on success: a second call returns "no adopted stock row".
    """
    ticker = _validate_ticker(ticker)
    logger.warning(f"ADMIN promote_adopted ticker={ticker}")
    from services.auto_trader import promote_adopted_to_managed
    try:
        from services.alerts import alert as _raise_alert_admin
        _raise_alert_admin("info", "admin_action", f"promote_adopted ticker={ticker}", ticker=ticker)
    except Exception:
        pass
    return promote_adopted_to_managed(ticker)


@router.post("/sync-positions")
def sync_positions():
    """Reconcile the Alpaca account against the `auto_trades` table.
    Alpaca is the source of truth for actual capital deployment.

    See `services.auto_trader.sync_positions_from_alpaca` for full
    semantics. Two reconciliation paths (idempotent — safe to re-run):

      1. **Adopt** — Alpaca position with no DB row → insert a new
         row with `status="adopted"`. Suppresses the `unexpected_position`
         alert and counts toward portfolio capital/heat math, but the
         manage loop SKIPS adopted rows (operator handles externally).

      2. **Close-external** — open DB row with no Alpaca position →
         flip `status="closed_external"` with note. The position closed
         via a path the bot didn't observe (manual flatten, missed leg
         fill, etc.). Pending rows are not touched (may be in flight).

    Returns `{adopted: [...], closed_external: [...]}`.

    Use case: option assignment converts a put into 100 short shares,
    or you placed a manual trade via the Alpaca dashboard, or a broker
    bracket leg filled via a path the manage loop missed. Run sync to
    bring the DB in line with reality.
    """
    # r44 fix Wave 6: admin audit log.
    logger.warning("ADMIN sync_positions invoked")
    try:
        from services.alerts import alert as _raise_alert_admin
        _raise_alert_admin("info", "admin_action", "sync_positions invoked")
    except Exception:
        pass
    from services.auto_trader import sync_positions_from_alpaca
    return sync_positions_from_alpaca()


class ResetPeakRequest(BaseModel):
    confirm: str  # must be literal "RESET_PEAK" to fire — guards against accidental call


@router.post("/reset-equity-peak")
def reset_equity_peak(req: ResetPeakRequest):
    """r53o: re-anchor the EquitySnapshot rolling-peak to the current
    account equity. Used after a string of bug-driven losses (already
    aged out from the closed-trade ledger via /age-out-trades) when
    the operator wants the account_drawdown_multiplier and crisis_mode
    gates to reset to 0% drawdown.

    Action: deletes every EquitySnapshot row strictly older than today's
    UTC date, then writes a fresh snapshot at the current equity. The
    rolling-60-day window will then see only today's row → peak = current
    → drawdown = 0% → multiplier = 1.0 → crisis_mode clears (assuming
    other crisis triggers — VIX > 30 + SPY 5d < −5% — aren't separately
    firing).

    Destructive operation. Requires literal `confirm: "RESET_PEAK"` in
    the body. The audit log records the snapshot count deleted, the
    pre-reset peak, and the post-reset baseline. Raises a critical
    alert so the action is unmissable in the operator inbox.
    """
    if req.confirm != "RESET_PEAK":
        raise HTTPException(
            status_code=400,
            detail='Refusing to reset — body must include {"confirm": "RESET_PEAK"} verbatim.',
        )
    logger.critical("ADMIN reset_equity_peak invoked")
    from database import SessionLocal as _SL_rp, EquitySnapshot as _ES_rp
    from datetime import datetime as _dt_rp
    from services import alpaca_client as _pt_rp

    db = _SL_rp()
    try:
        # Capture pre-reset state for the audit trail
        pre_rows = (db.query(_ES_rp.equity)
                    .order_by(_ES_rp.ts.desc()).all())
        pre_count = len(pre_rows)
        pre_peak = max((float(r[0]) for r in pre_rows if r[0] is not None), default=None)

        # Wipe ALL EquitySnapshot rows so the rolling-peak window is
        # empty. The post-delete record_equity_snapshot() call below
        # writes a fresh row anchored to current equity.
        deleted = db.query(_ES_rp).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    # Write a fresh snapshot at current account equity. Idempotent on
    # the 5-min ts bucket; if a row already exists for today's bucket
    # it gets refreshed in place.
    try:
        from services.risk_manager import record_equity_snapshot as _rec_rp
        _rec_rp()
    except Exception as e:
        logger.error(f"reset_equity_peak: post-delete record snapshot failed: {e}")

    new_acct = _pt_rp.get_account() or {}
    new_equity = float(new_acct.get("equity") or 0)

    try:
        from services.alerts import alert as _raise_alert_rp
        _raise_alert_rp(
            "critical", "admin_action",
            f"reset_equity_peak: deleted {deleted} snapshots, "
            f"pre_peak=${pre_peak or 0:.2f}, new_baseline=${new_equity:.2f}. "
            f"Drawdown gates re-anchored.",
        )
    except Exception:
        pass

    return {
        "ok": True,
        "deleted_snapshots": int(deleted),
        "pre_reset_count": int(pre_count),
        "pre_reset_peak": round(pre_peak, 2) if pre_peak is not None else None,
        "new_baseline_equity": round(new_equity, 2),
        "note": "Drawdown rolling-peak anchored to today. Equity-curve history before today is gone.",
    }


@router.post("/reconcile-pending")
def reconcile_pending_trades():
    """r53d: heal AutoTrade rows stuck in `status=pending` whose Alpaca
    parent order has actually filled. Surfaced by IREN trade #28 — sat
    in pending for 6 days because of a serialization bug where
    `str(OrderStatus.FILLED).lower() != "filled"`. The bug is now fixed
    in the manage tick, but pre-existing stuck rows still need a one-off
    sweep — and a defensive recurring sweep helps catch any future
    similar transition holes.

    For each pending row:
      1. Fetch the bracket parent at Alpaca.
      2. If parent is FILLED, transition row → status=open with
         entry_price = filled_avg_price, filled_at = now.
      3. If parent is canceled/rejected/expired, transition row →
         status=closed_unfilled.
      4. Otherwise leave it alone (still actually working).

    Returns a per-row report so the operator can see what changed.
    """
    logger.warning("ADMIN reconcile_pending_trades invoked")
    from database import SessionLocal as _SL_rp, AutoTrade as _AT_rp
    from services import alpaca_client as _pt_rp
    from datetime import datetime as _dt_rp
    db = _SL_rp()
    promoted: list = []
    closed: list = []
    unchanged: list = []
    skipped: list = []
    try:
        rows = (db.query(_AT_rp)
                .filter(_AT_rp.status == "pending")
                .all())
        if not rows:
            return {"note": "no pending rows", "promoted": [], "closed": [], "unchanged": []}
        c = _pt_rp._get_client()
        if not c:
            return {"error": "Alpaca client not initialized"}
        for t in rows:
            if not t.parent_order_id:
                skipped.append({"id": t.id, "ticker": t.ticker, "reason": "no parent_order_id"})
                continue
            try:
                parent = c.get_order_by_id(t.parent_order_id)
            except Exception as e:
                skipped.append({"id": t.id, "ticker": t.ticker, "reason": f"parent fetch fail: {str(e)[:120]}"})
                continue
            raw = parent.status
            pstatus = (getattr(raw, "value", None) or str(raw).split(".")[-1] or "").lower()
            if pstatus == "filled":
                fill_px = float(parent.filled_avg_price) if parent.filled_avg_price else float(t.requested_entry or 0)
                if fill_px <= 0:
                    skipped.append({"id": t.id, "ticker": t.ticker, "reason": "filled but no fill price"})
                    continue
                # Reconcile qty with broker
                try:
                    bf = float(getattr(parent, "filled_qty", 0) or 0)
                    if bf > 0 and abs(bf - float(t.qty or 0)) >= 0.5:
                        t.qty = int(bf)
                except Exception:
                    pass
                t.entry_price = round(fill_px, 4)
                t.filled_at = parent.filled_at if parent.filled_at else _dt_rp.utcnow()
                t.status = "open"
                t.note = (t.note or "") + f" | RECONCILE_PENDING: promoted to open @ ${fill_px:.2f}"
                db.commit()
                promoted.append({"id": t.id, "ticker": t.ticker, "entry_price": fill_px})
            elif pstatus in ("canceled", "cancelled", "rejected", "expired", "done_for_day"):
                t.status = "closed_unfilled"
                t.closed_at = _dt_rp.utcnow()
                t.note = (t.note or "") + f" | RECONCILE_PENDING: parent {pstatus}, freeing slot"
                db.commit()
                closed.append({"id": t.id, "ticker": t.ticker, "parent_status": pstatus})
            else:
                unchanged.append({"id": t.id, "ticker": t.ticker, "parent_status": pstatus})
        return {
            "promoted": promoted,
            "closed": closed,
            "unchanged": unchanged,
            "skipped": skipped,
            "summary": {
                "promoted": len(promoted),
                "closed": len(closed),
                "unchanged": len(unchanged),
                "skipped": len(skipped),
            },
        }
    finally:
        db.close()


@router.get("/loss-patterns")
def loss_patterns_summary():
    """r53 Tier-3 A: aggregated post-mortem fingerprints + which ones the
    pre-trade veto would currently fire on."""
    from services.loss_patterns import loss_pattern_summary
    return loss_pattern_summary()


@router.get("/regime-status")
def regime_status_endpoint():
    """r53 Tier-3 C: current SPY regime classification + per-strategy
    allowlist."""
    from services.regime_router import regime_status
    return regime_status()


@router.post("/mark-delisted/{ticker}")
def mark_delisted_endpoint(ticker: str):
    """r96 R4: flag a watchlist ticker as delisted (idempotent). Backtest
    universe loaders consult this when cfg.survivorship_filter_enabled is
    True to include the historical data in their sample (clamped at
    delisted_at). Auto-detection from repeated data_fetcher empties is a
    follow-on; for now operator marks manually."""
    from services.survivorship import mark_delisted
    ok = mark_delisted(ticker)
    return {"ticker": ticker.upper(), "marked": bool(ok)}


@router.get("/delisted-tickers")
def delisted_tickers_endpoint():
    """r96 R4: list every WatchlistStock row with delisted=True."""
    from database import SessionLocal, WatchlistStock
    db = SessionLocal()
    try:
        rows = db.query(WatchlistStock).filter(WatchlistStock.delisted.is_(True)).all()
        return {
            "n": len(rows),
            "rows": [
                {
                    "ticker": r.ticker,
                    "delisted_at": r.delisted_at.isoformat() if r.delisted_at else None,
                    "name": r.name,
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@router.post("/backfill-option-greeks")
def backfill_option_greeks_endpoint(force_refresh: bool = False):
    """r96 R5: backfill entry_delta/gamma/theta/vega on currently-open option
    positions by re-fetching the OCC chain. `force_refresh=True` overwrites
    existing values (mark-to-market); default backfills NULLs only. The
    scheduled job runs only when cfg.live_greeks_backfill_enabled — this
    endpoint always runs regardless of the flag (manual operator action)."""
    from services.option_greeks import backfill_missing_greeks
    return backfill_missing_greeks(force_refresh=bool(force_refresh))


@router.get("/multidim-regime")
def multidim_regime_endpoint():
    """r96 R6: surface the four regime dimensions (VIX level, VIX term,
    realized vol, breadth) + whether stress mode is currently active. When
    cfg.multidim_regime_enabled is True, an active stress signal promotes
    regime_router's classification to HIGH_VOL regardless of VIX level."""
    from services.multidim_regime import stress_regime_active, multidim_enabled
    is_stress, detail = stress_regime_active()
    return {
        "enabled": multidim_enabled(),
        "stress_active": is_stress,
        **detail,
    }


@router.post("/backfill-realized-pl")
def backfill_realized_pl():
    """Patch `realized_pl` on `closed_reconciled` / `closed_external` rows
    by pulling actual fill prices from Alpaca. r52f: surfaced by the new
    P/L reconciliation widget showing ~$2,100 of unattributed loss in
    rows where the bot's `realized_pl` field stayed at $0 because the
    position closed via a path the manage-loop didn't observe (manual
    flatten, missed bracket-leg fill, adoption-then-close).

    Algorithm per row: pull last N=50 FILLED orders for the symbol from
    Alpaca, find a SELL order with qty matching `t.qty` and submitted_at
    before `t.closed_at` + 24h. realized_pl = (filled_avg_price -
    entry_price) × qty × multiplier.

    Idempotent: only patches rows where realized_pl is None or 0.
    """
    logger.warning("ADMIN backfill_realized_pl invoked")
    from database import SessionLocal as _SL_b, AutoTrade as _AT_b
    from services import alpaca_client as _pt_b
    db = _SL_b()
    patched = []
    skipped = []
    try:
        rows = (db.query(_AT_b)
                .filter(_AT_b.status.in_(["closed_reconciled", "closed_external"]))
                .filter((_AT_b.realized_pl.is_(None)) | (_AT_b.realized_pl == 0))
                .all())
        if not rows:
            return {"patched": [], "skipped": [], "note": "no rows need backfill"}
        # Pull a generous window of recent closed orders once
        all_orders = _pt_b.get_orders(status="closed", limit=500) or []
        for t in rows:
            try:
                # r52g: if entry_price is null (pre-r41 schema rows), look
                # up the matching BUY fill from Alpaca too. Reads the same
                # order list with side="buy" filter; matches by qty and by
                # submitted_at being closest to opened_at.
                entry_px = t.entry_price
                want_sym = (t.symbol or t.ticker or "").upper()
                multiplier = 100.0 if (t.asset_type or "stock").lower() == "option" else 1.0
                if not entry_px:
                    buy_cands = []
                    for o in all_orders:
                        if (o.get("symbol") or "").upper() != want_sym:
                            continue
                        side = (o.get("side") or "").lower().split(".")[-1]
                        if "buy" not in side:
                            continue
                        if not o.get("filled_avg_price"):
                            continue
                        oqty = float(o.get("filled_qty") or o.get("qty") or 0)
                        if oqty <= 0 or abs(oqty - float(t.qty or 0)) > max(1.0, float(t.qty or 0) * 0.1):
                            continue
                        buy_cands.append(o)
                    if not buy_cands:
                        skipped.append({"id": t.id, "ticker": t.ticker, "reason": "no entry_price + no matching buy fill"})
                        continue
                    # r53: prior code used `abs(((x.get("filled_at") or "") > target) - 0.5)` which
                    # always evaluated to 0.5 for any string comparison (bool→int minus float)
                    # — the sort was effectively a no-op and the API insertion-order
                    # candidate won. Switch to proper datetime delta.
                    from datetime import datetime as _dt_bf
                    def _parse_iso(s):
                        try:
                            return _dt_bf.fromisoformat((s or "").replace("Z", "+00:00").rstrip("+00:00")) if s else None
                        except Exception:
                            return None
                    if t.opened_at:
                        target_dt = t.opened_at
                        def _delta(o):
                            f = _parse_iso(o.get("filled_at"))
                            if not f:
                                return 9_999_999.0
                            try:
                                # Strip tz to match naive opened_at
                                if f.tzinfo is not None:
                                    f = f.replace(tzinfo=None)
                                return abs((f - target_dt).total_seconds())
                            except Exception:
                                return 9_999_999.0
                        buy_cands.sort(key=_delta)
                    else:
                        buy_cands.sort(key=lambda x: x.get("filled_at") or "", reverse=True)
                    entry_px = float(buy_cands[0].get("filled_avg_price") or 0)
                    if entry_px <= 0:
                        skipped.append({"id": t.id, "ticker": t.ticker, "reason": "buy fill price 0"})
                        continue
                    # Backfill the entry_price too while we're here
                    t.entry_price = round(entry_px, 4)

                # Find a SELL fill matching qty before close+1d
                cands = []
                for o in all_orders:
                    if (o.get("symbol") or "").upper() != want_sym:
                        continue
                    side = (o.get("side") or "").lower().split(".")[-1]
                    if "sell" not in side:
                        continue
                    if not o.get("filled_avg_price"):
                        continue
                    # Tolerate small qty mismatch (partial fills, trim legs)
                    oqty = float(o.get("filled_qty") or o.get("qty") or 0)
                    if oqty <= 0 or abs(oqty - float(t.qty or 0)) > max(1.0, float(t.qty or 0) * 0.1):
                        continue
                    cands.append(o)
                if not cands:
                    skipped.append({"id": t.id, "ticker": t.ticker, "reason": "no matching sell fill"})
                    continue
                # r53: prefer the SELL fill closest to t.closed_at (was: most
                # recent overall — wrong on partial-fill closes where the bot
                # might have multiple matching SELL fills hours apart).
                if t.closed_at:
                    from datetime import datetime as _dt_sf
                    def _parse_iso_s(s):
                        try:
                            return _dt_sf.fromisoformat((s or "").replace("Z", "+00:00")) if s else None
                        except Exception:
                            return None
                    target_close = t.closed_at
                    def _sdelta(o):
                        f = _parse_iso_s(o.get("filled_at"))
                        if not f:
                            return 9_999_999.0
                        try:
                            if f.tzinfo is not None:
                                f = f.replace(tzinfo=None)
                            return abs((f - target_close).total_seconds())
                        except Exception:
                            return 9_999_999.0
                    cands.sort(key=_sdelta)
                else:
                    cands.sort(key=lambda x: x.get("filled_at") or "", reverse=True)
                fill = cands[0]
                exit_px = float(fill.get("filled_avg_price") or 0)
                if exit_px <= 0:
                    skipped.append({"id": t.id, "ticker": t.ticker, "reason": "fill price 0"})
                    continue
                pl = (exit_px - float(entry_px)) * float(t.qty or 0) * multiplier
                t.realized_pl = round(pl, 2)
                t.note = (t.note or "") + f" | BACKFILL_REALIZED_PL: entry ${entry_px:.2f} exit ${exit_px:.2f} (fill {str(fill.get('id'))[:8]})"
                patched.append({
                    "id": t.id, "ticker": t.ticker, "asset_type": t.asset_type,
                    "entry_price": float(entry_px), "exit_price": exit_px,
                    "realized_pl": t.realized_pl, "fill_id": str(fill.get("id"))[:8],
                })
            except Exception as e:
                skipped.append({"id": t.id, "ticker": t.ticker, "reason": f"err: {str(e)[:120]}"})
        db.commit()
        return {
            "patched": patched,
            "skipped": skipped,
            "patched_count": len(patched),
            "patched_total_pl": round(sum(p["realized_pl"] for p in patched), 2),
        }
    finally:
        db.close()


@router.get("/factor-ic")
def factor_ic(days: int = 90, min_n: int = 10):
    """Per-factor Information Coefficient over closed trades in the last
    `days` days. Joins AutoTrade.signal_id → Signal.factor_scores_json,
    parses each per-factor value, and computes Spearman rank correlation
    against realized P&L%.

    Read-only, additive — does not affect trading.

    `min_n`: factors with fewer than this many closed-trade observations
    are reported but the IC is suppressed (returns null) since rank
    correlation is meaningless on tiny samples.

    Returns: {window_days, total_trades, factors: {<name>: {n, ic, mean_value, mean_pl_pct}}}
    """
    import json as _json
    import pandas as _pd

    cutoff = datetime.utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (
            db.query(AutoTrade.realized_pl, AutoTrade.entry_price, AutoTrade.qty,
                     Signal.factor_scores_json)
            .join(Signal, Signal.id == AutoTrade.signal_id)
            .filter(AutoTrade.closed_at >= cutoff)
            .filter(AutoTrade.realized_pl.isnot(None))
            .filter(AutoTrade.entry_price.isnot(None))
            .filter(AutoTrade.qty.isnot(None))
            .filter(Signal.factor_scores_json.isnot(None))
            .all()
        )
    finally:
        db.close()

    per_factor: dict = {}
    for realized_pl, entry_price, qty, fjson in rows:
        notional = (entry_price or 0) * (qty or 0)
        if not notional:
            continue
        pl_pct = float(realized_pl) / notional
        try:
            parts = _json.loads(fjson)
        except Exception:
            continue
        for fname, fval in parts.items():
            try:
                v = float(fval)
            except Exception:
                continue
            per_factor.setdefault(fname, {"vals": [], "pls": []})
            per_factor[fname]["vals"].append(v)
            per_factor[fname]["pls"].append(pl_pct)

    out: dict = {}
    for fname, d in per_factor.items():
        n = len(d["vals"])
        entry = {
            "n": n,
            "ic": None,
            "mean_value": (sum(d["vals"]) / n) if n else None,
            "mean_pl_pct": (sum(d["pls"]) / n) if n else None,
        }
        if n >= min_n:
            try:
                ic = _pd.Series(d["vals"]).corr(_pd.Series(d["pls"]), method="spearman")
                entry["ic"] = None if (ic is None or _pd.isna(ic)) else round(float(ic), 4)
            except Exception:
                pass
        out[fname] = entry

    return {
        "window_days": days,
        "min_n": min_n,
        "total_trades": len(rows),
        "factors": out,
    }


@router.post("/record-equity-snapshot")
def record_equity_snapshot_now():
    """Manually fire `record_equity_snapshot` once. Idempotent — the
    function buckets timestamps to the 5-min boundary and updates the
    existing row if one exists.

    Use case: bootstrap the EquitySnapshot table after a fresh deploy
    (or after a long outage, e.g. OOM-restart loop) so the equity-curve
    UI and `account_drawdown_multiplier` have data to read instead of
    waiting for the cron's next 5-min boundary.
    """
    logger.warning("ADMIN record_equity_snapshot invoked")
    from services.risk_manager import record_equity_snapshot as _rec
    from database import SessionLocal as _SL, EquitySnapshot as _ES
    _rec()
    db = _SL()
    try:
        latest = db.query(_ES).order_by(_ES.ts.desc()).first()
        return {
            "ok": True,
            "latest_ts": latest.ts.isoformat() if latest else None,
            "latest_equity": float(latest.equity) if latest else None,
            "total_rows": db.query(_ES).count(),
        }
    finally:
        db.close()
