"""Pydantic models for internal data shapes.

Historically the trading flow passes around `Dict[str, Any]` for signals
and trade snapshots — flexible but prone to `KeyError` at runtime and
bypasses IDE completion. These models define the canonical shape.

**Migration policy** — additive. Existing call sites still work with
plain dicts. New code is encouraged to construct `SignalData` /
`TradeContext` / etc. directly. Helper factories (`SignalData.from_dict`)
convert when crossing a boundary.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict, Any, Literal

from pydantic import BaseModel, Field, ConfigDict


class SignalData(BaseModel):
    """Canonical signal shape used by signal_generator.generate_signal()."""
    model_config = ConfigDict(extra="allow")  # tolerate extra fields during migration

    ticker: str
    signal_type: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(..., ge=0, le=100)
    timeframe: Optional[str] = None
    strategy: Optional[str] = None
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target1: Optional[float] = None
    target2: Optional[float] = None
    target3: Optional[float] = None
    reasoning: Optional[str] = None
    patterns: Optional[List[str]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignalData":
        """Tolerant conversion — fills only the fields we care about."""
        return cls(
            ticker=d.get("ticker", "?"),
            signal_type=d.get("signal_type", "HOLD"),
            confidence=float(d.get("confidence") or 0),
            timeframe=d.get("timeframe"),
            strategy=d.get("strategy"),
            entry=d.get("entry"),
            stop_loss=d.get("stop_loss"),
            target1=d.get("target1"),
            target2=d.get("target2"),
            target3=d.get("target3"),
            reasoning=d.get("reasoning"),
            patterns=d.get("patterns"),
        )


class TradeContext(BaseModel):
    """Snapshot of an AutoTrade at a moment in time (e.g. when writing a note).
    Narrower than the AutoTrade ORM row; only the fields signal logic needs."""
    model_config = ConfigDict(extra="allow")

    trade_id: int
    ticker: str
    asset_type: Literal["stock", "option"]
    symbol: Optional[str] = None
    side: Literal["buy", "sell"]
    qty: float
    entry_price: Optional[float] = None
    requested_entry: Optional[float] = None
    stop_loss: Optional[float] = None
    current_stop: Optional[float] = None
    target1: Optional[float] = None
    target2: Optional[float] = None
    target3: Optional[float] = None
    level_index: int = 0
    status: str
    opened_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None

    @classmethod
    def from_auto_trade(cls, t: Any) -> "TradeContext":
        """Build from an AutoTrade ORM row."""
        return cls(
            trade_id=t.id, ticker=t.ticker, asset_type=t.asset_type,
            symbol=getattr(t, "symbol", None), side=t.side, qty=float(t.qty or 0),
            entry_price=t.entry_price, requested_entry=getattr(t, "requested_entry", None),
            stop_loss=t.stop_loss, current_stop=t.current_stop,
            target1=t.target1, target2=t.target2, target3=t.target3,
            level_index=int(t.level_index or 0), status=t.status,
            opened_at=t.opened_at, filled_at=getattr(t, "filled_at", None),
        )


class MultiplierStack(BaseModel):
    """Structured breakdown of the risk-multiplier stack for audit logs."""
    confidence: float = Field(..., gt=0)
    kelly: float = Field(..., gt=0)
    calibration: float = Field(..., gt=0)
    strategy: float = Field(..., gt=0)
    vix: float = Field(..., gt=0)

    @property
    def raw(self) -> float:
        return self.confidence * self.kelly * self.calibration * self.strategy * self.vix

    @property
    def clamped(self) -> float:
        """Clamp to RISK_MULT_CEILING from config (currently 2.0)."""
        from services.config import RISK_MULT_CEILING
        return min(self.raw, RISK_MULT_CEILING)


class MacroBlackoutStatus(BaseModel):
    in_blackout: bool
    event_key: Optional[str] = None
    event_name: Optional[str] = None
    importance: Optional[str] = None
    reason: Optional[str] = None
