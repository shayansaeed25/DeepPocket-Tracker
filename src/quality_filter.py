"""
Shared "quality" coin filter for DeepPocket Tracker (v3.0).

Restricts live trading to high-liquidity, established crypto assets and
blacklists meme tokens, pump tokens, gold/commodity stable-pegs (`XAUT`, `PAXG`),
and synthetic stock/index tickers (`NVDAB`, `TSLAB`, `SPCXB`, `EWYB`, `MSTRB`, etc.).
"""

import json
import os
import time

import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT = 15

DEFAULT_TOP_N = 200
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "quality_symbols_cache.json")
CACHE_MAX_AGE_HOURS = 72
PAGE_SIZE = 250

# Core Top-30 Institutional High-Liquidity Crypto Assets prioritized during live scans
TOP_30_INSTITUTIONAL_BASES = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "TRX", "AVAX", "LINK", "DOT",
    "UNI", "LTC", "BCH", "NEAR", "APT", "ARB", "OP", "SUI", "INJ", "TIA",
    "AAVE", "ENA", "PENDLE", "ONDO", "RENDER", "FET", "TAO", "FIL", "ATOM", "HBAR",
    "WLD", "ZEC", "POL", "SEI", "JUP", "MKR", "LDO", "AR", "IMX", "STX",
}


def _load_cache(now=None):
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        age_hours = ((now or time.time()) - float(data["saved_at"])) / 3600
        if age_hours <= CACHE_MAX_AGE_HOURS and data.get("symbols"):
            return set(data["symbols"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save_cache(symbols):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "symbols": sorted(symbols)}, f)
    except OSError:
        pass


# Blacklist covering meme coins, pump tokens, synthetic stock/index mirrors (*B), and gold/fiat pegs
MANUAL_MEME_BLACKLIST = {
    "DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "MEME", "BABYDOGE",
    "ELON", "SAFEMOON", "MOG", "TURBO", "BOME", "POPCAT", "PONKE", "BRETT",
    "PUMP", "MUBARAK", "MARSCOIN", "BROCCOLI", "TST", "CHEEMS", "1000CAT",
    "1000CHEEMS", "1000SATS", "1000PEPE", "1000SHIB", "1000BONK", "1000FLOKI",
    "PNUT", "GOAT", "MOODENG", "NEIRO", "MEW", "DOGS", "HMSTR", "CATI",
    "BANANAS31", "SIREN", "TUT", "PENGU", "FARTCOIN", "MELANIA",
    # Synthetic stock/index/commodity tickers that caused false signals in Oct 17-27 logs:
    "NVDAB", "TSLAB", "SPCXB", "EWYB", "MSTRB", "CRCLB", "SKHYB", "KORUB",
    "XAUT", "PAXG", "RWUSD", "GRAM", "MUB", "WBTC",
}


def get_quality_symbols(top_n=DEFAULT_TOP_N):
    """Return a set of uppercase ticker symbols that are top-N by market cap
    and not meme/synthetic coins."""
    try:
        top_coins = []
        for page in range(1, (top_n + PAGE_SIZE - 1) // PAGE_SIZE + 1):
            top_resp = requests.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": min(PAGE_SIZE, top_n),
                    "page": page,
                    "sparkline": "false",
                },
                timeout=REQUEST_TIMEOUT,
            )
            top_resp.raise_for_status()
            top_coins.extend(top_resp.json())
            if page > 1:
                time.sleep(1.5)

        meme_resp = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "category": "meme-token",
                "sparkline": "false",
            },
            timeout=REQUEST_TIMEOUT,
        )
        meme_symbols = set()
        if meme_resp.status_code == 200:
            meme_symbols = {c["symbol"].upper() for c in meme_resp.json()}

    except (requests.RequestException, ValueError, KeyError) as e:
        cached = _load_cache()
        if cached is not None:
            print(f"[quality_filter] CoinGecko fetch failed ({e}); using the cached allow-list "
                  f"({len(cached)} symbols) instead of changing the universe.")
            return cached - MANUAL_MEME_BLACKLIST
        print(f"[quality_filter] CoinGecko fetch failed, skipping quality filter this run: {e}")
        return None

    quality = set()
    for c in top_coins:
        symbol = c.get("symbol", "").upper()
        if not symbol:
            continue
        if symbol in meme_symbols or symbol in MANUAL_MEME_BLACKLIST:
            continue
        quality.add(symbol)

    quality |= TOP_30_INSTITUTIONAL_BASES
    quality -= MANUAL_MEME_BLACKLIST
    if quality:
        _save_cache(quality)
    return quality
