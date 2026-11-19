"""CoinGecko Demo API DEX swap-flow confirmation for the Whale factor (v2.3).

This measures confirmed on-chain DEX swaps in selected liquid pools ($2M+ reserve).
Expanded watchlist includes top DeFi/ERC-20 & wrapped assets where on-chain DEX
liquidity is deepest and most informative.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_BASE = "https://api.coingecko.com/api/v3/onchain"
STATE_FILE = Path(__file__).resolve().parent.parent / "dex_flow_state.json"
TIMEOUT = 20
SCAN_INTERVAL_SECONDS = 3 * 60 * 60
POOL_REFRESH_SECONDS = 24 * 60 * 60
MIN_POOL_LIQUIDITY_USD = 2_000_000
MIN_WINDOW_VOLUME_USD = 100_000
MIN_TRADE_USD = 100
MIN_TRADES = 20
MIN_NET_FLOW_PCT = 40.0   # v2.3: raised from 20% to 40% (data showed >=45% imbalance drove almost all TP1/TP2 wins)

# Exact token IDs/symbols prevent an unrelated token with the same ticker from
# being mistaken for a top asset. Wrapped equivalents and high-liquidity DeFi tokens are included.
ASSET_MATCHES = {
    "BTC": {"ids": {"bitcoin", "wrapped-bitcoin", "binance-peg-bitcoin", "coinbase-wrapped-btc"}, "symbols": {"BTC", "WBTC", "BTCB", "CBBTC"}},
    "ETH": {"ids": {"ethereum", "weth"}, "symbols": {"ETH", "WETH"}},
    "BNB": {"ids": {"binancecoin", "wbnb", "wrapped-bnb"}, "symbols": {"BNB", "WBNB"}},
    "XRP": {"ids": {"ripple", "binance-peg-xrp", "wrapped-xrp"}, "symbols": {"XRP", "WXRP"}},
    "SOL": {"ids": {"solana", "wrapped-solana", "wrapped-sol"}, "symbols": {"SOL", "WSOL"}},
    "ADA": {"ids": {"cardano", "binance-peg-cardano", "wrapped-cardano"}, "symbols": {"ADA", "WADA"}},
    "DOGE": {"ids": {"dogecoin", "binance-peg-dogecoin", "wrapped-dogecoin"}, "symbols": {"DOGE", "WDOGE"}},
    "TRX": {"ids": {"tron", "binance-peg-trx", "wrapped-tron"}, "symbols": {"TRX", "WTRX"}},
    "TON": {"ids": {"the-open-network", "toncoin", "wrapped-ton"}, "symbols": {"TON", "WTON"}},
    "AVAX": {"ids": {"avalanche-2", "wrapped-avax"}, "symbols": {"AVAX", "WAVAX"}},
    "LINK": {"ids": {"chainlink"}, "symbols": {"LINK"}},
    "DOT": {"ids": {"polkadot", "binance-peg-polkadot", "wrapped-dot"}, "symbols": {"DOT", "WDOT"}},
    "LTC": {"ids": {"litecoin", "binance-peg-litecoin", "wrapped-litecoin"}, "symbols": {"LTC", "WLTC"}},
    "BCH": {"ids": {"bitcoin-cash", "binance-peg-bitcoin-cash", "wrapped-bitcoin-cash"}, "symbols": {"BCH", "WBCH"}},
    "UNI": {"ids": {"uniswap"}, "symbols": {"UNI"}},
    "ATOM": {"ids": {"cosmos", "binance-peg-cosmos-token", "wrapped-atom"}, "symbols": {"ATOM", "WATOM"}},
    # v2.3: Deep-liquidity on-chain DeFi & L1/L2 tokens
    "AAVE": {"ids": {"aave"}, "symbols": {"AAVE"}},
    "ARB": {"ids": {"arbitrum"}, "symbols": {"ARB"}},
    "OP": {"ids": {"optimism"}, "symbols": {"OP"}},
    "LDO": {"ids": {"lido-dao"}, "symbols": {"LDO"}},
    "PENDLE": {"ids": {"pendle"}, "symbols": {"PENDLE"}},
    "ENA": {"ids": {"ethena"}, "symbols": {"ENA"}},
    "ONDO": {"ids": {"ondo-finance"}, "symbols": {"ONDO"}},
    "MKR": {"ids": {"maker"}, "symbols": {"MKR"}},
    "SUI": {"ids": {"sui"}, "symbols": {"SUI"}},
    "INJ": {"ids": {"injective-protocol"}, "symbols": {"INJ"}},
}


def _read_state():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _headers():
    key = os.environ.get("COINGECKO_DEMO_API_KEY", "").strip()
    return {"x-cg-demo-api-key": key, "accept": "application/json"} if key else None


def _parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _candidate_pool(payload, ticker):
    match = ASSET_MATCHES[ticker]
    included = {
        token.get("id"): token.get("attributes", {})
        for token in payload.get("included", [])
        if token.get("type") == "token"
    }
    candidates = []
    for pool in payload.get("data", []):
        attrs = pool.get("attributes", {})
        rels = pool.get("relationships", {})
        matched_side = None
        matched_token = None
        for side in ("base_token", "quote_token"):
            token_id = (rels.get(side, {}).get("data") or {}).get("id")
            token = included.get(token_id, {})
            token_symbol = str(token.get("symbol", "")).upper()
            coin_id = str(token.get("coingecko_coin_id", "")).lower()
            id_match = coin_id in match["ids"]
            symbol_match = token_symbol in match["symbols"]
            if symbol_match and (id_match or not coin_id):
                matched_side, matched_token = side.replace("_token", ""), token
                break
        if not matched_side:
            continue
        try:
            reserve = float(attrs.get("reserve_in_usd") or 0)
            volume_24h = float((attrs.get("volume_usd") or {}).get("h24") or 0)
        except (TypeError, ValueError):
            continue
        if reserve < MIN_POOL_LIQUIDITY_USD or volume_24h <= 0:
            continue
        pool_id = str(pool.get("id", ""))
        if "_" not in pool_id:
            continue
        network, address = pool_id.split("_", 1)
        candidates.append({
            "network": network,
            "address": address,
            "token_side": matched_side,
            "token_symbol": matched_token.get("symbol"),
            "token_id": matched_token.get("coingecko_coin_id"),
            "pool_name": attrs.get("name", ""),
            "reserve_usd": reserve,
            "volume_24h_usd": volume_24h,
        })
    if not candidates:
        return None
    return max(candidates, key=lambda item: min(item["reserve_usd"], item["volume_24h_usd"]))


def _find_pool(session, ticker):
    response = session.get(
        f"{API_BASE}/search/pools",
        params={"query": ticker, "include": "base_token,quote_token"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return _candidate_pool(response.json(), ticker)


def _pool_for(session, state, ticker, now):
    cached = state.get("pools", {}).get(ticker)
    if isinstance(cached, dict):
        try:
            checked_at = float(cached.get("checked_at", 0))
        except (TypeError, ValueError):
            checked_at = 0
        if now - checked_at < POOL_REFRESH_SECONDS:
            return cached.get("pool")

    pool = _find_pool(session, ticker)
    state.setdefault("pools", {})[ticker] = {"checked_at": now, "pool": pool}
    return pool


def _fetch_trade_flow(session, pool, since_epoch):
    response = session.get(
        f"{API_BASE}/networks/{pool['network']}/pools/{pool['address']}/trades",
        params={"token": pool["token_side"]},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    trades = response.json().get("data", [])
    relevant = []
    for trade in trades:
        attrs = trade.get("attributes", {})
        stamp = _parse_time(attrs.get("block_timestamp", attrs.get("timestamp")))
        if stamp is None or stamp < since_epoch:
            continue
        try:
            volume = float(attrs.get("volume_in_usd") or 0)
        except (TypeError, ValueError):
            continue
        if volume < MIN_TRADE_USD:
            continue
        kind = str(attrs.get("kind", "")).lower()
        if kind not in {"buy", "sell"}:
            continue
        relevant.append((stamp, kind, volume))

    buys = sum(volume for _, kind, volume in relevant if kind == "buy")
    sells = sum(volume for _, kind, volume in relevant if kind == "sell")
    total = buys + sells
    count = len(relevant)
    if total < MIN_WINDOW_VOLUME_USD or count < MIN_TRADES:
        return None

    if len(trades) >= 300:
        oldest = min((_parse_time(item.get("attributes", {}).get("block_timestamp",
                                                    item.get("attributes", {}).get("timestamp")))
                      for item in trades), default=None)
        if oldest is not None and oldest > since_epoch:
            return None

    net_pct = 100 * (buys - sells) / total if total else 0.0
    if abs(net_pct) < MIN_NET_FLOW_PCT:
        return None
    return {
        "direction": "LONG" if net_pct > 0 else "SHORT",
        "net_buy_sell_imbalance_pct": net_pct,
        "buy_volume_usd": buys,
        "sell_volume_usd": sells,
        "window_volume_usd": total,
        "trade_count": count,
        "from_timestamp": datetime.fromtimestamp(since_epoch, timezone.utc).isoformat(),
        "pool": pool["pool_name"],
        "network": pool["network"],
        "pool_liquidity_usd": pool["reserve_usd"],
        "source": "CoinGecko Demo / GeckoTerminal confirmed DEX swaps (on-chain)",
    }


def get_onchain_dex_flows(symbols):
    """Return fresh on-chain DEX buy/sell imbalances for supported top coins."""
    headers = _headers()
    if not headers:
        print("[onchain-dex] missing COINGECKO_DEMO_API_KEY; on-chain DEX checks skipped")
        return {}

    state = _read_state()
    now = time.time()
    try:
        last_scan = float(state.get("last_scan_epoch", 0))
    except (TypeError, ValueError):
        last_scan = 0
    if now - last_scan < SCAN_INTERVAL_SECONDS:
        return {}
    since_epoch = max(last_scan, now - SCAN_INTERVAL_SECONDS) if last_scan else now - SCAN_INTERVAL_SECONDS

    requested = []
    for symbol in symbols:
        value = str(symbol).upper()
        if value.endswith("USDT"):
            base = value[:-4]
            if base in ASSET_MATCHES:
                requested.append((value, base))

    session = requests.Session()
    session.headers.update(headers)
    signals = {}
    for symbol, ticker in requested:
        try:
            pool = _pool_for(session, state, ticker, now)
            if not pool:
                print(f"[onchain-dex] {ticker}: no verified liquid DEX pool found")
                continue
            flow = _fetch_trade_flow(session, pool, since_epoch)
            if flow:
                signals[symbol] = flow
        except requests.RequestException as exc:
            print(f"[onchain-dex] {ticker}: provider request failed: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[onchain-dex] {ticker}: response could not be parsed: {exc}")

    state["last_scan_epoch"] = now
    state["last_scan_iso"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
    _write_state(state)
    return signals
