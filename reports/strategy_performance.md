# DeepPocket - Strategy Performance (net of fees + slippage, in R)

Closed trades analysed: **19**. Decisions need >= 30 closed trades per strategy; below that the table is information, not a verdict.

| Strategy | Mode | Total Trades | Wins | Losses | BE | Win Rate | Win Rate 95% CI | Avg Win | Avg Loss | Profit Factor | Expectancy (R) | t-stat | Net P&L | Max Drawdown (R) | Ambiguous | Recommendation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Apex Trend | LIVE | 1 | 0 | 0 | 1 | 0% | 0-79% | 0.95 | 0.00 | inf | +0.42 | - | +0.95 | 0.0 | 0 | INSUFFICIENT DATA (1/30) - keep forward-testing |
| Consensus: Alpha Prime, Elite Trend, Global Core | LIVE | 8 | 3 | 1 | 4 | 38% | 14-69% | 1.05 | 0.00 | inf | +0.50 | 7.38 | +8.42 | 0.0 | 0 | INSUFFICIENT DATA (8/30) - keep forward-testing |
| Consensus: Alpha Prime, Prime Edge, Smart Money EMA20 Pullback, Elite Trend, Nova Alpha, Global Core, Quant Prime | LIVE | 1 | 0 | 0 | 1 | 0% | 0-79% | 0.60 | 0.00 | inf | +1.44 | - | +0.60 | 0.0 | 0 | INSUFFICIENT DATA (1/30) - keep forward-testing |
| Consensus: Alpha Prime, Smart Money CVD Breakdown, Elite Trend, Global Core | LIVE | 4 | 3 | 0 | 1 | 75% | 30-95% | 1.33 | 0.00 | inf | +0.67 | 8.72 | +5.30 | 0.0 | 0 | INSUFFICIENT DATA (4/30) - keep forward-testing |
| Consensus: Alpha Prime, Smart Money EMA20 Pullback, Elite Trend, Global Core | LIVE | 1 | 0 | 1 | 0 | 0% | 0-79% | 0.00 | -1.00 | 0.00 | -1.43 | - | -1.00 | -1.4 | 0 | INSUFFICIENT DATA (1/30) - keep forward-testing |
| Consensus: Elite Trend, Smart Vector, Apex Trend | LIVE | 1 | 1 | 0 | 0 | 100% | 21-100% | 1.55 | 0.00 | inf | +0.69 | - | +1.55 | 0.0 | 0 | INSUFFICIENT DATA (1/30) - keep forward-testing |
| Consensus: Smart Money EMA20 Pullback, Apex Trend | LIVE | 1 | 0 | 0 | 1 | 0% | 0-79% | 0.94 | 0.00 | inf | +0.46 | - | +0.94 | 0.0 | 0 | INSUFFICIENT DATA (1/30) - keep forward-testing |
| Consensus: Smart Vector, Apex Trend | LIVE | 2 | 2 | 0 | 0 | 100% | 34-100% | 1.55 | 0.00 | inf | +0.71 | 37.26 | +3.10 | 0.0 | 0 | INSUFFICIENT DATA (2/30) - keep forward-testing |

## Does the Quality Score work? (forward data)

| Bucket | Trades | Win Rate | Expectancy (R) | Profit Factor |
|---|---|---|---|---|
| score >= 70 | 15 | 53% | +0.63 | - |
| no score | 4 | 25% | +0.05 | 3.09 |

## Do the tiers work? (A = full size, B = half, C = track only)

| Tier | Trades | Win Rate | Expectancy (R) | Profit Factor | Net P&L |
|---|---|---|---|---|---|
| Tier A | 15 | 53% | +0.63 | - | +17.77 |
| no tier | 4 | 25% | +0.05 | 3.09 | +2.10 |

_If Tier A does not beat Tier B and Tier B does not beat Tier C after 30+ trades each, the score weights are wrong and must be changed (as a new, versioned forward test)._

## Reality check: net P&L per day

- Days with closed trades: **3**; positive days: **2**
- Mean net P&L per day: **+6.62 USD** (+4.4% of the 150 USD reference account); day-to-day stdev 10.53 USD
- Best day +18.73 USD, worst day -0.40 USD
- A daily target is an outcome, not something a system can promise: with a stdev this large, a few days say almost nothing about the next one.

## Exit diagnostics (is a time stop or an earlier TP1 justified?)

| Strategy | Mode | Median hours held | Median hours held (stopped) | Median peak profit before SL | Stopped trades that were >= 1R up first |
|---|---|---|---|---|---|
| Apex Trend | LIVE | 7.5 | - | - | - |
| Consensus: Alpha Prime, Elite Trend, Global Core | LIVE | 0.7 | - | 0.98% | 1/1 |
| Consensus: Alpha Prime, Prime Edge, Smart Money EMA20 Pullback, Elite Trend, Nova Alpha, Global Core, Quant Prime | LIVE | 11.2 | - | - | - |
| Consensus: Alpha Prime, Smart Money CVD Breakdown, Elite Trend, Global Core | LIVE | 2.6 | - | - | - |
| Consensus: Alpha Prime, Smart Money EMA20 Pullback, Elite Trend, Global Core | LIVE | - | - | 0.00% | 0/1 |
| Consensus: Elite Trend, Smart Vector, Apex Trend | LIVE | 1.5 | - | - | - |
| Consensus: Smart Money EMA20 Pullback, Apex Trend | LIVE | 2.9 | - | - | - |
| Consensus: Smart Vector, Apex Trend | LIVE | 3.9 | - | - | - |

_Read: if many stopped trades were already >= 1R in profit, TP1/break-even is too far away; if stopped trades are held far longer than winners, a time stop is worth a versioned forward test. Do not decide on < 30 stopped trades._
