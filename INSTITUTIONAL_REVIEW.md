# v2.2 - aap ki dono xlsx files aur naye mashwaron ka jawab

## 0. Sab se pehle ek sachchi baat (ghar ke kharche ke baare mein)

Aap ne likha ke 10-15 USD roz chahiye taake ghar ka kharcha chale. $150 par 10-15 USD roz **7% se 10% roz** hai. Koi professional desk ye promise nahi karti, aur ye bot bhi nahi kar sakta. Aap ka apna data:

- Pehle hafte (6 din, 250 paper trades) ka net **-$363** tha (fees ke baad -$433). Roz ka average **-$15**, sirf 2 din positive, sab se achha din +$21 (36 trades ke saath), sab se bura din -$88.
- Ye bot abhi **paper forward-test** hai. Koi strategy ab tak saabit nahi hui. Is paise par ghar ka kharcha mat rakhein. Sirf wo paisa lagayein jo kho jaye to ghar na ruke.

Isi liye weekly report mein ab "Reality check: net P&L per day" hai: asal roz ka average, stdev, positive din. Target wo nateeja hai jo report dikhaye, wo cheez nahi jo hum promise karen.

## 1. Aap ki xlsx se kya nikla (fees 0.30% ke baad, R mein)

`DEEPPOCKETS.xlsx` (pehla hafta, 231 closed trades): sab milakar E[R] **-0.58** per trade, t-stat **-4.3**, yani nuqsan itefaqi nahi tha, wo configuration waqai negative thi.

| Source | Trades | Net $ | E[R] | t | Note |
|---|---|---|---|---|---|
| Smart Vector solo | 103 | -332 | -0.62 | -5.5 | **BUY: 109 trades, 6% TP2, -$342. SELL: 6 trades, 83% TP2, +$24** (girti market mein long haare, short jeete) |
| Consensus (sab) | 71 | -75 | -0.80 | -2.2 | |
| Whale Volume | 35 | -35 | -0.51 | -2.4 | band hi rahe |
| Whale Flow | 15 | +2 | +0.06 | 0.1 | **BUY: 6 trades, 0 jeet, -$17. SELL: 9 trades, 56%, +$19** |
| Movers Pullback | 7 | +7 | +0.70 | 0.9 | sirf 7 trades |

Stop ki chaudai: 4% se bada stop = 85 trades, **-$376**. 1.5-2.5% stop = +$12. Trade kitni der khuli rehti hai (median): SL **6.9 ghante**, BE 16h, TP2 **21.8 ghante**.

`DEEPPOCKETS_2.xlsx` (naya build, sirf 12 trades, 3 closed, +$3.4): itna chhota sample hai ke kuch bhi nahi batata.

## 2. Doosre AI ke mashwaron ka verdict (data ke saath)

| Mashwara | Verdict |
|---|---|
| "Roz kam az kam 5 signals" | Signals ki kami masla thi hi nahi: pehle hafte mein **roz ~42 paper trades** aate the aur wahi nuqsan mein the. Kam signals ki asal wajah scan sirf 55-62 coins par chalna tha (neeche). Is liye: 200 coins + roz ka **Top-5 board** (best 5 ranked setups). Quota jabri nahi: signal na ho to board khud likhta hai "sirf N aaye". |
| 3-tier system (A/B/C) | **Apnaya**, kuch tabdeelion ke saath. Un ka rule "regime ALIGNED lazmi" NEUTRAL din ko poora Tier C bana deta (aur wo khud NEUTRAL mein signals chahte hain). Mera grading: score aur trend/families se, BTC regime ulta ho to C. |
| "3-way consensus +26.32, sab wins" | **Ghalat.** Aap ki xlsx mein ye group sirf **4 trades** hai: +$17.16 (2 TP2, 1 BE, 1 SL). Hamesha sample size dekhein. |
| "Whale Flow BUY rakho, SELL band karo" | **Ulta data.** BUY 6 trades 0 jeet (-$17), SELL 9 trades +$19. Un ka daava sirf 3 open trades par tha. Dono taraf regime guard aur tier decide karte hain. |
| "Smart Vector solo band" | Theek, wo watch-only hai. **Lekin** pehle wala "sirf BUY" rule khud nuqsan ki wajah tha (BUY -$342). Ab dono taraf, BTC regime guard ke saath. |
| NEUTRAL mein threshold 45 / 24h baad 50 / "rolling threshold" | **Nahi.** Sab se kharab market mein standard giraana ulta hai, aur threshold ko roz hilana filter ko tune kar ke noise fit karna hai. Tier C band nahi hota, sirf track hota hai. |
| "Daily guarantee: roz 1 Tier B trade lazmi" | **Nahi.** Jabri trade. Board dikhata hai, trade aap ka faisla. |
| Cooldown 4h -> 2h | **Nahi.** Wahi coin dobara dobara = wahi ek bet, double risk. 6h rakha. |
| Scan har 30 min | **Nahi.** Rules 1h/4h *closed* candles par chalte hain, 30 minute mein naya information nahi aata. Har ghante candle band hone ke 3 minute baad. |
| "Smart Vector quality 25 bhi chalega" (ADA +4.36) | **Nahi.** Ek trade se rule nahi banta. |
| "Time stop 24h" (pehle wale paste mein) | **Nahi.** Data ulta: jeetne wali trades median 21.8h leti hain, 24h ka time stop aadhi jeet kaat deta. |
| Movers hamesha allow, "quality 40+" | Movers ko ab wahi score aur tier milta hai (sirf 7 trades ka data hai, special chhoot nahi). |
| Daily max loss, 3 SL ke baad break, max 5 positions | Pehle se hai (risk_manager). |

## 3. Bot mein kya add hua (v2.2)

1. **Universe: top-200 coins (bug fix).** Pehle sirf ~55 coins scan hote the. Wajah: CoinGecko ki market-cap top-200 list mein se sirf wo coins bachte the jo Binance par bhi USDT mein hain. Ab allow-list top-500 tak hai, phir Binance volume ke hisab se **top 200** (stablecoin/meme/leveraged bahar, $2M se kam volume bahar). Ek aur bug: leveraged-token filter substring se chalta tha ("UP" in name) aur JUP, SUPER jaise asal coins phenk deta tha. Har run ka log batata hai kitne coins kis filter se kate.
2. **Allow-list cache.** CoinGecko ek baar fail ho to purani list (72 ghante tak) use hoti hai, universe chupke se nahi badalta.
3. **Tiers A/B/C.** A: score 70+, 2+ independent families, trend ALIGNED/PARTIAL, regime ulta nahi = poora risk (1% equity). B: score 50+ = aadha (0.5%). C = sirf track. Live-eligible signal C ban jaye to **watch-only mein downgrade** hota hai (delete nahi). Discord alert mein tier aur us tier ke hisab se size likha hota hai. Weekly report mein "Do the tiers work?" table hai: agar A, B se behtar nahi to weights galat hain.
4. **Daily Top-5 board** (06:00, 12:00, 18:00 UTC): din ke 5 sab se behtar setups, tier ke saath. Sirf wahi signals jo scan ne pehle se register kiye, naye trades nahi banata.
5. **Smart Vector solo dono taraf**, **minimum stop 1%** (0.30% fee ke saath 0.5% stop mein 0.6R sirf cost mein chala jata).
6. **Report:** tier table, roz ka net P&L reality check.
7. **Dashboard reset:** `FRESH_START_VERSION = "zero-reset-v2-2-tiers"`. Pehli run Google Sheet (Signals, Positions, Backtesting, Dashboard) saaf kar degi. State files zero hain.

## 4. Kya test hua

161 unit tests pass; `screener.main()` ka mocked end-to-end run. Live Binance/CoinGecko/Discord/Sheets ke saath test nahi hua. **Pehli run ka log zaroor dekhein**: `[universe]` wali line mein kitne coins scan hue wo likha hoga.

---

# Institutional Review (v2) - aap ke Excel analysis ka jawab

Aap ne jo "Excel Data Ka Asal Natija" wala analysis diya tha, maine us ki har baat code aur repo ke data se check ki.
Neeche likha hai: kya sahi tha (rakha), kya galat ya adhoora tha (theek kiya), aur kahan do data sources aapas mein takraate hain.

**Pehle ek zaroori baat:** analysis ke numbers (+26.32 USD, +11.71 USD, -16.77 USD wagaira) aap ki Excel file se aaye hain. Wo Excel file zip mein nahi thi, sirf summary text thi, is liye main un numbers ko khud verify nahi kar saka. Aur "3 strategies agree hon to win rate ~75%" ka koi saboot is repo ke data mein nahi hai, to maine usay kisi rule ya document mein nahi likha.

---

## 1. Verdict, baat ba baat

| Analysis ki baat | Verdict | Ab code mein kya hai |
|---|---|---|
| Smart Vector solo band karo | Sahi simt. Repo ka review-week data: 105 trades, 80 SL, -$301 | WATCH ONLY (sirf BUY, stop <= 2.5%). Live nahi. 30+ closed trades aur stats pass hon tab hi promote |
| Whale Flow solo band karo | Sahi, **lekin** repo ke review-week mein Whale Flow +$13 tha (do data sources takraate hain). Aur uska stop (flat 2.5%) ab badal gaya hai, yani purana data naye rule ka saboot nahi | WATCH ONLY forward test. `WHALE_FLOW_MODE = "ACTIVE"` se live ho sakta hai |
| 3-way consensus (Elite Trend + Quant Prime + Signal Edge) sab se behtar | Mumkin hai, lekin sample chhota hai aur review-week mein har Signal Edge group negative tha | Ek khaas, pehle se tay forward test (WATCH ONLY). Baqi Signal Edge groups band |
| Prime Edge + Quant Prime rakho | Theek, magar ye WMA + DEMA hai = **ek hi family** (dono moving-average trend followers). "2 strategies agree" asal mein ~1 vote hai | WATCH ONLY. Quality score mein 1 independent family ginti hai |
| Elite Trend + Smart Vector rakho | Theek. Ye breakout + momentum = 2 alag families | WATCH ONLY |
| Smart Vector + Signal Edge, Prime Edge + Signal Edge band karo | Sahi | Pehle se suppressed |
| Whale Volume "filter ke sath rakho" | **Nahi.** Repo data: 37 trades, -$23, 68% SL | Band hi rahi (`WHALE_VOLUME_ENABLED = False`). Wapas lani ho to pehle evidence chahiye |
| Movers Pullback rakho | Theek | Watch-only forward test, pehle jaisa |
| "2-way mein sirf Elite Trend + Quant Prime" (aakhri mashwara) | Analysis ki apni table mein is jore ka koi number hi nahi, aur ye upar ki baat se takrata hai | Alag se enable nahi kiya. Wo jora 3-way group ke andar already hai |

## 2. 10 steps: kya sahi tha, kya badla

1. **Trend filter H4 + D1** - sahi, institutional standard. Ab Quality Score ka 30 point ka hissa. Live signal ke liye COUNTER-TREND (dono timeframe khilaf) seedha reject.
2. **Volume ratio >= 1.5** - sahi. Live gate hai (1.5 se kam = reject). Score mein 2.5x par full credit.
3. **ATR-based SL/TP** - strategy signals mein pehle se 1.7x ATR tha. Asal kami **Whale Flow** mein thi (har coin ka flat 2.5% stop). Ab ATR stop + wahi 0.5%-4% stop-size gate.
4. **Signal frequency kam karo** - scan pehle se har ghante, cooldown 6 ghante (aap ke 4 ghante se sakht), burst caps pehle se. Ab live positions ki global limit bhi (max 5).
5. **Quality Score formula** - **galat tha.** `Volume Ratio x 25` unbounded hai: ek 12x spike baqi sab kuch dba deta. Ab har cheez pehle 0..1 mein normalize hoti hai, phir weight (trend 30, volume 25, independent confluence 25, regime 20). Threshold 70 ek hypothesis hai, saboot nahi, is liye watch-only signals ko block nahi karta, sirf score log hota hai taake forward data se check ho (weekly report mein "score >= 70 vs < 70" table).
6. **Trailing stop 0.5x ATR** - **nahi lagaya.** 0.5x ATR normal shor ke andar hai, jeet ko scratch bana deta hai. TP1 ke baad break-even pehle se hai. Trailing lagani ho to 2-3x ATR aur alag version ke taur par forward test.
7. **Regime filter** - BEARISH mein sirf SELL / BULLISH mein sirf BUY: ab symmetric (BTC momentum <= -25 par BUY band, >= +25 par SELL band; pehle sirf BUY guard tha). **"NEUTRAL mein koi trade nahi"** nahi apnaya: zyada tar din data hi nahi banta. Neutral ko score mein aadha credit milta hai.
8. **Consensus lazmi** - consensus ab *independent families* se ginti hai, naam ki ginti se nahi. Solo signals live nahi, sirf watch-only.
9. **Risk management** - `src/risk_manager.py`: 1% risk per trade (Discord alert batata hai aap ki equity par kitna size 1% risk hoga), max 5 open live positions / 5% open risk, same-direction risk max 3% (altcoins aik doosre ke saath chalte hain, 5 longs = zyada tar ek hi bet), break-even position risk nahi ginti. 3 SL lagatar = 2 ghante pause, aur -3R ek din mein = us din band. Watch-only trades breaker ko trigger nahi karte.
10. **Weekly review: lagatar nuqsan wali strategy 2 hafte band** - **galat tareeqa.** Chhote sample par on/off karna noise ko fit karna hai. Ab `src/strategy_stats.py` + `weekly_review.yml`: 30 closed trades se pehle koi faisla nahi; phir net expectancy, profit factor aur t-stat se PROMOTE / CONTINUE / SUSPEND. Win rate ke saath Wilson 95% interval (12 trades par "75%" asal mein "47% se 91% ke beech").

## 3. Wo kamiyan jo analysis mein thi hi nahi (aur theek ki gayin)

- **Fees aur slippage P&L mein nahi the.** Ab har closed trade par 0.30% round trip (fee 0.10% + slippage 0.05% har taraf). Naye columns: `cost_usd`, `net_pnl_usd`, `r_multiple`. `realized_pnl_usd` gross hi hai taake Google Sheet dashboard na toote.
- **Fixed $100 notional = risk har trade ka alag** (4% stop ka risk 1% stop se 4 guna). Is liye sab limits aur stats **R** mein hain (1R = us trade ka asal risk).
- **"Consensus" ki ginti** - upar dekhein, independent families.
- **BUY-only BTC guard** - ab symmetric.

## 4. Sheet structure ke baare mein (sach)

- Analysis ne Signals mein V/W/X columns (Quality Score, Consensus Count, Trend Alignment) aur nayi "Strategy Performance" sheet maangi thi.
- **Maine Google Sheet ka layout nahi chheda.** Sheets code (1400+ lines) mein fixed column maps hain aur main live Sheet ke bagair usay test nahi kar sakta tha; ek galat column poora dashboard bigaad deta.
- Is ke bajaye: journal CSV (`deep_pocket_log.csv`) mein naye columns `quality_score`, `r_multiple`, `net_pnl_usd`, `cost_usd`; `trend_alignment` aur `independent_families` har signal ke `details` JSON mein; aur "Strategy Performance" table `reports/strategy_performance.csv` / `.md` mein (har Monday khud banti hai, ya `python src/strategy_stats.py`). Chahein to agla kaam Google tab banana ho sakta hai.

## 5. Kya test hua, kya nahi

- 148 unit tests pass (pehle 114; 34 naye: quality score, risk limits, circuit breaker, costs/R, statistics, ATR levels, workflows). Ek purana test (`sells are not guarded`) maine jaan kar badla, kyunke ab SELL guard symmetric hai.
- Pure `screener.main()` ka mocked end-to-end run (Whale Flow watch-only aur live dono raste, consensus watch-only raasta).
- **Live Binance / CoinGecko / Discord / Google Sheets ke saath test nahi hua** (yahan network band tha). Pehli run Actions mein manually chala kar log dekh lein.
- Naye signals ke liye har candidate coin par 2-3 extra Binance klines calls (4h, 1d, kabhi 1h). Sirf candidates par, 200 coins par nahi.

## 6. Deploy

1. Poora zip repo mein upload karein (purani files overwrite). `open_trades.json`, `last_signals.txt` waghera pehle jaise khali/zero-state hain.
2. `src/risk_manager.py` mein `ACCOUNT_EQUITY_USD` ko apni asal equity par set karein (abhi 150 = dashboard reference).
3. Journal CSV ka header pehli run par khud naye columns ke saath migrate ho jata hai.
4. Actions -> **DeepPocket Tracker Scan** manually chala kar log check karein.

## 7. v2.1 mein jo add hua

- Consensus ab sirf same timeframe par agree karne wale rules se banta hai (1h + 4h ka mel ab consensus nahi).
- Liquidity floor: 24h volume $5M se kam wali pairs scan se bahar.
- Weekly report mein exit diagnostics (kitni der trade khuli rehti hai, SL se pehle kitna profit tha). **Time stop abhi nahi lagaya**: us ke liye naya exit outcome chahiye jo Sheets code nahi jaanta, aur data mein koi saboot nahi. Report se faisla karein.

## 8. Agle version ke liye (abhi nahi badla)

- TP policy (TP1 = 2%-5% clamp) se reward:risk har trade par alag hota hai; R-based targets zyada saaf hain.
- Whale Flow ko solo ke bajaye consensus ke confirmation factor ke taur par istemal karna.
