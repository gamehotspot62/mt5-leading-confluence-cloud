# Global Leading-Indicator Research & Engineering Design (`LeadingConfluencePro` v2.00)

## Executive Summary

Most retail MetaTrader indicators fail across symbols and timeframes because they rely on **lagging price-only transformations** (moving-average crossovers, MACD, RSI, Stochastic) that react long after institutional order execution has completed, repaint live bars, or collapse when applied across different volatility and liquidity regimes (FX, Metals, Indices, Crypto, Equities).

This document synthesizes global empirical research across **Trading Psychology & Behavioral Finance**, **Order Flow & Market Microstructure**, **Volume Spread Analysis (VSA)**, **Institutional Fair Value & Volatility Compression**, **Cross-Asset Regime Filtering**, **On-Chain / ETF / COT / Volatility Term-Structure Flows**, and **Purged Walk-Forward ML Meta-Labeling**—and documents the autonomous v2.00 cloud + MT5 architecture.

---

## 1. Research Synthesis & On-Chart Microstructure

### 1.1 Trading Psychology & Behavioral Finance (Measurable On-Chart)
- **Loss Aversion & Stop Clustering:** Traders cluster protective stops right behind obvious structural swing highs/lows. Institutional block algorithms require counterparty liquidity and sweep those resting stop clusters before reversing. Implemented via **Wyckoff Spring/Upthrust & Swing Failure Pattern (Liquidity Sweep Playbook)**.
- **Herding & FOMO Chasing:** Retail traders chase extended candles far from fair value. Guarded via **VWAP Z-Score anti-chasing caps** (`|vwapZ| <= 1.2` on sweeps, `<= 2.2` on squeeze breakouts, `<= 1.8` on pullbacks).
- **Strict Closed-Bar Discipline:** Signals evaluate and stamp exclusively on completed bars (`i <= rates_total - 2`). The live forming bar (`rates_total - 1`) never emits arrows or locks signals.

### 1.2 Order Flow & Market Microstructure (Leading On-Chart Proxies)
Grounded in **Easley, López de Prado, and O'Hara (2012, 2016)**:
- **Bulk Volume Classification (BVC):**
  $$\text{BVC}_{\text{net}, i} = 2 \cdot \Phi\!\left(\frac{C_i - C_{i-1}}{\sigma_{\Delta P, 20}}\right) - 1 \in [-1, +1]$$
- **Close Location Value (CLV) Absorption Blend:**
  $$\text{CLV}_i = \frac{(C_i - L_i) - (H_i - C_i)}{H_i - L_i}, \quad \text{OFI}_i = 0.60 \cdot \text{BVC}_{\text{net}, i} + 0.40 \cdot \text{CLV}_i$$
- **Estimated Bar Delta & Cumulative Volume Delta (CVD):** $\Delta_i = V_{\text{eff}, i} \cdot \text{OFI}_i$, $\text{CVD}_i = \text{CVD}_{i-1} + \Delta_i$, used to detect bullish/bearish **CVD Divergence** at liquidity sweeps.
- **Volume Spread Analysis (VSA):** **Relative Volume (`RVOL`)** vs trailing 20-bar baseline paired with rejection wicks and CLV.

### 1.3 Fair Value Gaps (FVG), Institutional Displacement & Volatility Context (v2.00 Upgrade)
- **Fair Value Gap (FVG):** 3-candle liquidity void (`low[i] > high[i-2]` bullish; `high[i] < low[i-2]` bearish) left by unilateral institutional imbalance.
- **Displacement:** High-conviction body expansion (`|close - open| >= 0.65 * ATR`) backed by `RVOL >= 1.25` and `|CLV| >= 0.50`.
- **ATR Percentile Rank (`0..100` over 100 bars):** Separates dead-liquidity chop (`< 8th percentile`, where non-squeeze triggers are suppressed) from early volatility expansion.

### 1.4 Cross-Asset Regime & Causal Multi-Timeframe Filtering
- **Kaufman Efficiency Ratio (`KER`):** Scale-free `[0, 1]` trend-efficiency metric smoothed with a 3-bar EMA into `RegimeBuffer`.
- **Causal HTF Bias:** Auto-maps each chart timeframe to its structural higher timeframe and aligns strictly **completed** HTF bars (`htfCloseTime <= barCloseTime`).

---

## 2. Autonomous 24/7 Cloud Macro / On-Chain / ETF / COT Engine

Because MT5 custom indicators run on the chart UI thread (where `WebRequest()` raises error `4014`), v2.00 decouples cloud ingestion into two stages:
1. **Serverless Cloud Pipeline (`cloud/fetch_macro_data.py` on GitHub Actions every hour):**
   - **Volatility Term Structure:** `^VIX / ^VIX3M` ratio (`< 1.0` contango risk-on vs `> 1.0` backwardation stress).
   - **FRED Macro & Liquidity:** High-Yield Credit OAS (`BAMLH0A0HYM2`), Real 10Y TIPS Yield (`DFII10`), 10Y–2Y Curve (`T10Y2Y`), and Net Fed Liquidity (`WALCL - RRPONTSYD*1000 - WTREGEN`, normalized to millions USD).
   - **Cross-Asset Breadth & ETF Money Flow:** `RSP/SPY` breadth, `HYG/TLT` credit appetite, `QQQ/XLP` risk appetite, `DX-Y.NYB`, and 10-day Chaikin-style money flow on `IBIT`, `FBTC`, `ETHA`, `GLD`, `SLV`, `USO`, `SPY`, `QQQ`.
   - **CFTC Commitments of Traders (Socrata JSON API):** TFF (`gpe5-46if`) Asset Manager & Leveraged Money percentiles + Disaggregated (`72hh-3qpy`) Managed Money & Producer/Merchant percentiles (with `USDJPY` automatically inverted since CME JPY futures are quoted `JPY/USD`).
   - **Crypto On-Chain & Perpetual Derivatives:** CoinMetrics Community (`CapMVRVCur`, `FlowInExUSD`, `FlowOutExUSD`, `SplyExNtv`), DefiLlama 30d Stablecoin supply growth, Deribit perpetual 8h funding contrarian extremes, and Alternative.me Fear & Greed.
2. **MT5 Background Service (`LeadingConfluenceCloudBridge.mq5`):**
   - Uses `#property service` (dedicated background thread) to poll the public GitHub raw CSV endpoints every 5 minutes via `WebRequest()`, writing `MacroFlowBias.csv`, `OptimizedConfig.csv`, and `MLModelWeights.csv` cleanly to `MQL5\Files\` (or `Common\Files`) and publishing `MACRO_BIAS_*` + `LCP_CLOUD_STATUS` Terminal Global Variables.

---

## 3. Purged Walk-Forward Triple-Barrier ML & Parameter Optimization

Grounded in **Marcos López de Prado (*Advances in Financial Machine Learning*)**:
1. **Triple-Barrier Labeling (`cloud/ml_optimizer.py`):** Evaluates candidate setups bar-by-bar over a 30-bar vertical horizon with conservative SL-first tie-breaking and a `0.05R` spread/slippage cost deduction (`y = 1` when TP1 hits before SL).
2. **Purged & Embargoed Chronological Splits:** Expanding walk-forward folds purge all training events whose exit bar overlaps into `[test_start - embargo, ...]`, preventing temporal leakage.
3. **Stability-Penalized Parameter Optimization:** Searches a curated low-overfit grid on `H1` and `D1` maximizing:
   $$\text{Obj} = \text{OOS\_ExpR} \cdot \sqrt{\min(N_{\text{OOS}}, 60)} - 0.35 \cdot \sigma_{\text{folds}} - 0.10 \cdot \text{MaxDD}_R$$
   Adopts tuned parameters only when `OOS_Trades >= 12` and `OOS_ExpR > 0`; otherwise exports safe defaults (`FALLBACK_DEFAULT`).
4. **Native Cross-Asset Meta-Labeling (`MLModelWeights.csv`):** Trains both a calibrated **StandardScaler + LogisticRegression (`C=0.5`)** model (whose 10 feature weights export directly to MQL5 for native tickless evaluation inside `LeadingConfluencePro.mq5`) and a calibrated **`HistGradientBoostingClassifier`** benchmark on identical purged OOS folds, choosing the optimal probability cutoff (`ML_MIN_PROB`) out-of-sample.

---

## 4. Honest Quantitative Expectations

No quantitative system eliminates losing trades across shifting macro regimes. In `LeadingConfluencePro` v2.00, **"confirmed signal"** strictly means:
1. Closed-bar non-repainting price/microstructure confirmation (`rates_total - 2`).
2. Multi-factor confluence score `>= MIN_SCORE` (with optional Macro/On-Chain/ETF alignment).
3. Positive out-of-sample calibrated ML meta-probability gate (`ml_prob >= ML_MIN_PROB`).
4. Explicit structural invalidation (`SL`) and asymmetric targets (`TP1` / `TP2`), audited transparently in real time via causal statistics.
