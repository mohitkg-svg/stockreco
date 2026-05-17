"""r96 R5: live option-Greeks persistence pipeline.

The audit flagged that risk_manager.portfolio_greeks falls back to crude
defaults (delta=±0.4, gamma=0, theta=-0.05, vega=0.10) when an option
AutoTrade row has null entry_delta/gamma/theta/vega. Pre-r48 rows are
missing them entirely; new rows also miss them when the OPRA feed didn't
return Greeks at quote time.

This module exposes:

  * `backfill_missing_greeks(force_refresh=False)` — iterates currently
    open + pending option positions, calls options_fetcher to re-fetch
    the OCC contract's current quote (which carries live Greeks from the
    feed), and writes entry_delta/gamma/theta/vega on rows that have
    them as NULL. With force_refresh=True, overwrites existing values too
    (mark-to-market refresh).

  * `backfill_one(trade_id, db=None)` — single-row helper, used by ad-hoc
    operator commands.

The scheduled job calls backfill_missing_greeks() only when
cfg.live_greeks_backfill_enabled is True. Operator can also trigger the
backfill via POST /api/admin/backfill-option-greeks regardless of flag —
the flag only gates the periodic schedule.
"""
from __future__ import annotations
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def _fetch_contract_greeks(occ_symbol: str) -> Optional[Dict[str, Optional[float]]]:
    """Pull current quote + Greeks for an OCC symbol via options_fetcher.
    Returns {delta, gamma, theta, vega, iv} or None on failure. All values
    optional — feeds vary in coverage. Reads the chain (Polygon → Alpaca →
    Yahoo dispatch, 10min cache) and matches the OCC symbol's strike+type
    on the matching expiration."""
    try:
        from services.options_fetcher import _parse_occ, fetch_option_chain
        parsed = _parse_occ(occ_symbol)
        if not parsed:
            return None
        chain = fetch_option_chain(parsed["underlying"], parsed["expiration_epoch"])
        if not chain:
            return None
        side_list = chain.get("calls") if parsed["contract_type"] == "call" else chain.get("puts")
        if not side_list:
            return None
        target_strike = float(parsed["strike"])
        for c in side_list:
            try:
                if abs(float(c.get("strike") or 0) - target_strike) < 0.005:
                    return {
                        "delta": c.get("delta"),
                        "gamma": c.get("gamma"),
                        "theta": c.get("theta"),
                        "vega": c.get("vega"),
                        "iv": c.get("iv") or c.get("impliedVolatility"),
                    }
            except Exception:
                continue
        return None
    except Exception as e:
        logger.debug(f"option_greeks._fetch_contract_greeks({occ_symbol}): {e}")
        return None


def backfill_one(trade, db, force_refresh: bool = False) -> Dict[str, Any]:
    """Backfill entry_delta/gamma/theta/vega on a single AutoTrade row.
    Returns a summary {ticker, updated_fields}. Idempotent — when all
    fields are already populated and force_refresh=False, returns
    {updated_fields: []}.
    """
    if (trade.asset_type or "") != "option" or not trade.symbol:
        return {"ticker": trade.ticker, "skipped": "not option"}
    needed = []
    if force_refresh or trade.entry_delta is None:
        needed.append("delta")
    if force_refresh or trade.entry_gamma is None:
        needed.append("gamma")
    if force_refresh or trade.entry_theta is None:
        needed.append("theta")
    if force_refresh or trade.entry_vega is None:
        needed.append("vega")
    if not needed:
        return {"ticker": trade.ticker, "updated_fields": []}
    greeks = _fetch_contract_greeks(trade.symbol)
    if not greeks:
        return {"ticker": trade.ticker, "skipped": "no quote"}
    updated = []
    if "delta" in needed and greeks.get("delta") is not None:
        trade.entry_delta = float(greeks["delta"])
        updated.append("delta")
    if "gamma" in needed and greeks.get("gamma") is not None:
        trade.entry_gamma = float(greeks["gamma"])
        updated.append("gamma")
    if "theta" in needed and greeks.get("theta") is not None:
        trade.entry_theta = float(greeks["theta"])
        updated.append("theta")
    if "vega" in needed and greeks.get("vega") is not None:
        trade.entry_vega = float(greeks["vega"])
        updated.append("vega")
    if updated:
        try:
            db.commit()
        except Exception as e:
            logger.warning(f"option_greeks.backfill_one commit failed: {e}")
            db.rollback()
            return {"ticker": trade.ticker, "error": str(e)}
    return {"ticker": trade.ticker, "symbol": trade.symbol, "updated_fields": updated}


def backfill_missing_greeks(force_refresh: bool = False) -> Dict[str, Any]:
    """Sweep all open + pending option positions and backfill Greeks where
    missing. Returns {checked, updated, skipped, errors}."""
    from database import SessionLocal, AutoTrade
    out = {"checked": 0, "updated": 0, "skipped": 0, "errors": 0, "rows": []}
    db = SessionLocal()
    try:
        rows = db.query(AutoTrade).filter(
            AutoTrade.asset_type == "option",
            AutoTrade.status.in_(["open", "pending"]),
        ).all()
        for t in rows:
            out["checked"] += 1
            try:
                res = backfill_one(t, db, force_refresh=force_refresh)
                if res.get("updated_fields"):
                    out["updated"] += 1
                    out["rows"].append(res)
                else:
                    out["skipped"] += 1
            except Exception as e:
                out["errors"] += 1
                logger.warning(f"option_greeks.backfill_missing_greeks {t.ticker}: {e}")
    finally:
        db.close()
    return out


def backfill_enabled() -> bool:
    """Cheap accessor — used by the scheduler tick to decide whether to
    run the periodic backfill. Manual endpoint ignores this flag."""
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            return bool(getattr(cfg, "live_greeks_backfill_enabled", False)) if cfg else False
        finally:
            db.close()
    except Exception:
        return False
