# LeadingConfluencePro v2.00 — Autonomous Cloud Macro, ML Optimizer & MT5 System

A universal, strictly non-repainting MetaTrader 5 indicator and **24/7 serverless cloud quantitative engine** built for **any symbol** (Forex, Metals, Energy, Indices, Crypto) and **any timeframe** (`M1` through `MN1`).

It combines **leading on-chart microstructure** (BVC + CLV Order Flow, CVD Divergence, VSA, Wyckoff Liquidity Sweeps/SFP, Bollinger–Keltner Volatility Squeezes, FVG/Displacement, Adaptive VWAP, Kaufman Efficiency Ratio, ATR Percentile, Causal MTF) with **24/7 cloud-computed Macro / On-Chain / ETF / COT / Volatility Term-Structure feeds** and **Purged Walk-Forward ML Meta-Labeling** running completely without your laptop turned on.

---

## Live Cloud Endpoints & Dashboard (`gamehotspot62/mt5-leading-confluence-cloud`)

- **Live Web Dashboard (GitHub Pages):** `https://gamehotspot62.github.io/mt5-leading-confluence-cloud/`
- **Macro / On-Chain / ETF / COT Bias Feed:** `https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/MacroFlowBias.csv`
- **Cloud Walk-Forward Optimized Params:** `https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/OptimizedConfig.csv`
- **Cloud Trained ML Meta-Model Weights:** `https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/MLModelWeights.csv`
- **Live Closed-Bar Signals JSON:** `https://raw.githubusercontent.com/gamehotspot62/mt5-leading-confluence-cloud/main/data/latest_signals.json`

---

## Project Files

- [LeadingConfluencePro.mq5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluencePro.mq5) & [LeadingConfluencePro.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluencePro.ex5) — Adaptive v2.00 MT5 indicator with native logistic ML meta-filter & 11 `iCustom` buffers.
- [LeadingConfluenceCloudBridge.mq5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceCloudBridge.mq5) & [LeadingConfluenceCloudBridge.ex5](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/LeadingConfluenceCloudBridge.ex5) — MT5 background service syncing cloud CSVs & Terminal Global Variables every 5 minutes.
- [index.html](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/index.html) — Live GitHub Pages dashboard visualizing macro stress, watchlist telemetry, confirmed signals, and ML audit metrics.
- [RESEARCH_AND_DESIGN.md](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/RESEARCH_AND_DESIGN.md) — Complete research synthesis and mathematical specifications.
- `cloud/`:
  - [data_clients.py](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/cloud/data_clients.py) — Resilient keyless HTTP clients (Yahoo Finance v8, FRED, CFTC Socrata COT, CoinMetrics Community, Deribit, DefiLlama, Fear & Greed).
  - [fetch_macro_data.py](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/cloud/fetch_macro_data.py) — Hourly macro/on-chain/ETF/COT aggregator.
  - [features.py](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/cloud/features.py) — Exact causal Python mirror of MQL5 v2.00 indicators + 10 standardized ML features.
  - [ml_optimizer.py](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/cloud/ml_optimizer.py) — Purged & embargoed walk-forward parameter optimizer + calibrated Logistic & HistGradientBoosting meta-labeler.
  - [live_signal_scanner.py](file:///C:/Users/Lenovo/.gemini/antigravity/scratch/mt5-leading-confluence-indicator/cloud/live_signal_scanner.py) — Hourly cloud signal scanner with deduplicated Telegram/Discord notifications.

---

## MetaTrader 5 Installation & Cloud Sync Setup

1. Open MetaTrader 5 → **File → Open Data Folder**.
2. Copy `LeadingConfluencePro.ex5` (and optionally `.mq5`) into `MQL5\Indicators\`.
3. Copy `LeadingConfluenceCloudBridge.ex5` (and optionally `.mq5`) into `MQL5\Services\`.
4. Whitelist the GitHub raw HTTPS domain for `WebRequest`:
   - Go to **Tools → Options → Expert Advisors**.
   - Check **Allow WebRequest for listed URL**.
   - Add **`https://raw.githubusercontent.com`** and click **OK**.
5. Start the Cloud Sync Background Service:
   - In MT5 **Navigator**, right-click **Services → Refresh**.
   - Right-click **LeadingConfluenceCloudBridge** → **Add Service** → confirm settings → **OK**.
   - Verify in the **Experts** tab: `LeadingConfluenceCloudBridge: Sync OK | Macro GVs=...`
6. Drag **LeadingConfluencePro** onto any chart. It automatically reads cloud macro biases, walk-forward optimized parameters, and ML model weights whenever synced (and safely falls back to manual defaults on any symbol not in the watchlist).

> **Note on Zero-Laptop Operation:** All cloud data harvesting, ML retraining, parameter optimization, web dashboard updates, and Telegram/Discord signal alerts run 24/7 on GitHub Actions even when your laptop is powered off. If you also want your **MT5 desktop terminal itself** (and MT5 push/trade execution) online 24/7 without keeping your laptop on, run MT5 + `LeadingConfluenceCloudBridge` on a Windows MT5 VPS.

---

## Optional Zero-Laptop Telegram / Discord Cloud Alerts

To receive confirmed `H1`/`H4` Buy/Sell signals directly on your phone even when MT5 and your laptop are off:
1. Open your GitHub repository `gamehotspot62/mt5-leading-confluence-cloud` → **Settings → Secrets and variables → Actions**.
2. Add any of:
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
   - `DISCORD_WEBHOOK_URL`
3. The hourly workflow (`live_data_and_signals.yml`) automatically pushes deduplicated closed-bar signals whenever a new setup confirms.

---

## `iCustom()` Buffer Map (v2.00 — 11 Buffers)

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

> **EA Integration Tip:** Always read signal buffers from shift **`1`** (latest closed bar) and never from shift `0`.
