"""AI-judge layer — Claude as a veto / context layer over the rule engine.

Three call sites this module supports:

  * `entry_veto(signal, context)` — fired by `consider_signal` after every
    other gate has passed. Claude returns either `{verdict: "proceed"}`
    (no-op) or `{verdict: "skip", reason: ...}` (rejects the trade).

  * `news_exit_decision(trade, news_item)` — fired from the manage loop
    when fresh news arrives on an open position. Claude returns
    `{action: "hold"|"trim"|"close", is_thesis_relevant: bool, reason}`.

  * `confidence_multiplier(signal, context)` — fired in the sizing block.
    Returns a multiplier in [AI_MULT_MIN, AI_MULT_MAX] that joins the
    existing multiplier stack (clamped by RISK_MULT_CEILING=2.0× there).

DESIGN PRINCIPLES — read before changing:

  1. **AI is a veto, not an originator.** It never emits prices, sizes,
     or order-typed parameters. The rule engine owns all numerical
     decision-making. The LLM is constrained via tool-use schemas to
     emit a tiny, validated set of categorical / multiplier values.

  2. **Fail open.** Every call has a hard timeout. Any error (network,
     API down, malformed response, schema mismatch) returns the
     "abstain" value (proceed / hold / 1.0×) — the rule engine still
     trades. The bot must never depend on Claude being up.

  3. **Shadow by default.** Each call site has an env flag. Default is
     `shadow` — log the decision, don't honor it. Set to `active` only
     after reviewing ≥ 200 shadow decisions in the AIDecisionLog table
     and confirming the verdicts agree with what you'd manually pick.

  4. **Cost-bounded.** Haiku (~$0.001/call) at ~50 high-conf signals/day
     ≈ $0.05/day. Don't move to Opus without measuring marginal accuracy
     improvement on the shadow log.
"""
from __future__ import annotations
import json
import logging
import os
import time
from typing import Dict, Any, Optional, Literal

logger = logging.getLogger(__name__)


# ---------- Mode resolution -------------------------------------------------
# Each call site has its own env flag so they can be enabled independently.
#   * "off"     — never call Claude (full bypass)
#   * "shadow"  — call Claude, log result, but DON'T honor it
#   * "active"  — call Claude AND honor the result
# Defaults are "off" for every call until you flip them via env. After flip,
# go "shadow" first for at least a week / 200 decisions before "active".

VetoMode = Literal["off", "shadow", "active"]


def _mode(env_key: str, default: str = "off") -> VetoMode:
    v = (os.getenv(env_key, default) or default).strip().lower()
    if v not in ("off", "shadow", "active"):
        return "off"
    return v  # type: ignore[return-value]


def entry_veto_mode() -> VetoMode:
    return _mode("AI_ENTRY_VETO_MODE")


def news_exit_mode() -> VetoMode:
    return _mode("AI_NEWS_EXIT_MODE")


def confidence_mult_mode() -> VetoMode:
    return _mode("AI_CONFIDENCE_MULT_MODE")


# ---------- Anthropic client (lazy + cached) --------------------------------

_client = None
_client_init_attempted = False
_client_init_last_attempt = 0.0   # r39: monotonic ts of last init attempt for 10-min retry


def _get_client():
    """Return a cached Anthropic client, or None if no API key is set.

    r39 audit cleanup: previously a single failed init permanently
    disabled AI calls until process restart (`_client_init_attempted=True`
    on first failure). Now we retry once every 10 minutes — a transient
    network blip on the first call doesn't permanently disable the AI
    judge layer for the life of the process.
    """
    global _client, _client_init_attempted, _client_init_last_attempt
    now = time.time()
    if _client is not None:
        return _client
    if _client_init_attempted and (now - _client_init_last_attempt) < 600:
        return None
    _client_init_attempted = True
    _client_init_last_attempt = now
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info("ai_judge: ANTHROPIC_API_KEY not set; all AI calls will abstain")
        return None
    try:
        import anthropic
        _client = anthropic.Anthropic(api_key=api_key)
        return _client
    except Exception as e:
        logger.warning(f"ai_judge: Anthropic client init failed: {e}; will retry in 10m")
        return None


# ---------- Decision log (DB-persisted shadow review) -----------------------

def _log_decision(call_site: str, mode: str, prompt_summary: Dict[str, Any],
                   response: Dict[str, Any], latency_ms: int,
                   honored: bool, error: Optional[str] = None) -> None:
    """Persist a decision row for shadow-mode review.

    Failure here is non-fatal — we never let a logging hiccup block a
    trade. The DB row is best-effort.
    """
    try:
        from database import SessionLocal, AIDecisionLog
        db = SessionLocal()
        try:
            row = AIDecisionLog(
                call_site=call_site,
                mode=mode,
                prompt_summary=json.dumps(prompt_summary, default=str)[:4000],
                response=json.dumps(response, default=str)[:2000],
                latency_ms=latency_ms,
                honored=honored,
                error=error[:500] if error else None,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"ai_judge: decision log skipped: {e}")


# ---------- Tool-use schemas (single source of truth per call site) ---------

_ENTRY_VETO_TOOL = {
    "name": "vote_on_entry",
    "description": "Vote on whether the proposed trade should be entered.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["proceed", "skip"],
                "description": "proceed = the rule engine's decision is fine; "
                               "skip = there's a semantic reason to NOT take this trade "
                               "that the rule engine couldn't see "
                               "(e.g. earnings/event tomorrow not in our calendar, "
                               "obvious bull-trap setup, sector-wide news that "
                               "invalidates the setup, recent CEO departure, etc.)",
            },
            "reason": {
                "type": "string",
                "description": "1-2 sentence justification. Required.",
                "maxLength": 280,
            },
        },
        "required": ["verdict", "reason"],
    },
}

_NEWS_EXIT_TOOL = {
    "name": "vote_on_news_exit",
    "description": "Decide what to do with an open position when news arrives.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_thesis_relevant": {
                "type": "boolean",
                "description": "Does this news materially impact the original trade thesis?",
            },
            "action": {
                "type": "string",
                "enum": ["hold", "trim", "close"],
                "description": "hold = keep, trim = halve, close = exit fully",
            },
            "reason": {
                "type": "string",
                "maxLength": 280,
            },
        },
        "required": ["is_thesis_relevant", "action", "reason"],
    },
}

_CONFIDENCE_MULT_TOOL = {
    "name": "rate_confidence",
    "description": "Rate the conviction of the trade for sizing purposes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "multiplier": {
                "type": "number",
                "description": (
                    "A real number in [0.6, 1.4]. 1.0 = neutral. "
                    "Below 1.0 = down-size for caution. Above 1.0 = up-size for "
                    "high conviction (use sparingly — most trades are neutral)."
                ),
            },
            "reason": {
                "type": "string",
                "maxLength": 280,
            },
        },
        "required": ["multiplier", "reason"],
    },
}


# ---------- Generic LLM call helper ----------------------------------------

def _call_with_tool(
    system_prompt: str,
    user_prompt: str,
    tool: Dict[str, Any],
    timeout_sec: float,
) -> Optional[Dict[str, Any]]:
    """Single-turn tool-forced call. Returns the tool's input dict, or None
    on any failure (network, API, schema). Caller treats None as abstain."""
    client = _get_client()
    if client is None:
        return None
    from services.config import AI_JUDGE_MODEL, AI_JUDGE_MAX_TOKENS
    try:
        # Anthropic SDK doesn't expose a per-call timeout that wraps the
        # stream cleanly; we set the SDK's own request_timeout via
        # `timeout=` (>=0.18) and let the underlying httpx raise.
        resp = client.messages.create(
            model=AI_JUDGE_MODEL,
            max_tokens=AI_JUDGE_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            timeout=timeout_sec,
        )
    except Exception as e:
        logger.warning(f"ai_judge: API call failed ({type(e).__name__}: {e}); abstaining")
        return None
    # Find the tool_use block
    try:
        for block in resp.content or []:
            if getattr(block, "type", None) == "tool_use":
                inp = getattr(block, "input", None)
                if isinstance(inp, dict):
                    return inp
        logger.warning(f"ai_judge: no tool_use block in response; abstaining")
        return None
    except Exception as e:
        logger.warning(f"ai_judge: malformed response ({e}); abstaining")
        return None


# ---------- Public call: entry veto ----------------------------------------

def entry_veto(signal: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Claude whether to proceed with this entry.

    Returns `{verdict: "proceed"|"skip", reason: str, honored: bool, mode: str}`.
    A `skip` is honored only when mode == "active". In `shadow` we log
    but always return `proceed`.

    Caller invariant: a network/API/schema failure here NEVER blocks the
    trade. The `verdict` is always "proceed" on the abstain path.
    """
    mode = entry_veto_mode()
    if mode == "off":
        return {"verdict": "proceed", "reason": "off", "honored": False, "mode": "off"}

    from services.config import AI_JUDGE_TIMEOUT_SEC
    started = time.time()
    sys_prompt = (
        "You are a risk-aware trading assistant reviewing a proposed entry "
        "AFTER it has passed every rule-based gate (price geometry, ATR-vs-stop, "
        "earnings calendar, macro blackout, liquidity, regime caps, idempotency, "
        "etc.). Your job: catch SEMANTIC reasons to skip that the rule engine "
        "couldn't see — sector-wide news, recent management changes, obvious "
        "bull/bear traps, contradictory news flow on the ticker. Default to "
        "PROCEED unless you have a concrete, specific reason to skip. "
        "Never veto on vague 'feels frothy' or 'market looks weak' — that's "
        "what the regime / macro gates already handle."
    )
    user_prompt = json.dumps({"signal": signal, "context": context}, default=str, indent=2)
    out = _call_with_tool(sys_prompt, user_prompt, _ENTRY_VETO_TOOL, AI_JUDGE_TIMEOUT_SEC)
    latency_ms = int((time.time() - started) * 1000)

    if out is None:
        # Abstain — log + return proceed
        result = {"verdict": "proceed", "reason": "ai_abstain", "honored": False, "mode": mode}
        _log_decision("entry_veto", mode, {"ticker": signal.get("ticker")},
                       result, latency_ms, honored=False, error="abstain")
        return result

    verdict = str(out.get("verdict", "proceed")).strip().lower()
    if verdict not in ("proceed", "skip"):
        verdict = "proceed"
    reason = str(out.get("reason", ""))[:280]
    honored = (mode == "active" and verdict == "skip")
    result = {"verdict": verdict, "reason": reason, "honored": honored, "mode": mode}
    _log_decision("entry_veto", mode,
                   {"ticker": signal.get("ticker"),
                    "confidence": signal.get("confidence"),
                    "timeframe": signal.get("timeframe")},
                   result, latency_ms, honored=honored)
    if verdict == "skip":
        logger.info(
            f"ai_judge entry_veto {signal.get('ticker')}: SKIP ({reason}) "
            f"[mode={mode}, honored={honored}, latency={latency_ms}ms]"
        )
    return result


# ---------- Public call: confidence multiplier -----------------------------

def confidence_multiplier(signal: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Claude for a sizing multiplier in [AI_MULT_MIN, AI_MULT_MAX].

    Returns `{multiplier: float, reason: str, honored: bool, mode: str}`.
    The multiplier is clamped to [AI_MULT_MIN, AI_MULT_MAX] regardless of
    Claude's output. In `shadow` mode the returned multiplier is 1.0
    (no effect on sizing) but the requested value is still logged.
    """
    mode = confidence_mult_mode()
    if mode == "off":
        return {"multiplier": 1.0, "reason": "off", "honored": False, "mode": "off"}

    from services.config import AI_JUDGE_TIMEOUT_SEC, AI_MULT_MIN, AI_MULT_MAX
    started = time.time()
    sys_prompt = (
        "You are sizing a trade that has already passed every rule-based "
        "filter. Output a multiplier in [0.6, 1.4] for position size: "
        "1.0 = neutral (the rule-engine size is correct), <1.0 = downsize "
        "(some semantic concern), >1.0 = upsize (rare — only when the "
        "setup AND the surrounding context are unusually clean). "
        "Most trades should be 1.0 — only deviate when you have a "
        "specific, concrete reason."
    )
    user_prompt = json.dumps({"signal": signal, "context": context}, default=str, indent=2)
    out = _call_with_tool(sys_prompt, user_prompt, _CONFIDENCE_MULT_TOOL, AI_JUDGE_TIMEOUT_SEC)
    latency_ms = int((time.time() - started) * 1000)

    if out is None:
        result = {"multiplier": 1.0, "reason": "ai_abstain", "honored": False, "mode": mode}
        _log_decision("confidence_multiplier", mode, {"ticker": signal.get("ticker")},
                       result, latency_ms, honored=False, error="abstain")
        return result

    try:
        m_raw = float(out.get("multiplier", 1.0))
    except Exception:
        m_raw = 1.0
    m_clamped = max(AI_MULT_MIN, min(AI_MULT_MAX, m_raw))
    reason = str(out.get("reason", ""))[:280]
    # In shadow mode we expose 1.0 to the caller (no effect on sizing) but
    # still log the requested value.
    effective = m_clamped if mode == "active" else 1.0
    honored = (mode == "active" and abs(m_clamped - 1.0) > 0.01)
    result = {
        "multiplier": effective,
        "shadow_multiplier": m_clamped,
        "reason": reason,
        "honored": honored,
        "mode": mode,
    }
    _log_decision("confidence_multiplier", mode,
                   {"ticker": signal.get("ticker"),
                    "confidence": signal.get("confidence"),
                    "timeframe": signal.get("timeframe")},
                   result, latency_ms, honored=honored)
    return result


# ---------- Public call: news-driven exit ----------------------------------

def news_exit_decision(trade: Dict[str, Any], news_item: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Claude what to do with an open position when news lands on the
    underlying. Returns `{action: "hold"|"trim"|"close", is_thesis_relevant,
    reason, honored, mode}`. In `shadow` mode action is forced to "hold".
    """
    mode = news_exit_mode()
    if mode == "off":
        return {"action": "hold", "is_thesis_relevant": False, "reason": "off",
                "honored": False, "mode": "off"}

    from services.config import AI_JUDGE_TIMEOUT_SEC
    started = time.time()
    sys_prompt = (
        "You are reviewing news that just arrived on a stock you currently "
        "hold a position in. Decide whether the news is THESIS-RELEVANT "
        "(materially affects why the trade was opened) and recommend an "
        "action: hold (most cases), trim (halve — uncertainty raised but "
        "thesis intact), or close (thesis broken — exit fully). "
        "Earnings beats with cut guidance, surprise downgrades, regulatory "
        "actions, M&A announcements going against the position, and CEO "
        "departures usually warrant trim or close. Routine news, sector "
        "moves, and tangential headlines are hold."
    )
    user_prompt = json.dumps({"trade": trade, "news": news_item}, default=str, indent=2)
    out = _call_with_tool(sys_prompt, user_prompt, _NEWS_EXIT_TOOL, AI_JUDGE_TIMEOUT_SEC)
    latency_ms = int((time.time() - started) * 1000)

    if out is None:
        result = {"action": "hold", "is_thesis_relevant": False,
                  "reason": "ai_abstain", "honored": False, "mode": mode}
        _log_decision("news_exit", mode, {"ticker": trade.get("ticker")},
                       result, latency_ms, honored=False, error="abstain")
        return result

    action = str(out.get("action", "hold")).strip().lower()
    if action not in ("hold", "trim", "close"):
        action = "hold"
    is_relevant = bool(out.get("is_thesis_relevant", False))
    reason = str(out.get("reason", ""))[:280]
    effective = action if mode == "active" else "hold"
    honored = (mode == "active" and action != "hold")
    result = {
        "action": effective,
        "shadow_action": action,
        "is_thesis_relevant": is_relevant,
        "reason": reason,
        "honored": honored,
        "mode": mode,
    }
    _log_decision("news_exit", mode,
                   {"ticker": trade.get("ticker"),
                    "trade_id": trade.get("id"),
                    "news_title": news_item.get("title")},
                   result, latency_ms, honored=honored)
    if action != "hold":
        logger.info(
            f"ai_judge news_exit {trade.get('ticker')}: {action} "
            f"({reason}) [mode={mode}, honored={honored}, latency={latency_ms}ms]"
        )
    return result
