"""BTC / Market Regime factor.

One score from -100 (strong risk-off) to +100 (strong risk-on) that answers:
"what is the overall backdrop for BTC right now?" It combines six parts and
renormalises over whichever parts are available this run:

  btc_momentum  30%  BTC trend on 1h / 4h / 1d (EMA structure, RSI, 7d return)
  news          20%  recent crypto + macro/world headlines (keyword sentiment, recency-weighted)
  macro         10%  S&P 500, US dollar (DXY), VIX (Yahoo Finance)
  us10y         10%  US 10-year Treasury yield: level + 1-day and 5-day move (rising = headwind)
  sentiment     10%  Crypto Fear & Greed index
  global        10%  total crypto market-cap 24h change (CoinGecko)
  derivatives   10%  BTC funding rate (crowding) + spot taker buy/sell flow

Every part fails soft: a blocked or slow API just drops that part and the
rest still produce a score. If less than MIN_COVERAGE of the weight is
available the label is UNKNOWN and nothing is gated.

The score is informational by default (it annotates every alert and the
journal). Only the Movers strategies are gated by it (see screener.py).
This is a screening aid, not a prediction.
"""
import calendar
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    import feedparser
except ImportError:  # feedparser is in requirements.txt; degrade gracefully anyway
    feedparser = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REGIME_FILE = os.path.join(ROOT, "market_regime.json")

BINANCE_BASE = "https://data-api.binance.vision"
FUTURES_BASE = "https://fapi.binance.com"
REQUEST_TIMEOUT = 15
USER_AGENT = {"User-Agent": "Mozilla/5.0 (DeepPocketTracker market-regime)"}

REGIME_ENABLED = True
CACHE_MINUTES = 25                 # reuse a fresh result (manual re-runs, retries)
REPORT_EVERY_HOURS = 6             # standalone Discord regime report cadence (also on label change)
MIN_COVERAGE = 0.40
ALIGN_THRESHOLD = 15               # |score| above this counts as a directional backdrop
GATE_THRESHOLD = 40                # Movers: block counter-regime signals beyond this

WEIGHTS = {"btc_momentum": 0.30, "news": 0.20, "macro": 0.10, "us10y": 0.10,
           "sentiment": 0.10, "global": 0.10, "derivatives": 0.10}
YIELD_GATE_THRESHOLD = 60          # Movers: US10Y component beyond +/-60 blocks the opposing side
YIELD_ALIGN_THRESHOLD = 20         # |US10Y score| above this counts as a tailwind / headwind

NEWS_QUERIES = [
    "bitcoin", "crypto market", "bitcoin ETF flows", "SEC crypto regulation",
    "federal reserve interest rates", "stock market inflation tariffs geopolitics",
]
CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]
CRYPTO_FEED_RELEVANCE = ("bitcoin", "btc", "crypto market", "fed ", "federal reserve", "etf", "sec ",
                         "inflation", "rate cut", "rate hike", "tariff", "stablecoin", "treasury")

# Matching rules: a plain term matches as a WHOLE word (optional s/es/ed/ing ending), so "ban"
# no longer fires on "bank"/"banner" and "war" not on "warns". A trailing * marks a stem
# that may continue ("liquidat*" -> liquidation, liquidated).
BULLISH_TERMS = {
    "inflow": 1.5, "rate cut": 2.0, "approve": 1.5, "approval": 1.5, "adopt*": 1.0,
    "all-time high": 2.0, "record high": 1.5, "surge": 1.0, "rally": 1.0, "accumulat*": 1.0,
    "buys bitcoin": 1.5, "strategic reserve": 2.0, "easing": 1.0, "ceasefire": 1.0, "soar": 1.2,
    "breakout": 1.0, "bullish": 1.0, "cooling inflation": 1.5, "inflation cools": 1.5,
    "institutional": 0.6, "recovery": 0.8, "hike bets ease": 1.0,
}
BEARISH_TERMS = {
    "outflow": 1.5, "rate hike": 2.0, "hawkish": 1.5, "hack": 2.0, "exploit": 1.5, "ban": 1.5,
    "crackdown": 1.5, "lawsuit": 1.0, "sec sues": 2.0, "sec charges": 2.0, "liquidat*": 1.5,
    "crash": 2.0, "plunge": 1.5, "sell-off": 1.5, "selloff": 1.5, "bearish": 1.0, "tariff": 1.2,
    "war": 1.2, "hotter inflation": 1.5, "inflation rises": 1.2, "recession": 1.5,
    "bankruptcy": 2.0, "insolven*": 2.0, "collapse": 1.5, "fraud": 1.5, "sanction": 1.0,
    "slump": 1.0, "tumble": 1.0,
}
NEWS_SCALE = 0.35        # average weighted headline score that maps to ~76/100 (tanh(1) = 0.76)
ALTCOIN_TICKER = re.compile(r"\((?!BTC\b)[A-Z0-9]{2,8}\)")
BTC_WORDS = re.compile(r"bitcoin|\bbtc\b", re.I)


def _compile_terms(terms):
    compiled = []
    for term, weight in terms.items():
        if term.endswith("*"):
            pattern = r"\b" + re.escape(term[:-1])
        else:
            pattern = r"\b" + re.escape(term) + r"(?:s|es|ed|d|ned|ning|ing)?\b"
        compiled.append((re.compile(pattern), weight))
    return compiled


_BULL_RE = _compile_terms(BULLISH_TERMS)
_BEAR_RE = _compile_terms(BEARISH_TERMS)


# ----------------------------- small helpers --------------------------

def _clamp(x, lo=-100.0, hi=100.0):
    return max(lo, min(hi, x))


def _ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0)) / period
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)


def _get_json(url, params=None, headers=None):
    resp = requests.get(url, params=params, headers=headers or USER_AGENT, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _klines(interval, limit, symbol="BTCUSDT"):
    raw = _get_json(f"{BINANCE_BASE}/api/v3/klines",
                    {"symbol": symbol, "interval": interval, "limit": limit})
    return {"close": [float(c[4]) for c in raw], "quote_volume": [float(c[7]) for c in raw],
            "taker_buy_quote": [float(c[10]) for c in raw]}


# ----------------------------- pure scoring functions -----------------

def score_btc_trend(k1h, k4h, k1d):
    """-100..+100 from EMA structure + RSI on three timeframes plus the 7-day return."""
    parts, weights, notes = [], [], []
    for name, k, w, slow in (("1h", k1h, 0.25, 50), ("4h", k4h, 0.35, 50), ("1d", k1d, 0.40, 200)):
        closes = (k or {}).get("close") or []
        if len(closes) < slow + 5:
            slow = 50
        if len(closes) < 55:
            continue
        ema20, ema_slow = _ema(closes, 20)[-1], _ema(closes, slow)[-1]
        rsi = _rsi(closes)
        if ema20 is None or ema_slow is None or rsi is None:
            continue
        s = (25 if closes[-1] > ema_slow else -25) + (25 if ema20 > ema_slow else -25)
        s += _clamp(rsi - 50, -25, 25)
        s = s * 4 / 3
        parts.append(_clamp(s)); weights.append(w)
        notes.append(f"{name} {'↑' if s > 10 else '↓' if s < -10 else '→'} RSI {rsi:.0f}")
    if not parts:
        return None, "BTC data unavailable"
    base = sum(p * w for p, w in zip(parts, weights)) / sum(weights)
    closes_d = (k1d or {}).get("close") or []
    ret7 = 0.0
    if len(closes_d) >= 9:
        ret7 = (closes_d[-2] / closes_d[-9] - 1) * 100
        base = base * 0.8 + _clamp(ret7 * 2, -20, 20)
        notes.append(f"7d {ret7:+.1f}%")
    return _clamp(base), " · ".join(notes)


def score_funding(rate_pct):
    """Funding as a crowding gauge: very positive = crowded longs (bearish), negative = squeeze fuel."""
    if rate_pct is None:
        return None
    if rate_pct >= 0.05:
        return -40.0
    if rate_pct >= 0.02:
        return -15.0 - (rate_pct - 0.02) / 0.03 * 25
    if rate_pct <= -0.02:
        return 35.0
    if rate_pct <= -0.005:
        return 15.0
    return 0.0


def score_taker_flow(k1h):
    """Spot taker buy share over the last 24 hourly candles: >50% buyers aggressive = bullish."""
    qv = (k1h or {}).get("quote_volume", [])[-25:-1]
    tb = (k1h or {}).get("taker_buy_quote", [])[-25:-1]
    if len(qv) < 12 or sum(qv) <= 0:
        return None, None
    share = sum(tb) / sum(qv)
    # Spot taker-buy share normally sits a little under 50% (about 47-50%), so centre on 49%
    # and keep it gentle: 46% is mildly bearish, not a -37 shock.
    return _clamp((share - 0.49) * 600, -40, 40), share


def score_fear_greed(value, previous=None):
    if value is None:
        return None
    s = _clamp((value - 50) * 1.2, -60, 60)
    if previous is not None:
        s += _clamp((value - previous) * 2, -15, 15)
    return _clamp(s)


def score_global(mcap_change_24h_pct):
    return None if mcap_change_24h_pct is None else _clamp(mcap_change_24h_pct * 15)


def score_macro(changes):
    """changes: {'spx': pct, 'dxy': pct, 'vix': (level, pct)}. ('tnx' is still accepted but the
    10-year yield now has its own factor, see score_us10y.)"""
    parts, notes = [], []
    if changes.get("spx") is not None:
        parts.append(_clamp(changes["spx"] * 40)); notes.append(f"S&P {changes['spx']:+.1f}%")
    if changes.get("dxy") is not None:
        parts.append(_clamp(-changes["dxy"] * 60)); notes.append(f"DXY {changes['dxy']:+.1f}%")
    if changes.get("vix") is not None:
        level, pct = changes["vix"]
        s = _clamp(-pct * 1.5) + (-25 if level >= 25 else 10 if level <= 15 else 0)
        parts.append(_clamp(s)); notes.append(f"VIX {level:.0f}")
    if changes.get("tnx") is not None:
        parts.append(_clamp(-changes["tnx"] * 8)); notes.append(f"10y {changes['tnx']:+.1f}%")
    if not parts:
        return None, "macro data unavailable"
    return _clamp(sum(parts) / len(parts)), " · ".join(notes)


def score_us10y(closes):
    """US 10-year yield factor from daily closes in percent (oldest -> newest).

    Rising yields tighten liquidity and hurt risk assets (crypto included), falling yields
    help. Score = -(1-day move x4 + 5-day move x1.5) in basis points, plus a level
    adjustment (>= 4.75% is a headwind by itself, <= 3.75% a tailwind).
    Returns (score, note, info) -- info feeds the per-signal alignment."""
    ys = [float(c) for c in closes if c is not None]
    ys = [y / 10 if y > 15 else y for y in ys]            # some feeds quote the yield x10
    if len(ys) < 2:
        return None, "US10Y unavailable", None
    level = ys[-1]
    day_bps = (ys[-1] - ys[-2]) * 100
    week_bps = (ys[-1] - ys[-6]) * 100 if len(ys) >= 6 else day_bps
    move = _clamp(-(day_bps * 4 + week_bps * 1.5), -80, 80)
    adj = (-20 if level >= 5.0 else -10 if level >= 4.75 else 20 if level <= 3.25
           else 10 if level <= 3.75 else 0)
    score = _clamp(move + adj)
    trend = "rising (headwind)" if score <= -YIELD_ALIGN_THRESHOLD else \
            "falling (tailwind)" if score >= YIELD_ALIGN_THRESHOLD else "flat"
    note = f"{level:.2f}% · {day_bps:+.0f} bps 1d · {week_bps:+.0f} bps 5d · {trend}"
    return score, note, {"level": round(level, 3), "day_bps": round(day_bps, 1),
                         "week_bps": round(week_bps, 1), "score": round(score, 1)}


def _term_score(text):
    t = text.lower()
    bull = sum(w for rx, w in _BULL_RE if rx.search(t))
    bear = sum(w for rx, w in _BEAR_RE if rx.search(t))
    return max(-3.0, min(3.0, bull - bear))


def _clean_title(title):
    """Google News appends ' - Publisher'; strip it so the same story is not counted per outlet."""
    return re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", title).strip()


def _is_btc_relevant(title):
    """Skip stories about one specific altcoin, e.g. 'Hyperliquid (HYPE) surges ...'."""
    return not (ALTCOIN_TICKER.search(title) and not BTC_WORDS.search(title))


def score_headlines(items, now=None, min_items=3):
    """items: [{'title': str, 'age_hours': float}] -> (score, note, top_headlines).

    The score is the AVERAGE recency-weighted headline sentiment (not the sum), so pulling in
    more feeds cannot push it to +/-100 by volume alone."""
    now = now or time.time()
    seen, scored = set(), []
    for it in items:
        title = _clean_title(it["title"])
        key = re.sub(r"\W+", " ", title.lower()).strip()[:70]
        if not key or key in seen or not _is_btc_relevant(title):
            continue
        seen.add(key)
        raw = _term_score(title)
        recency = 0.5 ** (max(0.0, it.get("age_hours", 6.0)) / 12)
        scored.append((raw * recency, raw, title))
    if len(scored) < min_items:
        return None, "not enough headlines", []
    avg = sum(s for s, _, _ in scored) / len(scored)
    score = _clamp(100 * math.tanh(avg / NEWS_SCALE))
    movers = sorted((x for x in scored if x[1] != 0), key=lambda x: abs(x[0]), reverse=True)[:3]
    bull_n = sum(1 for _, r, _ in scored if r > 0)
    bear_n = sum(1 for _, r, _ in scored if r < 0)
    return score, f"{len(scored)} headlines · {bull_n} bullish / {bear_n} bearish", [
        {"title": t[:120], "impact": "Bullish" if r > 0 else "Bearish"} for _, r, t in movers]


def combine(components, weights=WEIGHTS):
    """components: {name: (score|None, note)} -> (score, coverage)."""
    total = used = 0.0
    for name, w in weights.items():
        score = (components.get(name) or (None, ""))[0]
        if score is not None:
            total += score * w
            used += w
    if used <= 0:
        return None, 0.0
    return total / used, used / sum(weights.values())


def label_for(score, coverage):
    if score is None or coverage < MIN_COVERAGE:
        return "UNKNOWN"
    if score >= 40:
        return "STRONG BULLISH"
    if score >= ALIGN_THRESHOLD:
        return "BULLISH"
    if score <= -40:
        return "STRONG BEARISH"
    if score <= -ALIGN_THRESHOLD:
        return "BEARISH"
    return "NEUTRAL"


# ----------------------------- data fetchers (each fails soft) --------

def _fetch_btc_momentum():
    k1h, k4h, k1d = _klines("1h", 220), _klines("4h", 220), _klines("1d", 260)
    return score_btc_trend(k1h, k4h, k1d)


def _fetch_derivatives():
    k1h = _klines("1h", 60)
    taker, share = score_taker_flow(k1h)
    funding = None
    try:
        data = _get_json(f"{FUTURES_BASE}/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
        funding = round(float(data["lastFundingRate"]) * 100, 4)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        try:   # Binance futures is geo-blocked from many cloud IPs; OKX public funding is not
            data = _get_json("https://www.okx.com/api/v5/public/funding-rate", {"instId": "BTC-USDT-SWAP"})
            funding = round(float(data["data"][0]["fundingRate"]) * 100, 4)
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
            pass
    parts = [s for s in (score_funding(funding), taker) if s is not None]
    if not parts:
        return None, "derivatives data unavailable"
    notes = []
    if funding is not None:
        notes.append(f"funding {funding:+.4f}%")
    if share is not None:
        notes.append(f"taker buy {share * 100:.1f}%")
    return _clamp(sum(parts) / len(parts)), " · ".join(notes)


def _fetch_sentiment():
    data = _get_json("https://api.alternative.me/fng/", {"limit": 2})["data"]
    value = int(data[0]["value"])
    prev = int(data[1]["value"]) if len(data) > 1 else None
    return score_fear_greed(value, prev), f"Fear & Greed {value} ({data[0].get('value_classification', '')})"


def _fetch_global():
    headers = dict(USER_AGENT)
    key = os.environ.get("COINGECKO_DEMO_API_KEY")
    if key:
        headers["x-cg-demo-api-key"] = key
    data = _get_json("https://api.coingecko.com/api/v3/global", headers=headers)["data"]
    change = float(data["market_cap_change_percentage_24h_usd"])
    dom = data.get("market_cap_percentage", {}).get("btc")
    return score_global(change), f"mcap {change:+.1f}% 24h" + (f" · BTC dom {dom:.1f}%" if dom else "")


def _yahoo_change(symbol):
    """(latest level, % change vs the previous daily close). The newest daily bar already holds
    the live price while a market is open, and the last session's close when it is shut."""
    data = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     {"range": "5d", "interval": "1d"})
    res = data["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    if len(closes) < 2:
        raise ValueError("not enough daily closes")
    return closes[-1], (closes[-1] / closes[-2] - 1) * 100


def _fetch_macro():
    changes = {}
    for key, sym in (("spx", "^GSPC"), ("dxy", "DX-Y.NYB"), ("vix", "^VIX")):
        try:
            level, pct = _yahoo_change(sym)
            changes[key] = (level, pct) if key == "vix" else pct
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
            continue
    pcts = [v[1] if isinstance(v, tuple) else v for v in changes.values()]
    if len(pcts) >= 2 and all(abs(p) < 0.005 for p in pcts):
        return None, "macro data looks stale (no change on any market)"   # never count dead data as 'neutral'
    return score_macro(changes)


def _yahoo_closes(symbol, rng="1mo"):
    data = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     {"range": rng, "interval": "1d"})
    res = data["chart"]["result"][0]
    return [c for c in res["indicators"]["quote"][0]["close"] if c is not None]


def _fred_dgs10_closes():
    """Fallback: FRED daily 10-year constant-maturity yield (public CSV, one-day lag)."""
    resp = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": "DGS10"},
                        headers=USER_AGENT, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    values = []
    for line in resp.text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) == 2 and parts[1] not in ("", "."):
            values.append(float(parts[1]))
    return values[-12:]


def _fetch_us10y():
    closes = None
    try:
        closes = _yahoo_closes("^TNX")
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        pass
    if not closes or len(closes) < 2:
        closes = _fred_dgs10_closes()
    score, note, info = score_us10y(closes)
    _fetch_us10y.info = info
    return score, note


_fetch_us10y.info = None


def _entry_age_hours(entry, now):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return 6.0
    return max(0.0, (now - calendar.timegm(parsed)) / 3600)


def _fetch_headlines(now=None):
    now = now or time.time()
    if feedparser is None:
        return []
    items = []
    urls = [("https://news.google.com/rss/search?q=" + q.replace(" ", "+") + "+when:1d"
             "&hl=en-US&gl=US&ceid=US:en", False) for q in NEWS_QUERIES]
    urls += [(u, True) for u in CRYPTO_FEEDS]
    for url, needs_filter in urls:
        try:
            resp = requests.get(url, headers=USER_AGENT, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            for entry in feedparser.parse(resp.content).entries[:25]:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                if needs_filter and not any(k in title.lower() + " " for k in CRYPTO_FEED_RELEVANCE):
                    continue
                age = _entry_age_hours(entry, now)
                if age <= 24:
                    items.append({"title": title, "age_hours": age})
        except requests.RequestException:
            continue
    return items


def _fetch_news():
    score, note, top = score_headlines(_fetch_headlines())
    _fetch_news.top = top
    return score, note


_fetch_news.top = []


# ----------------------------- public API -----------------------------

FETCHERS = {"btc_momentum": _fetch_btc_momentum, "news": _fetch_news, "macro": _fetch_macro,
            "us10y": _fetch_us10y, "sentiment": _fetch_sentiment, "global": _fetch_global, "derivatives": _fetch_derivatives}


def compute_regime(fetchers=None, now=None):
    """Compute a fresh regime dict. `fetchers` is injectable for tests."""
    fetchers = fetchers or FETCHERS
    now = now or time.time()

    def run(name):
        try:
            result = fetchers[name]()
            return name, result if isinstance(result, tuple) else (result, "")
        except Exception as exc:  # noqa: BLE001 - any single source may fail
            return name, (None, f"unavailable ({type(exc).__name__})")

    with ThreadPoolExecutor(max_workers=7) as pool:
        components = dict(pool.map(run, list(fetchers)))
    score, coverage = combine(components)
    return {
        "computed_at": now,
        "score": None if score is None else round(score, 1),
        "label": label_for(score, coverage),
        "coverage": round(coverage, 2),
        "components": {n: {"score": None if s is None else round(s, 1), "note": note,
                           "weight": WEIGHTS.get(n, 0)} for n, (s, note) in components.items()},
        "headlines": list(getattr(fetchers.get("news"), "top", []) or []),
        "us10y": getattr(fetchers.get("us10y"), "info", None),
    }


def _read_file():
    try:
        with open(REGIME_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_regime(regime, extra=None):
    data = _read_file()
    data.update(regime)
    if extra:
        data.update(extra)
    tmp = REGIME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, REGIME_FILE)
    return data


def get_regime(now=None, force=False):
    """Fresh-enough cached regime, else recompute (and persist). Returns None if disabled/failed."""
    if not REGIME_ENABLED:
        return None
    now = now or time.time()
    cached = _read_file()
    if (not force and cached.get("score") is not None
            and now - float(cached.get("computed_at", 0)) < CACHE_MINUTES * 60):
        return cached
    try:
        regime = compute_regime(now=now)
    except Exception as exc:  # noqa: BLE001
        print(f"[regime] compute failed: {exc}")
        return cached if cached.get("score") is not None else None
    return save_regime(regime)


def direction_is_long(text):
    t = str(text).upper()
    if "SELL" in t or "SHORT" in t:
        return False
    return True if ("BUY" in t or "LONG" in t) else None


def alignment(regime, is_long):
    if not regime or regime.get("score") is None or regime.get("label") == "UNKNOWN" or is_long is None:
        return "UNKNOWN"
    s = regime["score"] if is_long else -regime["score"]
    if s >= ALIGN_THRESHOLD:
        return "ALIGNED"
    if s <= -ALIGN_THRESHOLD:
        return "COUNTER-TREND"
    return "NEUTRAL"


def blocks_signal(regime, is_long, threshold=GATE_THRESHOLD):
    """True when the backdrop strongly opposes this direction (used for Movers only)."""
    if not regime or regime.get("score") is None or regime.get("label") == "UNKNOWN":
        return False
    return (regime["score"] <= -threshold) if is_long else (regime["score"] >= threshold)


def yield_alignment(regime, is_long):
    """TAILWIND / HEADWIND for this trade direction from the US10Y factor (UNKNOWN if no data).
    Rising yields are a headwind for longs and a tailwind for shorts; falling yields the reverse."""
    info = (regime or {}).get("us10y")
    if not info or is_long is None:
        return "UNKNOWN"
    s = info["score"] if is_long else -info["score"]
    if s >= YIELD_ALIGN_THRESHOLD:
        return "TAILWIND"
    if s <= -YIELD_ALIGN_THRESHOLD:
        return "HEADWIND"
    return "NEUTRAL"


def yield_blocks_signal(regime, is_long, threshold=YIELD_GATE_THRESHOLD):
    """True when yields are moving sharply against this direction (used for Movers only)."""
    info = (regime or {}).get("us10y")
    if not info:
        return False
    return (info["score"] <= -threshold) if is_long else (info["score"] >= threshold)


def yield_text(regime, is_long=None):
    info = (regime or {}).get("us10y")
    if not info:
        return "unavailable"
    text = f"{info['level']:.2f}% ({info['day_bps']:+.0f} bps 1d, {info['week_bps']:+.0f} bps 5d)"
    al = yield_alignment(regime, is_long) if is_long is not None else "UNKNOWN"
    if al == "HEADWIND":
        text += " · ⚠️ yields moving against this trade"
    elif al == "TAILWIND":
        text += " · ✅ yields support this trade"
    return text


def short_text(regime, is_long=None):
    if not regime or regime.get("score") is None:
        return "unavailable"
    text = f"{regime['label']} ({regime['score']:+.0f})"
    al = alignment(regime, is_long) if is_long is not None else "UNKNOWN"
    if al == "COUNTER-TREND":
        text += " · ⚠️ counter-trend to BTC backdrop"
    elif al == "ALIGNED":
        text += " · ✅ aligned with BTC backdrop"
    return text


def annotate_details(signal, regime):
    """Record the regime on a signal so it lands in the journal / Sheet 'Reason' column."""
    if not regime or regime.get("score") is None:
        return signal
    details = dict(signal.get("details") or {}) if isinstance(signal.get("details"), dict) else {}
    is_long = direction_is_long(signal.get("action") or signal.get("direction"))
    details.update({"regime_score": regime["score"], "regime": regime["label"],
                    "regime_alignment": alignment(regime, is_long)})
    info = regime.get("us10y")
    if info:
        details.update({"us10y": info["level"], "us10y_day_bps": info["day_bps"],
                        "us10y_week_bps": info["week_bps"],
                        "us10y_alignment": yield_alignment(regime, is_long)})
    signal["details"] = details
    return signal


def build_report_embed(regime):
    colors = {"STRONG BULLISH": 3066993, "BULLISH": 5763719, "NEUTRAL": 9807270,
              "BEARISH": 15105570, "STRONG BEARISH": 15158332, "UNKNOWN": 9807270}
    names = {"btc_momentum": "BTC Momentum", "news": "News / World", "macro": "Macro (S&P · DXY · VIX)", "us10y": "US 10Y Yield",
             "sentiment": "Sentiment", "global": "Global Crypto Market", "derivatives": "Derivatives / Flow"}
    fields = []
    for key, comp in regime.get("components", {}).items():
        value = "n/a" if comp["score"] is None else f"{comp['score']:+.0f}  ·  {comp['note']}"
        fields.append({"name": f"{names.get(key, key)} ({int(comp['weight'] * 100)}%)",
                       "value": value[:300], "inline": False})
    if regime.get("headlines"):
        fields.append({"name": "Headlines moving the score", "inline": False, "value": "\n".join(
            f"{'🟢' if h['impact'] == 'Bullish' else '🔴'} {h['title']}" for h in regime["headlines"])[:900]})
    score = regime.get("score")
    return {"embeds": [{
        "title": f"🌍 BTC / Market Regime: {regime.get('label', 'UNKNOWN')}"
                 + ("" if score is None else f" ({score:+.0f}/100)"),
        "color": colors.get(regime.get("label"), 9807270),
        "description": ("Overall backdrop for BTC from price momentum, news, macro, sentiment, "
                        "global market and derivatives. Informational: alerts show whether they align."),
        "fields": fields,
        "footer": {"text": f"Coverage {int(regime.get('coverage', 0) * 100)}% of factors · "
                           "keyword-based news scoring · not financial advice"},
    }]}


def report_due(regime, now=None):
    """True when the label changed or REPORT_EVERY_HOURS have passed since the last report."""
    now = now or time.time()
    data = _read_file()
    if not regime or regime.get("score") is None:
        return False
    if data.get("last_report_label") != regime.get("label"):
        return True
    return now - float(data.get("last_report_ts", 0)) >= REPORT_EVERY_HOURS * 3600


def mark_reported(regime, now=None):
    save_regime({}, {"last_report_ts": now or time.time(), "last_report_label": regime.get("label")})
