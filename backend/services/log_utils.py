"""
Lightweight structured-logging helpers.

The existing codebase logs free-form strings like
    f"AutoTrader skip {ticker}: idempotent dup of trade #{recent_dup.id}"
which is fine for a human tail, but useless for grep-based ops queries
("how many idempotent dups today?"). These helpers let new code emit the
same human line PLUS a parseable `key=value` tail without pulling in a
real structured-logging library.

Usage:
    from services.log_utils import kv, with_emoji
    logger.info(kv("AutoTrader skip idempotent dup",
                   ticker=ticker, signal_id=sig.id, dup_trade=dup.id))
    # → "AutoTrader skip idempotent dup ticker=AAPL signal_id=42 dup_trade=17"

The emoji map keeps decorative prefixes out of the data layer — services
log plain strings, the UI/CLI applies the emoji it wants.
"""
from typing import Any


def kv(message: str, **fields: Any) -> str:
    """Append `key=value` pairs to a log message in a stable order.

    Values are coerced via `repr` only when they contain whitespace, so
    common cases (numbers, short symbols) stay readable.
    """
    if not fields:
        return message
    parts = [message]
    for k, v in fields.items():
        s = str(v)
        if " " in s or "=" in s:
            s = repr(s)
        parts.append(f"{k}={s}")
    return " ".join(parts)


# Decorative prefix map — services should NOT bake these into log strings.
# A UI layer (frontend, CLI tail script) can apply them when rendering.
EVENT_EMOJI = {
    "trade_open":   "🟢",
    "trade_close":  "🔴",
    "signal_new":   "✨",
    "stop_trail":   "↗️",
    "post_mortem":  "🔬",
    "regime_chop":  "〰️",
    "regime_trend": "📈",
}


def with_emoji(event: str, message: str) -> str:
    """Render `<emoji> <message>` for a known event tag, or just message."""
    e = EVENT_EMOJI.get(event)
    return f"{e} {message}" if e else message
