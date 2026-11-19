# Nayi Google Sheet ko connect karne ka tareeqa

Is bot ke liye alag spreadsheet use hogi. Purani spreadsheet ka ID yahan na dein.

1. Nayi Google Sheet banayein aur uska URL copy karein. URL mein `/d/` aur `/edit` ke darmiyan jo text hai, woh spreadsheet ID hai.
2. Service-account JSON mein `client_email` dekhein. Nayi sheet ko us email ke saath **Editor** ke taur par share karein. Wohi service-account JSON dobara use ho sakta hai; nayi API ya naya service account zaroori nahi, jab tak us account ko sheet tak access ho.
3. GitHub par DeepPocket-Tracker repo kholein: **Settings → Secrets and variables → Actions**.
4. **Secrets** mein `GOOGLE_SHEETS_SPREADSHEET_ID` banayein aur nayi sheet ka ID paste karein.
5. **Secrets** mein `GOOGLE_SERVICE_ACCOUNT_JSON` banayein aur service-account JSON ka poora matn paste karein.
6. Actions tab se **DeepPocket Tracker Scan** ko manually run karein.

On-chain DEX confirmation ke liye GitHub Secrets mein `COINGECKO_DEMO_API_KEY`
bhi hona chahiye. Iski free Demo key CoinGecko API account se milti hai; bot
is key ko sirf scheduled DEX-pool/trade requests mein use karta hai.

Connector `Signals`, `Positions`, `Backtesting`, `Dashboard`, aur `System Health` tabs bana ya update karega, magar aapko sirf `Positions` aur `Dashboard` nazar aayenge. `Signals`, `Backtesting`, aur `System Health` tabs hidden rahenge—delete nahi honge—kyunke bot unhein TP/SL tracking aur Dashboard formulas ke liye use karta hai. `Positions` live khuli/band paper positions dikhayega. `Dashboard` ka maujooda layout rahega aur usmein $150 starting-account reference ke saath all-time/current-month simulated P&L, wins/losses, TP/SL counts, break-even exits, open exposure aur estimated equity dikhegi.

Pehli successful connection par bot purani `Signals`, `Positions`, `Backtesting`, aur `System Health` rows aur Dashboard values **ek martaba clear** karega, phir isi run ke naye records likhega. Column headers, tabs, design, aur formatting rahenge. `dashboard_reset.json` marker reset ko dobara chalne se rokega. Isliye deploy se pehle Sheet ka data manually delete na karein; reset bot khud karega. Local open trades alert-silent rahengi aur nayi Sheet mein wapas add nahi hongi.

Har paper signal ka notional fixed $100 hai. Dashboard mein starting-account reference $150 hi rehta hai; is liye kai open positions ka combined notional $150 se zyada ho sakta hai. Yeh exchange par trade execute nahi karta. Fees, funding aur slippage P&L mein shamil nahi hain; Dashboard simulated/backtest estimate hai, guaranteed revenue nahi.

TP1 touch hone par Discord message batayega ke TP1 hit ho gaya hai aur remaining position ka SL entry par move karein. Tracker apni paper position mein stop entry par set karega; agar baad mein wahan touch ho to `BE_HIT` (break-even) record hoga, `SL_HIT` loss nahi. Yeh manual instruction hai; bot exchange par order nahi bhejta.

Pehli deployed run ek cutover timestamp save karegi. Is waqt pehle se open purane positions local tracker mein silently monitor hongi; unke TP/SL hits na Discord par alert honge na fresh Sheet mein likhe jayenge. Naye rollout ke baad bani trades par alerts aur Sheet records chalte rahenge.

Current policy (v2): Whale Flow BUY/SELL LIVE; Smart Vector solo (BUY only), Elite Trend + Smart Vector, Prime Edge + Quant Prime aur Elite Trend + Quant Prime + Signal Edge WATCH ONLY forward tests; Whale Volume band; baqi groups suppress. Watch-only signals bhi paper-tracked hain aur exchange orders nahi bhejte. Journal CSV mein ab `cost_usd`, `net_pnl_usd`, `r_multiple` aur `quality_score` columns hain (Google Sheet ka layout nahi badla). Weekly report `reports/strategy_performance.md` mein banti hai.

Google Sheets setup na ho to bot ka local `deep_pocket_log.csv` phir bhi chalta rahega. Credentials ZIP mein shamil nahi hain; unhein GitHub Secrets/Variables mein rakhna hai. Agar pehli run mein Google permission error aaye to service account ki `client_email` ko spreadsheet par Editor access dein.
