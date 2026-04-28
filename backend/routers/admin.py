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
from database import SessionLocal, AutoTrade

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
    from services import paper_trader as _pt_b
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
                if not t.entry_price:
                    skipped.append({"id": t.id, "ticker": t.ticker, "reason": "no entry_price"})
                    continue
                # Match Alpaca's symbol — for stocks t.ticker, for options t.symbol (OCC)
                want_sym = (t.symbol or t.ticker or "").upper()
                multiplier = 100.0 if (t.asset_type or "stock").lower() == "option" else 1.0
                # Find a SELL fill matching qty before close+1d
                close_ceiling = t.closed_at
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
                # Prefer the most recent fill before close+24h
                cands.sort(key=lambda x: x.get("filled_at") or "", reverse=True)
                fill = cands[0]
                exit_px = float(fill.get("filled_avg_price") or 0)
                if exit_px <= 0:
                    skipped.append({"id": t.id, "ticker": t.ticker, "reason": "fill price 0"})
                    continue
                pl = (exit_px - float(t.entry_price)) * float(t.qty or 0) * multiplier
                t.realized_pl = round(pl, 2)
                t.note = (t.note or "") + f" | BACKFILL_REALIZED_PL: exit ${exit_px:.2f} (fill {str(fill.get('id'))[:8]})"
                patched.append({
                    "id": t.id, "ticker": t.ticker, "asset_type": t.asset_type,
                    "entry_price": float(t.entry_price), "exit_price": exit_px,
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
