# LeadingConfluencePro v2.01 — 15-Minute (`M15`) & `XAUUSD` Cloud ML + MT5 Trading Ecosystem

A universal, strictly non-repainting MetaTrader 5 indicator, 5-minute cloud sync service, optional `M15` auto-entry Expert Advisor, and **24/7 serverless cloud engine** checking signals **every 15 minutes** (`M15` + `H1`) with **`XAUUSD`** featured alongside Crypto, Forex, Energy, Metals, and Indices.

---

## Live Cloud Endpoints & Dashboard (`gamehotspot62/mt5-leading-confluence-cloud`)

- **Live Web Dashboard (GitHub Pages):** https://gamehotspot62.github.io/mt5-leading-confluence-cloud/
- **Macro / On-Chain / ETF / COT Bias Feed:** https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/MacroFlowBias.csv
- **Walk-Forward Optimized Settings (`M15`, `H1`, `D1`):** https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/OptimizedConfig.csv
- **Trained ML Meta-Model Weights:** https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/MLModelWeights.csv
- **Live 15-Minute Closed-Bar Signals JSON:** https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/latest_signals.json

---

## Project Files

- [LeadingConfluencePro.mq5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluencePro.mq5) & [LeadingConfluencePro.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluencePro.ex5) — v2.01 non-repainting indicator (11 buffers including `MLProbBuffer`).
- [LeadingConfluenceCloudBridge.mq5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceCloudBridge.mq5) & [LeadingConfluenceCloudBridge.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceCloudBridge.ex5) — Background `#property service` polling cloud CSVs every 5 minutes into `MQL5\Files\` and Terminal Global Variables.
- [LeadingConfluenceAutoEntry_M15.mq5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceAutoEntry_M15.mq5) & [LeadingConfluenceAutoEntry_M15.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceAutoEntry_M15.ex5) — Optional `M15` auto-entry Expert Advisor reading closed-bar (`shift = 1`) signals via `iCustom`, sizing by `%` equity risk, and managing TP1 partial close + breakeven.
- [index.html](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/index.html) — Live GitHub Pages dashboard showing `M15` telemetry, `M15`/`H1` signals, macro stress, and walk-forward ML audits.
- [RESEARCH_AND_DESIGN.md](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/RESEARCH_AND_DESIGN.md) — Empirical research synthesis and mathematical specifications.
- `cloud/` & `.github/workflows/` — 15-minute cloud signal scanner (`*/15 * * * *`), hourly macro refresh, and weekly purged walk-forward ML retraining.

---

## MetaTrader 5 Installation (`XAUUSD M15` Quick Start)

1. Open MetaTrader 5 → **File → Open Data Folder**.
2. Copy files to your MT5 data folder:
   - [LeadingConfluencePro.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluencePro.ex5) → `MQL5\Indicators\`
   - [LeadingConfluenceCloudBridge.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceCloudBridge.ex5) → `MQL5\Services\`
   - *(Optional Auto-Entry)* [LeadingConfluenceAutoEntry_M15.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceAutoEntry_M15.ex5) → `MQL5\Experts\`
3. **Allow Cloud HTTPS Requests:** Go to **Tools → Options → Expert Advisors**, check **Allow WebRequest for listed URL**, and add **`https://raw.githubusercontent.com`**.
4. **Start the Cloud Bridge Service:** In MT5 **Navigator → Services → Refresh**, right-click **LeadingConfluenceCloudBridge → Add Service** → **OK**. Confirm `Sync OK` appears in the **Experts** tab.
5. **Attach to `XAUUSD` `M15` Chart:** Open an `XAUUSD` chart on the **`M15`** timeframe and drag **LeadingConfluencePro** onto it. Broker symbol variants (`XAUUSDm`, `XAUUSD.raw`, `XAUUSD_i`, `GOLD`) are normalized automatically to `XAUUSD,M15`.
6. *(Optional)* Attach **LeadingConfluenceAutoEntry_M15** to the same `XAUUSD M15` chart (`InpDryRunLogOnly = true` first on Demo/Strategy Tester before enabling live `OrderSend`).

---

## How It Works (End-to-End Methodology)

1. **Cloud Layer (Zero Laptop Needed, 24/7):**
   - Every **15 minutes**, GitHub Actions refreshes macro/flow data (if older than 50 min: `VIX/VIX3M`, FRED/Treasury credit & real yields, `GLD`/ETF flows, CFTC COT, CoinMetrics, Deribit, DefiLlama, Fear & Greed) and runs `cloud/live_signal_scanner.py` across closed **`M15`** and **`H1`** candles for `XAUUSD` and the 9 other assets, sending Telegram/Discord alerts if secrets are configured.
   - Weekly, `cloud/ml_optimizer.py` runs **Triple-Barrier labeling** and **Purged Walk-Forward Cross-Validation** on `M15`, `H1`, and `D1` history, writing tuned parameters to `OptimizedConfig.csv` and calibrated logistic meta-model weights to `MLModelWeights.csv`.
2. **MT5 Cloud Sync Service (`LeadingConfluenceCloudBridge`):**
   - Runs on an independent background thread (avoiding MT5's `OnCalculate` error `4014` network restriction), polling the raw GitHub CSVs every 5 minutes into `MQL5\Files\` and `MACRO_BIAS_*` Terminal Global Variables.
3. **Live On-Chart Signal Engine (`LeadingConfluencePro` v2.01):**
   - **Parameter & ML Adoption:** Every 60 seconds, refreshes `OptimizedConfig.csv` (matching `(EXACT_SYM, EXACT_TF)` → `(NORM_SYM, EXACT_TF)` → `(NORM_SYM, ALL)` → `(GLOBAL, ALL)`), `MacroFlowBias.csv`, and `MLModelWeights.csv`. If an optimized parameter changes, triggers one clean recalculation.
   - **Real-Time Closed-Bar Catching:** Computes BVC + CLV Order Flow (`OFI`), `CVD`, `RVOL`, Rolling/Session `VWAP` ± bands, BB-inside-KC Squeeze + OLS Momentum, `KER`, 100-bar `ATR` percentile, `FVG`/Displacement, causal swing pivots (`p = i - 3`), and strictly completed `H1` macro bias (`htfCloseTime <= barCloseTime`).
   - **Zero-Repainting Gate:** Live bar (`rates_total - 1`) signal buffers are always forced empty (`EMPTY_VALUE` / `0.0`). Only when a 15-minute bar closes (`rates_total - 2`) does it score the 3 playbooks (`Sweep/SFP`, `SqzBreak`, `VWAPPull`) out of 100, run the native 10-feature logistic ML probability check (`EvaluateMLWinProb >= ML_MIN_PROB`), stamp the Buy/Sell arrow with `SL`/`TP1`/`TP2`, and fire a once-per-bar alert.
4. **Optional Auto-Entry EA (`LeadingConfluenceAutoEntry_M15`):**
   - Polls `iCustom` strictly at **`shift = 1`** (newly closed `M15` bar), checks spread and risk-based lot sizing (`0.5%` equity default), sends the market order with `SL` and `TP2`, and manages `50%` partial close + breakeven stop move once `TP1` is reached.

---

## `iCustom()` Buffer Map (v2.01 — 11 Buffers)

| Buffer Index | Name | Values |
| :---: | :--- | :--- |
| `0` | `BuySignalBuffer` | Arrow price (`low - 0.35*ATR`) on confirmed BUY bar, else `EMPTY_VALUE` |
| `1` | `SellSignalBuffer` | Arrow price (`high + 0.35*ATR`) on confirmed SELL bar, else `EMPTY_VALUE` |
| `2` | `VWAPBuffer` | Institutional VWAP value |
| `3` | `VWAPUpperBuffer` | Upper VWAP band |
| `4` | `VWAPLowerBuffer` | Lower VWAP band |
| `5` | `ScoreBuffer` | `+score` on BUY, `-score` on SELL, `0.0` otherwise |
| `6` | `SLBuffer` | Projected structural Stop Loss price on signal bar, else `EMPTY_VALUE` |
| `7` | `TP1Buffer` | Projected TP1 price on signal bar, else `EMPTY_VALUE` |
| `8` | `TP2Buffer` | Projected TP2 price on signal bar, else `EMPTY_VALUE` |
| `9` | `RegimeBuffer` | Smoothed Kaufman Efficiency Ratio (`0.0..1.0`) |
| `10` | `MLProbBuffer` | Calibrated Cloud ML win probability (`0.0..1.0`) on signal bar, else `0.0` |
