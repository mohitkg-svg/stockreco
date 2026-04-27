"""SEC Form 4 insider-trades aggregator.

Form 4 = director/officer purchases and sales, filed within 2 business days
of the trade. We pull the latest filings per ticker from SEC EDGAR, extract
transaction code + shares + price, and aggregate into 30d / 90d buy/sell
counts + a net buy ratio. Signal generator uses the ratio to tilt BUY
confidence on tickers where insiders are accumulating.

Source: https://www.sec.gov/cgi-bin/browse-edgar — free, no API key.
EDGAR has a 10 req/s rate limit and requires a User-Agent header identifying
the requester (they enforce this).

Signal value:
  * Strong on small/mid caps where C-suite has real information asymmetry
  * Near-zero on mega-caps (insider dispositions are mostly scheduled
    10b5-1 plans, not sentiment)
  * Buying = more informative than selling (insiders have many reasons to
    sell: taxes, diversification, personal cash needs)

Weekly cadence is sufficient — Form 4 filings lag by 1-2 days anyway.
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from database import SessionLocal, InsiderSummary, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)

# SEC-required User-Agent: "Company/App email@domain"
_UA = "stockrecs-trading-bot gupta.pccs@gmail.com"
_EDGAR_TIMEOUT = 15.0
# Transaction codes on Form 4 — P=Open-market purchase, S=Open-market sale.
# Other codes (A=Award, M=Exercise, F=Payment-of-exercise-tax, etc.) are
# mechanical and NOT sentiment signal — ignore.
_BUY_CODES = {"P"}
_SELL_CODES = {"S"}


def _ticker_to_cik(ticker: str) -> Optional[str]:
    """Resolve ticker → 10-digit zero-padded CIK via the SEC's ticker map.
    Cached in-process since this file rarely changes."""
    import httpx
    if not hasattr(_ticker_to_cik, "_cache"):
        try:
            with httpx.Client(timeout=_EDGAR_TIMEOUT,
                              headers={"User-Agent": _UA}) as c:
                r = c.get("https://www.sec.gov/files/company_tickers.json")
            if r.status_code != 200:
                logger.warning(f"insider: EDGAR ticker map HTTP {r.status_code}")
                _ticker_to_cik._cache = {}  # type: ignore
                return None
            data = r.json() or {}
            cache = {}
            for v in data.values():
                if isinstance(v, dict):
                    t = (v.get("ticker") or "").upper()
                    cik = v.get("cik_str")
                    if t and cik is not None:
                        cache[t] = str(cik).zfill(10)
            _ticker_to_cik._cache = cache  # type: ignore
        except Exception as e:
            logger.debug(f"insider: ticker map fetch failed: {e}")
            _ticker_to_cik._cache = {}  # type: ignore
    return _ticker_to_cik._cache.get(ticker.upper())  # type: ignore


def _fetch_recent_form4s(cik: str, lookback_days: int = 90) -> List[Dict[str, Any]]:
    """Pull Form 4 filings for `cik` in the last `lookback_days`. Returns a
    list of (filing_date, transactions) — transactions is a list of
    {code, shares, price_per_share, value}. Parses the SEC XML directly."""
    import httpx
    import xml.etree.ElementTree as ET

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    index_url = (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        f"&type=4&dateb=&owner=include&count=40&action=getcompany&output=atom"
    )
    out: List[Dict[str, Any]] = []
    try:
        with httpx.Client(timeout=_EDGAR_TIMEOUT,
                          headers={"User-Agent": _UA, "Accept": "application/atom+xml"}) as c:
            r = c.get(index_url)
            if r.status_code != 200:
                logger.debug(f"insider: EDGAR index HTTP {r.status_code} for CIK {cik}")
                return out
            # Parse the Atom feed
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            root = ET.fromstring(r.text)
            for entry in root.findall("atom:entry", ns):
                updated = entry.findtext("atom:updated", default="", namespaces=ns)
                if updated[:10] < since:
                    continue
                # Find the accession link
                link_el = entry.find("atom:link", ns)
                if link_el is None:
                    continue
                href = link_el.get("href", "")
                # Construct the canonical Form 4 XML location
                # href looks like: https://www.sec.gov/Archives/edgar/data/CIK/ACCESSION-index.htm
                m = re.search(r"/Archives/edgar/data/\d+/([0-9-]+)-index\.htm", href)
                if not m:
                    continue
                acc_raw = m.group(1).replace("-", "")
                # Form 4 XML is usually named by primary form + .xml under Archives
                # The index.json endpoint gives us the actual filenames
                index_json_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_raw}/index.json"
                try:
                    r2 = c.get(index_json_url)
                    if r2.status_code != 200:
                        continue
                    items = r2.json().get("directory", {}).get("item", [])
                    xml_name = next((it["name"] for it in items
                                     if it.get("name", "").endswith(".xml") and "primary_doc" not in it.get("name", "").lower()
                                     and not it.get("name", "").startswith("R")), None)
                    # fall back to primary_doc.xml if named conventionally
                    if xml_name is None:
                        xml_name = next((it["name"] for it in items if it.get("name", "").endswith(".xml")), None)
                    if xml_name is None:
                        continue
                    xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_raw}/{xml_name}"
                    r3 = c.get(xml_url)
                    if r3.status_code != 200:
                        continue
                    filing = _parse_form4_xml(r3.text)
                    if filing:
                        filing["filed_at"] = updated[:10]
                        out.append(filing)
                except Exception as e:
                    logger.debug(f"insider: Form 4 parse failed ({cik}): {e}")
                    continue
    except Exception as e:
        logger.debug(f"insider: EDGAR fetch for {cik} failed: {e}")
    return out


def _parse_form4_xml(xml_text: str) -> Optional[Dict[str, Any]]:
    """Extract open-market purchases/sales from a Form 4 XML document."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    transactions: List[Dict[str, Any]] = []
    # Non-derivative transactions (common-stock trades)
    for nd in root.findall(".//nonDerivativeTransaction"):
        code_el = nd.find(".//transactionCode")
        shares_el = nd.find(".//transactionShares/value")
        price_el = nd.find(".//transactionPricePerShare/value")
        code = code_el.text.strip() if (code_el is not None and code_el.text) else ""
        if code not in _BUY_CODES and code not in _SELL_CODES:
            continue
        try:
            shares = float(shares_el.text) if (shares_el is not None and shares_el.text) else 0.0
            price = float(price_el.text) if (price_el is not None and price_el.text) else 0.0
        except Exception:
            continue
        if shares <= 0:
            continue
        transactions.append({
            "code": code,
            "shares": shares,
            "price": price,
            "value": shares * price,
        })
    return {"transactions": transactions} if transactions else None


def refresh_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    cik = _ticker_to_cik(ticker)
    if not cik:
        return None
    filings = _fetch_recent_form4s(cik, lookback_days=90)
    if not filings:
        # Write an empty row so we don't re-query every run, but only if nothing persisted
        _upsert({"ticker": ticker.upper(),
                 "buy_count_30d": 0, "buy_count_90d": 0,
                 "sell_count_30d": 0, "sell_count_90d": 0,
                 "net_buy_ratio_90d": None, "buy_dollar_90d": 0.0})
        return get_insider(ticker)
    today = datetime.now(timezone.utc).date()
    buys_30 = buys_90 = sells_30 = sells_90 = 0
    buy_value_90 = 0.0
    for f in filings:
        filed = f.get("filed_at", "")
        try:
            fd = datetime.strptime(filed, "%Y-%m-%d").date()
        except Exception:
            continue
        days_ago = (today - fd).days
        if days_ago > 90:
            continue
        for tx in f.get("transactions", []):
            code = tx.get("code")
            if code in _BUY_CODES:
                buys_90 += 1
                buy_value_90 += tx.get("value") or 0.0
                if days_ago <= 30:
                    buys_30 += 1
            elif code in _SELL_CODES:
                sells_90 += 1
                if days_ago <= 30:
                    sells_30 += 1
    total_90 = buys_90 + sells_90
    ratio = (buys_90 / total_90) if total_90 > 0 else None
    row = {
        "ticker": ticker.upper(),
        "buy_count_30d": buys_30, "buy_count_90d": buys_90,
        "sell_count_30d": sells_30, "sell_count_90d": sells_90,
        "net_buy_ratio_90d": round(ratio, 3) if ratio is not None else None,
        "buy_dollar_90d": round(buy_value_90, 2),
    }
    _upsert(row)
    return get_insider(ticker)


def _upsert(row: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        r = db.query(InsiderSummary).filter(InsiderSummary.ticker == row["ticker"]).first()
        if r is None:
            r = InsiderSummary(ticker=row["ticker"])
            db.add(r)
        for k, v in row.items():
            if k != "ticker":
                setattr(r, k, v)
        r.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def refresh_all() -> Dict[str, Any]:
    """Weekly rollup for watchlist + candidate pool. Serial (no threading) —
    SEC rate-limits to 10 req/s total per IP; doing this with concurrency
    tends to trip the throttle."""
    import time
    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
    finally:
        db.close()
    tickers = sorted(tickers)
    if not tickers:
        return {"checked": 0, "total": 0}
    ok = 0
    for t in tickers:
        try:
            if refresh_ticker(t) is not None:
                ok += 1
        except Exception as e:
            logger.debug(f"insider: refresh {t} failed: {e}")
        # Light pacing — stay under SEC's 10/sec cap comfortably.
        time.sleep(0.2)
    logger.info(f"insider_trades: refreshed {ok}/{len(tickers)} tickers")
    return {"checked": ok, "total": len(tickers)}


def get_insider(ticker: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        r = db.query(InsiderSummary).filter(InsiderSummary.ticker == ticker.upper()).first()
        if r is None:
            return None
        return {
            "ticker": r.ticker,
            "buy_count_30d": r.buy_count_30d, "buy_count_90d": r.buy_count_90d,
            "sell_count_30d": r.sell_count_30d, "sell_count_90d": r.sell_count_90d,
            "net_buy_ratio_90d": r.net_buy_ratio_90d,
            "buy_dollar_90d": r.buy_dollar_90d,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
    finally:
        db.close()


# ---------- Signal-generator integration ------------------------------------

_MULT_STRONG_INSIDER_BUY = 1.06
_MULT_MILD_INSIDER_BUY = 1.03
_MULT_NEUTRAL = 1.00
_MULT_INSIDER_SELLING = 0.97

# r43 fix #1.31: bumped min-count from 3 → 8 to filter out noise from
# director option-grant cycles where a single 3-buy-0-sell sample
# produces ratio=1.0 → +6% multiplier on noise. 8 transactions is the
# empirical floor where the 90d ratio reflects intent rather than HR cycles.
_MIN_COUNT_90D = 8


def insider_multiplier(ticker: str, direction: str) -> float:
    """BUY signals: strong insider buying (≥70% of 90d transactions are
    purchases, min 3 total) → 1.06. Moderate (≥60%) → 1.03. Heavy selling
    (≤30%) → 0.97. Else neutral.

    r44 Wave 7: cluster amplification. Cohen, Malloy, Pomorski (2012) show
    that ≥3 distinct insiders buying within a 30-day window TRIPLES the
    predictive power vs single-insider buys. We approximate by checking
    if buy_count_90d ≥ 12 (a multi-insider cluster threshold) AND ratio
    is strong, returning a 1.12× boost.
    """
    r = get_insider(ticker)
    if r is None:
        return _MULT_NEUTRAL
    total = (r.get("buy_count_90d") or 0) + (r.get("sell_count_90d") or 0)
    if total < _MIN_COUNT_90D:
        return _MULT_NEUTRAL
    ratio = r.get("net_buy_ratio_90d")
    if ratio is None:
        return _MULT_NEUTRAL
    direction = (direction or "").upper()
    buy_count = (r.get("buy_count_90d") or 0)
    sell_count = (r.get("sell_count_90d") or 0)
    is_cluster_buy = buy_count >= 12 and ratio >= 0.70
    is_cluster_sell = sell_count >= 12 and ratio <= 0.30
    if direction == "BUY":
        if is_cluster_buy: return 1.12   # cluster bonus
        if ratio >= 0.70: return _MULT_STRONG_INSIDER_BUY
        if ratio >= 0.60: return _MULT_MILD_INSIDER_BUY
        if ratio <= 0.30: return _MULT_INSIDER_SELLING
        return _MULT_NEUTRAL
    if direction == "SELL":
        if is_cluster_sell: return 1.12
        if ratio <= 0.30: return _MULT_STRONG_INSIDER_BUY   # i.e. strong-signal agreement
        if ratio <= 0.40: return _MULT_MILD_INSIDER_BUY
        if ratio >= 0.70: return _MULT_INSIDER_SELLING
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def insider_reason_line(ticker: str, direction: str) -> Optional[str]:
    r = get_insider(ticker)
    if r is None:
        return None
    total = (r.get("buy_count_90d") or 0) + (r.get("sell_count_90d") or 0)
    if total < _MIN_COUNT_90D or r.get("net_buy_ratio_90d") is None:
        return None
    mult = insider_multiplier(ticker, direction)
    if mult == _MULT_NEUTRAL:
        return None
    ratio = r["net_buy_ratio_90d"]
    buys = r["buy_count_90d"]
    sells = r["sell_count_90d"]
    buy_dollar = r.get("buy_dollar_90d") or 0
    mark = "👔✅" if mult > _MULT_NEUTRAL else "👔⚠️"
    dollar_bit = f", ${buy_dollar/1_000_000:.1f}M bought" if buy_dollar > 1_000_000 else ""
    return f"{mark} Insiders (90d): {buys} buys / {sells} sells ({ratio*100:.0f}% buy-ratio){dollar_bit} — {'confirms' if mult > _MULT_NEUTRAL else 'contradicts'} {direction}"
