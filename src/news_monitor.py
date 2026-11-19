"""
DeepPocket Tracker - News Monitor (v2.3 Institutional Upgrade)
--------------------------------------------------------------
Watches free crypto & macro news RSS feeds (no API key needed) for MAJOR
fundamental headlines -- exchange listings/delistings, hacks/exploits,
partnerships, regulatory action, ETF/institutional flows, macro/Fed/CPI
releases, and mainnet/tokenomics catalysts -- and sends an alert to Discord
when a headline mentions a tracked coin or a market-wide BTC/Macro catalyst.
"""

import os
import re
import sys
import time
import hashlib
import xml.etree.ElementTree as ET
from types import SimpleNamespace
import requests

try:
    import feedparser
except ImportError:
    feedparser = None

from quality_filter import get_quality_symbols, DEFAULT_TOP_N

# ----------------------------- CONFIG ---------------------------------

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}

RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
    "https://cryptopotato.com/feed/",
    "https://news.google.com/rss/search?q=cryptocurrency+OR+bitcoin+OR+ethereum+OR+crypto+etf+when:1d&hl=en-US&gl=US&ceid=US:en",
]

EXCLUDED_BASE_ASSETS = set()  # no coins excluded -- BTC/ETH/SOL news included too

COIN_NAME_TO_TICKER = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "binance coin": "BNB",
    "bnb chain": "BNB",
    "ripple": "XRP",
    "cardano": "ADA",
    "avalanche": "AVAX",
    "dogecoin": "DOGE",
    "polkadot": "DOT",
    "chainlink": "LINK",
    "polygon": "POL",
    "uniswap": "UNI",
    "litecoin": "LTC",
    "near protocol": "NEAR",
    "aptos": "APT",
    "arbitrum": "ARB",
    "optimism": "OP",
    "sui network": "SUI",
    "injective": "INJ",
    "celestia": "TIA",
    "bittensor": "TAO",
    "render": "RENDER",
    "fetch.ai": "FET",
    "aave": "AAVE",
    "makerdao": "MKR",
    "lido": "LDO",
    "pendle": "PENDLE",
    "ethena": "ENA",
    "ondo": "ONDO",
    "toncoin": "TON",
    "tron": "TRX",
    "stellar": "XLM",
    "hedera": "HBAR",
    "cosmos": "ATOM",
    "filecoin": "FIL",
    "immutable": "IMX",
    "arweave": "AR",
    "vechain": "VET",
    "zcash": "ZEC",
    "monero": "XMR",
    "algorand": "ALGO",
    "pepe": "PEPE",
    "shiba inu": "SHIB",
    "bonk": "BONK",
    "floki": "FLOKI",
    "worldcoin": "WLD",
    "jupiter": "JUP",
    "pyth": "PYTH",
    "sei network": "SEI",
    "starknet": "STRK",
    "eigenlayer": "EIGEN",
}

NEWS_CATEGORIES = {
    "Listing": [
        "lists", "listing", "to list", "will list", "adds trading", "launches trading",
        "binance lists", "coinbase lists", "upbit lists", "perpetual futures for",
    ],
    "Delisting": [
        "delist", "delisting", "removes trading", "suspends trading", "will delist",
    ],
    "Hack / Exploit": [
        "hack", "hacked", "exploit", "exploited", "drained", "rug pull", "rugpull",
        "stolen", "breach", "vulnerability", "security incident", "private key compromised",
    ],
    "ETF / Institutional Flow": [
        "etf inflow", "etf inflows", "etf outflow", "etf outflows", "spot etf",
        "files for etf", "etf approval", "blackrock", "ibit", "fidelity",
        "microstrategy", "strategy buys", "institutional buying", "whale bought",
        "whale accumulation", "treasury reserve",
    ],
    "Macro / Fed / CPI": [
        "federal reserve", "fed rate", "rate cut", "rate hike", "fomc", "jerome powell",
        "cpi inflation", "cpi data", "core cpi", "nonfarm payroll", "jobless claims",
        "us treasury yield", "pce inflation", "tariff",
    ],
    "Regulation": [
        "sec charges", "sec sues", "sec approves", "sec drops", "lawsuit", "regulator",
        "regulatory", "cftc", "banned", "ban on", "settles with sec", "class action",
        "doj", "stablecoin bill", "crypto bill",
    ],
    "Mainnet / Upgrade": [
        "mainnet launch", "mainnet live", "hard fork", "token burn", "token buyback",
        "major upgrade", "v2 launch", "v3 launch", "v4 launch", "staking upgrade",
        "token unlock",
    ],
    "Partnership": [
        "partners with", "partnership with", "teams up with", "collaborat", "integrat",
        "selected by", "adopts",
    ],
}

MARKET_WIDE_CATEGORIES = {"Macro / Fed / CPI", "ETF / Institutional Flow", "Regulation"}

REQUEST_TIMEOUT = 15
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "seen_news.txt")
SEEN_CACHE_MAX = 600

IMPACT_BY_CATEGORY = {
    "Listing": "Bullish",
    "Delisting": "Bearish",
    "Hack / Exploit": "Bearish",
    "Partnership": "Bullish",
    "Mainnet / Upgrade": "Bullish",
}
REGULATION_BULLISH_KEYWORDS = [
    "sec approves", "approves", "court rules in favor", "clears", "wins lawsuit",
    "sec drops", "dismisses", "pro-crypto", "etf approval",
]
REGULATION_BEARISH_KEYWORDS = [
    "sec sues", "sec charges", "lawsuit", "banned", "ban on",
    "settles with sec", "class action", "cftc charges", "doj charges",
]
ETF_BULLISH_KEYWORDS = [
    "inflow", "inflows", "buys", "bought", "accumulation", "approves", "approval",
    "files for", "launches", "treasury reserve",
]
ETF_BEARISH_KEYWORDS = [
    "outflow", "outflows", "sells", "sold", "dump", "redemption", "rejects", "delays",
]
MACRO_BULLISH_KEYWORDS = [
    "rate cut", "cuts rates", "dovish", "cpi lower", "inflation cools", "inflation slows",
    "pause rate", "liquidity injection",
]
MACRO_BEARISH_KEYWORDS = [
    "rate hike", "hikes rates", "hawkish", "cpi hotter", "inflation rises",
    "inflation beats", "tariff", "yields surge",
]

RECENT_NEWS_FILE = os.path.join(os.path.dirname(__file__), "..", "recent_news.json")
RECENT_NEWS_RETENTION_HOURS = 72


# ----------------------------- COIN UNIVERSE ---------------------------------

def get_coin_universe():
    """Base assets pulled from Binance's tradable USDT pairs, intersected with
    the shared quality filter when available."""
    resp = requests.get("https://data-api.binance.vision/api/v3/exchangeInfo", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    bases = set()
    for s in data["symbols"]:
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["isSpotTradingAllowed"]:
            base = s["baseAsset"]
            if base in EXCLUDED_BASE_ASSETS:
                continue
            if len(base) < 2:
                continue
            bases.add(base.upper())

    quality_symbols = get_quality_symbols(DEFAULT_TOP_N)
    if quality_symbols is not None:
        filtered = bases & quality_symbols
        filtered |= (bases & set(COIN_NAME_TO_TICKER.values()))
        bases = filtered
    else:
        print("Quality filter unavailable this run -- proceeding without it (fundamentals check skipped).")

    return bases


# ----------------------------- MATCHING ---------------------------------

def find_category(text):
    text_lower = text.lower()
    for category, keywords in NEWS_CATEGORIES.items():
        for kw in keywords:
            if kw in text_lower:
                return category
    return None


def determine_impact(category, text_lower):
    """General historical-tendency impact, not a guarantee."""
    if category == "Regulation":
        for kw in REGULATION_BULLISH_KEYWORDS:
            if kw in text_lower:
                return "Bullish"
        for kw in REGULATION_BEARISH_KEYWORDS:
            if kw in text_lower:
                return "Bearish"
        return "Mixed"
    if category == "ETF / Institutional Flow":
        for kw in ETF_BEARISH_KEYWORDS:
            if kw in text_lower:
                return "Bearish"
        for kw in ETF_BULLISH_KEYWORDS:
            if kw in text_lower:
                return "Bullish"
        return "Bullish"
    if category == "Macro / Fed / CPI":
        for kw in MACRO_BULLISH_KEYWORDS:
            if kw in text_lower:
                return "Bullish"
        for kw in MACRO_BEARISH_KEYWORDS:
            if kw in text_lower:
                return "Bearish"
        return "Mixed"
    if category == "Mainnet / Upgrade" and "token unlock" in text_lower:
        return "Bearish"
    return IMPACT_BY_CATEGORY.get(category, "Mixed")


AMBIGUOUS_TICKERS = {"ONE", "GAS", "SUN", "WIN", "HOT", "FUN", "KEY", "AMP", "FOR", "OUT", "NOW", "NEW", "ACT", "THE", "CAT", "DOG", "AI"}


def find_mentioned_coin(text, universe, category=None):
    """Look for a coin ticker (e.g. '$ARB' or 'ARB') or full project name
    (e.g. 'Chainlink' -> 'LINK', 'Bitcoin' -> 'BTC') in the headline."""
    tokens = re.findall(r"\$?[A-Za-z0-9]{2,10}", text)
    for token in tokens:
        has_dollar = token.startswith("$")
        clean = token.lstrip("$").upper()
        if clean not in universe:
            continue
        if clean in AMBIGUOUS_TICKERS and not (has_dollar or token.isupper()):
            continue
        if has_dollar or token.isupper():
            return clean

    text_lower = text.lower()
    for name, ticker in COIN_NAME_TO_TICKER.items():
        if ticker in universe and re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            return ticker

    for token in tokens:
        clean = token.lstrip("$").upper()
        if clean in universe and clean not in AMBIGUOUS_TICKERS and len(clean) >= 3:
            return clean

    if category in MARKET_WIDE_CATEGORIES and "BTC" in universe:
        return "BTC"

    return None


def _parse_xml_bytes(xml_bytes, fallback_title=""):
    """Standard-library ElementTree RSS/Atom parser fallback when feedparser is unavailable."""
    root = ET.fromstring(xml_bytes)
    feed_title = (root.findtext("./channel/title") or root.findtext("{http://www.w3.org/2005/Atom}title") or fallback_title).strip()
    entries = []
    for item in root.findall(".//item"):
        entries.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "summary": (item.findtext("description") or "").strip(),
        })
    if not entries:
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(f".//{ns}entry"):
            link_el = entry.find(f"{ns}link")
            link = (link_el.get("href") if link_el is not None else "") or (entry.findtext(f"{ns}link") or "")
            entries.append({
                "title": (entry.findtext(f"{ns}title") or "").strip(),
                "link": link.strip(),
                "summary": (entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content") or "").strip(),
            })
    return SimpleNamespace(feed={"title": feed_title}, entries=entries)


def fetch_feed(feed_url):
    """Fetch RSS XML with a real browser User-Agent first to avoid Cloudflare 403 blocks,
    using feedparser when installed and falling back to stdlib ElementTree."""
    try:
        resp = requests.get(feed_url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200 and resp.content:
            if feedparser is not None:
                parsed = feedparser.parse(resp.content)
                if getattr(parsed, "entries", None):
                    return parsed
            return _parse_xml_bytes(resp.content, fallback_title=feed_url)
    except Exception:
        pass
    if feedparser is not None:
        return feedparser.parse(feed_url)
    return SimpleNamespace(feed={"title": feed_url}, entries=[])


# ----------------------------- STATE ---------------------------------

def headline_hash(title, link):
    norm_title = re.sub(r"\s+", " ", str(title or "").strip().lower())
    return hashlib.sha256(norm_title.encode("utf-8")).hexdigest()


def load_seen():
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_seen(seen_list):
    trimmed = seen_list[-SEEN_CACHE_MAX:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(trimmed) + ("\n" if trimmed else ""))


def load_recent_news_state():
    if not os.path.exists(RECENT_NEWS_FILE):
        return []
    try:
        import json
        with open(RECENT_NEWS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return []


def save_recent_news_state(entries):
    import json
    cutoff = time.time() - RECENT_NEWS_RETENTION_HOURS * 3600
    trimmed = [e for e in entries if e.get("timestamp", 0) >= cutoff]
    with open(RECENT_NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, indent=1)


def record_recent_news(entries, coin, category, impact, title, link):
    entries.append({
        "coin": coin,
        "category": category,
        "impact": impact,
        "title": title,
        "link": link,
        "timestamp": time.time(),
    })


# ----------------------------- DISCORD ---------------------------------

def send_news_alert(coin, category, title, link, source, impact):
    if not DISCORD_WEBHOOK_URL:
        print(f"No DISCORD_WEBHOOK_URL set, skipping send. [{category}] {coin}: {title}")
        return

    color_map = {
        "Listing": 3066993,
        "Delisting": 15158332,
        "Hack / Exploit": 15158332,
        "Partnership": 3447003,
        "Regulation": 15105570,
        "ETF / Institutional Flow": 3066993 if impact == "Bullish" else 15158332,
        "Macro / Fed / CPI": 10181046,
        "Mainnet / Upgrade": 3447003,
    }
    impact_emoji = {"Bullish": "🟢", "Bearish": "🔴", "Mixed": "🟡"}.get(impact, "🟡")

    embed = {
        "title": f"📰 {category}: {coin}",
        "description": title,
        "url": link,
        "color": color_map.get(category, 10070709),
        "fields": [
            {"name": "Asset / Scope", "value": f"{coin}/USDT" if coin != "BTC" else "BTC / Market-Wide", "inline": True},
            {"name": "Likely Impact", "value": f"{impact_emoji} {impact}", "inline": True},
        ],
        "footer": {"text": f"Source: {source} · Typical historical tendency, not guaranteed — "
                            f"confirm on chart before acting · DeepPocket Tracker"},
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 300:
            print(f"Discord send failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Discord send error: {e}", file=sys.stderr)


# ----------------------------- MAIN ---------------------------------

def main():
    print("DeepPocket Tracker News Monitor: starting scan...")
    try:
        universe = get_coin_universe()
    except requests.RequestException as e:
        print(f"Failed to fetch coin universe: {e}", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    seen_set = set(seen)
    new_seen = list(seen)
    recent_news_entries = load_recent_news_state()
    alerts_sent = 0

    for feed_url in RSS_FEEDS:
        try:
            parsed = fetch_feed(feed_url)
        except Exception as e:
            print(f"Feed error {feed_url}: {e}", file=sys.stderr)
            continue

        source_name = getattr(parsed, "feed", {}).get("title", feed_url) if hasattr(parsed, "feed") else feed_url
        entries = getattr(parsed, "entries", []) or []

        for entry in entries[:35]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "") or entry.get("description", "")
            if not title or not link:
                continue

            h = headline_hash(title, link)
            if h in seen_set:
                continue

            combined_text = f"{title} {summary[:240]}"
            category = find_category(title) or find_category(combined_text)
            if not category:
                new_seen.append(h)
                seen_set.add(h)
                continue

            coin = find_mentioned_coin(title, universe, category=category) or find_mentioned_coin(combined_text, universe, category=category)
            if not coin:
                new_seen.append(h)
                seen_set.add(h)
                continue

            impact = determine_impact(category, combined_text.lower())
            print(f"NEWS ALERT [{category}] {coin}: {title} (impact: {impact})")
            send_news_alert(coin, category, title, link, source_name, impact)
            record_recent_news(recent_news_entries, coin, category, impact, title, link)
            alerts_sent += 1
            new_seen.append(h)
            seen_set.add(h)
            time.sleep(0.3)

    save_seen(new_seen)
    save_recent_news_state(recent_news_entries)
    print(f"News scan complete. {alerts_sent} alert(s) sent.")


if __name__ == "__main__":
    main()
