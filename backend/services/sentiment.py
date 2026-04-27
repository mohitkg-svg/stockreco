"""Pluggable sentiment backend.

Defaults to VADER (lexicon-based, instant, ~65% accuracy on finance text).
Opt-in to FinBERT (ProsusAI/finbert, ~75-80% accuracy on finance text)
by setting `SENTIMENT_BACKEND=finbert` and installing `transformers` +
`torch` — adds ~1GB to the Docker image and ~30s to cold-start.

Interface: `score_text(text) → {score, label, severity}` where:
  * score ∈ [-1, +1] (negative = bearish, positive = bullish)
  * label ∈ {positive, negative, neutral}
  * severity ∈ [0, 100] (strength of the sign-independent signal)

Designed so callers (services/news.py) only touch `score_text` — the
backend choice is invisible at the call site.
"""
from __future__ import annotations
import logging
import os
import threading
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Same lexicon boosts VADER already uses for finance terms.
_FINANCIAL_LEXICON_BOOSTS = {
    "surge": 2.5, "soar": 2.8, "rally": 2.3, "rebound": 1.8, "beat": 2.0,
    "beats": 2.0, "outperform": 2.2, "upgrade": 2.0, "breakout": 2.0,
    "crash": -2.8, "plunge": -2.8, "slump": -2.4, "tumble": -2.4,
    "miss": -2.2, "misses": -2.2, "downgrade": -2.0, "bearish": -2.0,
    "lawsuit": -1.8, "fraud": -3.0, "bankruptcy": -3.0, "default": -2.4,
    "recall": -1.6, "investigation": -1.4, "probe": -1.2,
}

_POSITIVE_THRESHOLD = 0.15
_NEGATIVE_THRESHOLD = -0.15


# --------------------- VADER backend ---------------------------------------
_vader = None
_vader_lock = threading.Lock()


def _get_vader():
    global _vader
    if _vader is not None:
        return _vader
    with _vader_lock:
        if _vader is not None:
            return _vader
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        v = SentimentIntensityAnalyzer()
        v.lexicon.update(_FINANCIAL_LEXICON_BOOSTS)
        _vader = v
    return _vader


def _score_vader(text: str) -> Dict[str, Any]:
    v = _get_vader()
    s = v.polarity_scores(text)
    compound = float(s.get("compound", 0.0))
    if compound >= _POSITIVE_THRESHOLD:
        label = "positive"
    elif compound <= _NEGATIVE_THRESHOLD:
        label = "negative"
    else:
        label = "neutral"
    return {
        "score": round(compound, 4),
        "label": label,
        "severity": int(round(abs(compound) * 100)),
        "backend": "vader",
    }


# --------------------- FinBERT backend -------------------------------------
# Only initialized if SENTIMENT_BACKEND=finbert. Imports are inside the
# init function so a missing `transformers`/`torch` install gracefully
# falls back to VADER without crashing the app at import time.
_finbert = None
_finbert_lock = threading.Lock()
_finbert_available = None  # tri-state: None (not tested) / True / False


def _try_init_finbert():
    global _finbert, _finbert_available
    if _finbert_available is False:
        return None
    if _finbert is not None:
        return _finbert
    with _finbert_lock:
        if _finbert is not None:
            return _finbert
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch
            model_name = "ProsusAI/finbert"
            tok = AutoTokenizer.from_pretrained(model_name)
            model = AutoModelForSequenceClassification.from_pretrained(model_name)
            model.eval()
            _finbert = {"tok": tok, "model": model, "torch": torch,
                        "labels": ["positive", "negative", "neutral"]}
            _finbert_available = True
            logger.info("sentiment: FinBERT backend initialized")
        except Exception as e:
            logger.warning(f"sentiment: FinBERT init failed ({e}); falling back to VADER. "
                           "Install with `pip install transformers torch` to enable.")
            _finbert_available = False
            _finbert = None
    return _finbert


def _score_finbert(text: str) -> Dict[str, Any]:
    fb = _try_init_finbert()
    if fb is None:
        return _score_vader(text)
    try:
        tok = fb["tok"]
        model = fb["model"]
        torch = fb["torch"]
        # Truncate to FinBERT's 512-token limit.
        inputs = tok(text[:2000], return_tensors="pt", truncation=True, max_length=512)
        with torch.no_grad():
            logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)[0].tolist()
        labels = fb["labels"]
        label_idx = max(range(len(probs)), key=lambda i: probs[i])
        label = labels[label_idx]
        # Map to our compound-style score: positive prob − negative prob.
        p_pos = probs[labels.index("positive")]
        p_neg = probs[labels.index("negative")]
        score = p_pos - p_neg
        return {
            "score": round(score, 4),
            "label": label,
            "severity": int(round(abs(score) * 100)),
            "backend": "finbert",
        }
    except Exception as e:
        logger.debug(f"sentiment: FinBERT scoring failed ({e}); fallback VADER")
        return _score_vader(text)


# --------------------- Public API ------------------------------------------
def score_text(text: str) -> Dict[str, Any]:
    """Route to the configured backend. r43 fix #1.32: default is now
    `auto` — try FinBERT first, fall back to VADER if `transformers` /
    model files are not installed. Operators with the FinBERT deps
    automatically get the higher-accuracy backend without flipping
    `SENTIMENT_BACKEND` manually. Set explicitly to `vader` to force the
    legacy backend."""
    if not text or not text.strip():
        return {"score": 0.0, "label": "neutral", "severity": 0, "backend": "none"}
    backend = (os.getenv("SENTIMENT_BACKEND") or "auto").lower().strip()
    if backend == "vader":
        return _score_vader(text)
    if backend == "finbert":
        return _score_finbert(text)
    # auto: prefer FinBERT, fall back transparently.
    try:
        result = _score_finbert(text)
        if result.get("backend") and "finbert" in str(result["backend"]).lower():
            return result
    except Exception:
        pass
    return _score_vader(text)
