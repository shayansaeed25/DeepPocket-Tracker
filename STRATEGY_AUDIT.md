# Ten-strategy implementation

The latest `to_send_shayan_20260927_1646.zip` includes `quantx.db`, exact source records for the ten selected IDs, `strategy_brand_names.json`, and a 15-coin performance matrix. The bot now uses the database's exact source paths and parser-generated indicator parameters in `strategies/strategy_profiles.json`; the exact source text for each winner is retained in `strategies/database_sources/`.

The scanner applies the supplied generic dynamic-strategy engine's detected primary-indicator family to Binance's last closed candles. It evaluates every strategy independently on the configured timeframes (currently 1h and 4h), suppresses an individual rule if the two timeframes disagree, then merges all remaining same-coin, same-direction strategy hits into one consensus signal naming every agreeing rule. Opposing strategy directions on a coin are filtered. A single six-hour cooldown applies to that coin's combined Strategy signal. BUY/SELL entries, ATR-based stop-loss, TP1 at 2R and TP2 at 3.5R are reported and tracked. The original generic backtest engine is long-only; this live signal adapter mirrors its indicator rules for SELL/short alerts to support the bot's requested buy/sell output. These are indicator-rule signals, not a literal replay of each source file's strategy code.

Current policy (v2, see `src/strategy_policy.py`): Whale Flow BUY/SELL is the only LIVE source. Whale Volume (spike and pressure) is disabled. Smart Vector solo is a BUY-only WATCH ONLY forward test. Watch-only consensus groups are exactly: Elite Trend + Smart Vector, Prime Edge + Quant Prime, and Elite Trend + Quant Prime + Signal Edge. Every other group (and every group containing Apex Trend, Nova Alpha, Global Core, Alpha Prime, Core Momentum, Trend Momentum or another Signal Edge combination) is suppressed. Consensus is scored by independent indicator families (WMA/DEMA/VWMA are one family), not by the number of rule names. Watch-only alerts are marked and paper-tracked; promotion to LIVE needs the evidence bar in `src/strategy_stats.py`. Despite the historical table below, these archived rules are not all active.

| Rank | ID | Display brand (source archive name) | Primary rule | BTC/USDT 4h return | Trades | DB win rate* |
|---:|---:|---|---|---:|---:|---:|
| 1 | 2881 | Alpha Prime (Vanguard Prime) | Donchian | 74.21% | 20 | 47.37% |
| 2 | 469 | Core Momentum (Trident Divergence) | RSI | 54.78% | 34 | 76.47% |
| 3 | 8457 | Prime Edge (Nautilus Core) | WMA | 45.03% | 128 | 34.88% |
| 4 | 119 | Elite Trend (Storm Watch) | Donchian | 37.82% | 81 | 38.89% |
| 5 | 5068 | Nova Alpha (Beacon Chart) | WMA | 37.56% | 115 | 37.18% |
| 6 | 486 | Smart Vector (Compass Trend) | Momentum | 37.06% | 136 | 38.69% |
| 7 | 8449 | Global Core (Sentinel Data) | Donchian | 33.61% | 33 | 52.17% |
| 8 | 796 | Apex Trend (Horizon ESG) | Momentum | 32.01% | 132 | 32.90% |
| 9 | 3713 | Quant Prime (Anchor DEMA) | DEMA | 32.00% | 160 | 43.48% |
| 10 | 120 | Signal Edge (Tide Volume) | VWMA | 27.22% | 69 | 34.15% |

*The win rates come from the `backtests` table for BTC/USDT; that table does not store the timeframe alongside each row. The separate matrix reports BTC/USDT 4h return/trades, so the two columns should not be treated as the same backtest run. The available database win rates range from 32.90% to 76.47%, not around 90%. This does not establish what happened in a different test you ran, but the attached files do not substantiate a 90% win rate for these records.

The matrix includes results for only 15 coins, not all 200. The bot still runs the ten rules across the configured top-200 universe; the supplied matrix is kept as reference data and is not presented as validated 200-coin performance. The provided backtest returns are historical summaries and do not predict live signal accuracy.

## Free on-chain DEX flow in the Whale factor

`src/dex_onchain.py` uses CoinGecko Demo's on-chain DEX endpoints (powered by GeckoTerminal). The workflow passes `COINGECKO_DEMO_API_KEY` from GitHub Secrets. The scanner searches for DEX pools for the top-16 watchlist and only accepts recognized CoinGecko token identities with at least $2 million in pool liquidity. It reads recent swaps every three hours and requires at least 20 trades, $100,000 window volume, and a 20% buy-vs-sell USD imbalance before recording a low-confidence LONG/SHORT confirmation. If it cannot find a suitable verified pool, it skips that coin.

This measures actual DEX swaps recorded on-chain. It is not centralized-exchange wallet inflow/outflow, does not identify long-term holder intent, and may not cover every coin in the watchlist. The API returns at most 300 trades per pool within the past 24 hours; the scanner suppresses a signal if that cap makes the requested interval appear incomplete. At one token/pool discovery per coin per day plus one trade query per coin every three hours, expected usage is about 4,320 calls/month, below the Demo plan's 10,000 monthly calls, before retries or other requests. The [Demo endpoint list](https://docs.coingecko.com/demo/reference/endpoint-overview) includes pool search and pool trades, and the [free plan](https://www.coingecko.com/en/api/pricing) documents its call quota. Flow is now labeled `WHALE FLOW · ON-CHAIN DEX`; its Discord evidence is concise while the full evidence remains in the ledger.

## Whale Volume buy/sell pressure candidate

The original 3x-volume-plus-technical Whale Volume setup remains unchanged. A second independent Whale Volume subtype checks three closed Binance spot candles: quote-volume average must be at least 1.5x the previous 20-candle average, taker-buy quote share must be at least 58% for BUY or at most 42% for SELL, and price must have moved at least 1% in the same direction. SL/TP use ATR risk multiples. This is public exchange trade-flow data; it cannot prove institutional identity or actual wallet ownership.

## Source labels and TP/SL tracking

Journal and Google Sheets sources distinguish Whale Volume, Whale Flow, Trend Momentum, and individual strategies. Discord posts a factor-labeled alert when TP1, TP2, or SL is touched by a Binance candle. TP1 remains a milestone and the position stays open; TP2 or SL closes it. These are price-touch observations, not exchange fills.

Santiment's separate centralized-exchange balance metric remains restricted; its free plan excludes recent data. See [Santiment's metric access](https://academy.santiment.net/metrics/exchange-funds-flow/) and [plan limits](https://academy.santiment.net/products-and-plans/sanapi-plans/).
