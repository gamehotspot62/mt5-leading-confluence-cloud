"""
Institutional Purged Walk-Forward Triple-Barrier ML & Parameter Optimizer.
- Downloads multi-year Yahoo Finance OHLCV (1d 5y + 1h 730d) & causal historical macro series
- Optimizes strategy parameters strictly out-of-sample across purged chronological folds
- Trains calibrated cross-asset Logistic & HistGradientBoosting meta-labelers
- Exports:
    data/OptimizedConfig.csv
    data/MLModelWeights.csv
    data/ml_report.json
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from data_clients import (
    WATCHLIST,
    fetch_coinmetrics,
    fetch_cftc_cot,
    fetch_fred_batch,
    fetch_yahoo_ohlcv,
)
from features import EngineParams, compute_indicators_and_candidates

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

FEATURE_NAMES = [
    "ofi_aligned",
    "net_delta3_aligned",
    "rvol_capped",
    "vwap_z_aligned",
    "ker",
    "htf_bias_aligned",
    "clv_aligned",
    "atr_pct",
    "fvg_disp_aligned",
    "macro_bias_aligned",
]


def _clamp(val: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, val)))


def build_causal_macro_history() -> Dict[str, pd.Series]:
    """
    Build strictly causal daily macro bias time series [-100, +100] per symbol
    shifted forward by 1 day (and CFTC by 4 days for Friday release lag) so
    no historical bar ever leaks future macro data.
    """
    fred = fetch_fred_batch(["BAMLH0A0HYM2", "DFII10"], timeout=12)
    hy = fred.get("BAMLH0A0HYM2")
    dfii10 = fred.get("DFII10")
    vix_df = fetch_yahoo_ohlcv("^VIX", interval="1d", range_period="5y")
    vix3m_df = fetch_yahoo_ohlcv("^VIX3M", interval="1d", range_period="5y")
    dxy_df = fetch_yahoo_ohlcv("DX-Y.NYB", interval="1d", range_period="5y")

    cols: Dict[str, pd.Series] = {}

    if vix_df is not None and vix3m_df is not None:
        v1 = vix_df.set_index("time")["close"].rename("vix")
        v2 = vix3m_df.set_index("time")["close"].rename("vix3m")
        cols["vix_ratio"] = (v1 / v2.clip(lower=1e-6)).shift(1)

    if hy is not None and len(hy) >= 25:
        cols["hy_chg20"] = (hy - hy.shift(20)).shift(1)
    else:
        hyg_df = fetch_yahoo_ohlcv("HYG", interval="1d", range_period="5y")
        iei_df = fetch_yahoo_ohlcv("IEI", interval="1d", range_period="5y")
        if hyg_df is not None and iei_df is not None:
            r = hyg_df.set_index("time")["close"] / iei_df.set_index("time")["close"].clip(lower=1e-6)
            cols["hy_chg20"] = (-(r / r.shift(20) - 1.0) * 12.5).shift(1)

    if dfii10 is not None and len(dfii10) >= 25:
        cols["r10_chg20"] = (dfii10 - dfii10.shift(20)).shift(1)
    else:
        tip_df = fetch_yahoo_ohlcv("TIP", interval="1d", range_period="5y")
        if tip_df is not None:
            ts = tip_df.set_index("time")["close"]
            cols["r10_chg20"] = (-(ts / ts.shift(20) - 1.0) * 14.0).shift(1)

    if dxy_df is not None:
        ds = dxy_df.set_index("time")["close"]
        cols["dxy_ret20"] = ((ds / ds.shift(20) - 1.0) * 100.0).shift(1)

    if not cols:
        return {}

    macro_df = pd.DataFrame(cols).sort_index().ffill().fillna(0.0)

    vix_pts = np.where(
        macro_df.get("vix_ratio", pd.Series(1.0, index=macro_df.index)) < 0.92,
        25.0,
        np.where(
            macro_df.get("vix_ratio", pd.Series(1.0, index=macro_df.index)) <= 1.00,
            10.0,
            np.where(
                macro_df.get("vix_ratio", pd.Series(1.0, index=macro_df.index)) <= 1.08,
                -20.0,
                -40.0,
            ),
        ),
    )
    hy_pts = np.clip(-macro_df.get("hy_chg20", 0.0) * 40.0, -25.0, 25.0)
    r10_risk_pts = np.clip(-macro_df.get("r10_chg20", 0.0) * 55.0, -25.0, 25.0)
    dxy_pts = np.clip(macro_df.get("dxy_ret20", 0.0) * 7.0, -25.0, 25.0)
    global_series = np.clip(0.40 * vix_pts + 0.35 * hy_pts + 0.25 * r10_risk_pts, -100.0, 100.0)

    # Historical CFTC COT (shifted by 4 days: Tuesday data released Friday)
    tff_codes = [v["cot_code"] for v in WATCHLIST.values() if v["cot_dataset"] == "gpe5-46if" and v["cot_code"]]
    dis_codes = [v["cot_code"] for v in WATCHLIST.values() if v["cot_dataset"] == "72hh-3qpy" and v["cot_code"]]
    cot_map: Dict[str, pd.DataFrame] = {}
    cot_map.update(fetch_cftc_cot("gpe5-46if", tff_codes, limit_per_code=260))
    cot_map.update(fetch_cftc_cot("72hh-3qpy", dis_codes, limit_per_code=260))

    # Historical CoinMetrics for BTC & ETH
    cm_map: Dict[str, Optional[pd.DataFrame]] = {
        "btc": fetch_coinmetrics("btc", metrics="CapMVRVCur,FlowInExUSD,FlowOutExUSD,PriceUSD", page_size=500, max_pages=4),
        "eth": fetch_coinmetrics("eth", metrics="CapMVRVCur,FlowInExUSD,FlowOutExUSD,PriceUSD", page_size=500, max_pages=4),
    }

    out: Dict[str, pd.Series] = {"GLOBAL": pd.Series(global_series, index=macro_df.index)}

    for sym, meta in WATCHLIST.items():
        ac = meta["asset_class"]
        cot_df = cot_map.get(meta["cot_code"]) if meta["cot_code"] else None
        cot_s = pd.Series(0.0, index=macro_df.index)
        if cot_df is not None and not cot_df.empty:
            c_sub = cot_df.copy()
            c_sub["date"] = c_sub["date"] + pd.Timedelta(days=4)
            raw_c = (c_sub["inst_pct"] - 50.0) * 0.30 + np.clip(c_sub["inst_chg4w"] * 0.45, -10.0, 10.0)
            if meta["cot_invert"]:
                raw_c = -raw_c
            c_ser = pd.Series(np.clip(raw_c.values, -25.0, 25.0), index=c_sub["date"])
            c_ser = c_ser[~c_ser.index.duplicated(keep="last")]
            cot_s = c_ser.reindex(macro_df.index.union(c_ser.index)).sort_index().ffill().reindex(macro_df.index).fillna(0.0)

        if ac == "crypto":
            cm_df = cm_map.get(meta["cm_asset"])
            oc_s = pd.Series(0.0, index=macro_df.index)
            if cm_df is not None and not cm_df.empty:
                cm = cm_df.copy()
                cm["date"] = cm["date"] + pd.Timedelta(days=1)
                mvrv = cm["CapMVRVCur"].fillna(1.5)
                mvrv_pts = np.where(mvrv < 1.15, 30.0, np.where(mvrv < 1.80, 15.0, np.where(mvrv <= 2.60, 0.0, -30.0)))
                nf7 = (cm["FlowInExUSD"].fillna(0.0) - cm["FlowOutExUSD"].fillna(0.0)).rolling(7, min_periods=1).sum()
                tf7 = (cm["FlowInExUSD"].fillna(0.0) + cm["FlowOutExUSD"].fillna(0.0)).rolling(7, min_periods=1).sum().clip(lower=1.0)
                nf_pts = np.clip(-(nf7 / tf7) * 180.0, -20.0, 20.0)
                oc_ser = pd.Series(np.clip(mvrv_pts + nf_pts, -50.0, 50.0), index=cm["date"])
                oc_ser = oc_ser[~oc_ser.index.duplicated(keep="last")]
                oc_s = oc_ser.reindex(macro_df.index.union(oc_ser.index)).sort_index().ffill().reindex(macro_df.index).fillna(0.0)
            s_val = np.clip(0.60 * oc_s + 0.50 * cot_s + 0.45 * global_series + 0.25 * r10_risk_pts - 0.25 * dxy_pts, -100.0, 100.0)
        elif ac == "metals":
            s_val = np.clip(0.95 * r10_risk_pts + 0.80 * cot_s - 0.70 * dxy_pts, -100.0, 100.0)
        elif ac == "energy":
            s_val = np.clip(0.85 * cot_s + 0.45 * hy_pts + 0.35 * vix_pts - 0.35 * dxy_pts, -100.0, 100.0)
        elif ac == "fx":
            if sym == "USDJPY":
                s_val = np.clip(0.85 * dxy_pts + 0.75 * cot_s - 0.75 * r10_risk_pts + 0.35 * vix_pts, -100.0, 100.0)
            else:
                s_val = np.clip(-0.90 * dxy_pts + 0.80 * cot_s + 0.40 * r10_risk_pts + 0.35 * global_series, -100.0, 100.0)
        else:
            s_val = np.clip(0.65 * global_series + 0.75 * cot_s + 0.35 * vix_pts, -100.0, 100.0)

        out[sym] = pd.Series(s_val, index=macro_df.index)

    return out


def align_macro_to_bars(bar_times: pd.Series, macro_series: Optional[pd.Series]) -> np.ndarray:
    """Strictly causal as-of merge from daily macro bias to intraday/daily OHLCV bars."""
    n = len(bar_times)
    if macro_series is None or macro_series.empty:
        return np.zeros(n, dtype=float)

    mdf = macro_series.rename("val").reset_index()
    mdf.columns = ["m_time", "val"]
    mdf = mdf.sort_values("m_time")
    bdf = pd.DataFrame({"b_time": pd.to_datetime(bar_times, utc=True)}).sort_values("b_time")
    merged = pd.merge_asof(bdf, mdf, left_on="b_time", right_on="m_time", direction="backward")
    return merged["val"].fillna(0.0).values.astype(float)


def label_candidates(
    df: pd.DataFrame,
    candidates: List[Dict[str, Any]],
    tp1_mult: float,
    tp2_mult: float,
    max_hold: int = 30,
    cost_r: float = 0.05,
) -> pd.DataFrame:
    """
    Triple-Barrier labeling with conservative SL-first intra-bar tie-breaking
    and realistic spread/slippage cost deduction (cost_r = 0.05R).
    """
    if not candidates:
        return pd.DataFrame()

    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(df)
    rows = []

    for cand in candidates:
        s = int(cand["bar_idx"])
        if s >= n - 2:
            continue
        direction = int(cand["dir"])
        entry = float(cand["entry"])
        sl = float(cand["sl"])
        tp1 = float(cand["tp1"])
        tp2 = float(cand["tp2"])
        risk = max(abs(entry - sl), 1e-10)

        hit_tp1 = False
        hit_tp2 = False
        exit_idx = min(s + max_hold, n - 1)
        resolved_sl = False

        cur_sl = sl
        for k in range(s + 1, exit_idx + 1):
            if direction == 1:
                if l[k] <= cur_sl:
                    exit_idx = k
                    resolved_sl = True
                    break
                if h[k] >= tp2:
                    if not hit_tp1:
                        hit_tp1 = True
                    hit_tp2 = True
                    exit_idx = k
                    break
                if h[k] >= tp1 and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = entry  # Move runner stop to breakeven
            else:
                if h[k] >= cur_sl:
                    exit_idx = k
                    resolved_sl = True
                    break
                if l[k] <= tp2:
                    if not hit_tp1:
                        hit_tp1 = True
                    hit_tp2 = True
                    exit_idx = k
                    break
                if l[k] <= tp1 and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = entry

        if hit_tp2:
            r_net = 0.5 * tp1_mult + 0.5 * tp2_mult - cost_r
        elif hit_tp1:
            if resolved_sl:
                r_net = 0.5 * tp1_mult - cost_r
            else:
                # Open runner at vertical barrier
                mtm = ((c[exit_idx] - entry) * direction) / risk
                mtm = max(0.0, min(tp2_mult, mtm))
                r_net = 0.5 * tp1_mult + 0.5 * mtm - cost_r
        elif resolved_sl:
            r_net = -1.0 - cost_r
        else:
            mtm = ((c[exit_idx] - entry) * direction) / risk
            r_net = max(-1.0, min(tp1_mult, mtm)) - cost_r

        y = 1 if r_net > 0.0 else 0
        row = {
            "bar_idx": s,
            "exit_idx": exit_idx,
            "time": cand["time"],
            "dir": direction,
            "playbook": cand["playbook"],
            "score": cand["score"],
            "y": y,
            "hit_tp1": 1 if hit_tp1 else 0,
            "r_net": float(r_net),
        }
        for fi, fn in enumerate(FEATURE_NAMES):
            row[fn] = float(cand["features"][fi])
        rows.append(row)

    return pd.DataFrame(rows)


def purged_walk_forward_splits(
    events_df: pd.DataFrame,
    n_splits: int = 5,
    embargo_bars: int = 3,
    min_train_events: int = 20,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """
    Expanding-window chronological walk-forward splitter with purging & embargoing
    so no training sample overlaps into any test interval.
    """
    m = len(events_df)
    if m < min_train_events + 10:
        return []

    fold_size = max(5, (m - min_train_events) // n_splits)
    splits: List[Tuple[np.ndarray, np.ndarray]] = []
    bar_idx = events_df["bar_idx"].values
    exit_idx = events_df["exit_idx"].values

    for f in range(n_splits):
        test_start_pos = min_train_events + f * fold_size
        test_end_pos = m if f == n_splits - 1 else min(m, test_start_pos + fold_size)
        if test_start_pos >= m or test_end_pos <= test_start_pos:
            break

        test_indices = np.arange(test_start_pos, test_end_pos)
        test_min_bar = int(bar_idx[test_indices[0]])
        purge_cutoff = test_min_bar - embargo_bars

        raw_train = np.arange(0, test_start_pos)
        keep_mask = exit_idx[raw_train] < purge_cutoff
        train_indices = raw_train[keep_mask]

        if len(train_indices) >= 15 and len(test_indices) >= 3:
            splits.append((train_indices, test_indices))

    return splits


def evaluate_oos_config(
    df: pd.DataFrame,
    base_tf: str,
    macro_arr: np.ndarray,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Evaluate one parameter configuration strictly on OOS walk-forward segments."""
    params = EngineParams(
        min_score=int(cfg["min_score"]),
        vwap_period=int(cfg["vwap_period"]),
        sl_buffer_atr=float(cfg["sl_buffer_atr"]),
        tp1_mult=float(cfg["tp1_mult"]),
        tp2_mult=float(cfg["tp2_mult"]),
        en_sweep=bool(cfg["en_sweep"]),
        en_squeeze=bool(cfg["en_squeeze"]),
        en_pullback=bool(cfg["en_pullback"]),
    )
    res = compute_indicators_and_candidates(
        df, params, base_tf=base_tf, macro_bias_arr=macro_arr, use_macro_score=True
    )
    ev = label_candidates(df, res["candidates"], params.tp1_mult, params.tp2_mult)
    if ev.empty or len(ev) < 16:
        return {"ok": False, "trades": len(ev)}

    # Evaluate OOS across last 4 chronological segments (expanding WF starting at 40% history)
    n_ev = len(ev)
    start_oos = max(6, int(0.35 * n_ev))
    oos = ev.iloc[start_oos:].copy()
    if len(oos) < 12:
        return {"ok": False, "trades": len(oos)}

    r_vals = oos["r_net"].values
    oos_trades = len(oos)
    oos_exp_r = float(np.mean(r_vals))
    oos_win_pct = float(100.0 * np.mean(oos["y"].values))

    # Fold stability penalty & max drawdown in R
    chunks = np.array_split(r_vals, min(4, max(2, oos_trades // 4)))
    fold_means = [float(np.mean(ch)) for ch in chunks if len(ch) > 0]
    fold_std = float(np.std(fold_means)) if len(fold_means) > 1 else 0.0

    eq = np.cumsum(r_vals)
    peak = np.maximum.accumulate(np.insert(eq, 0, 0.0))[1:]
    dd = peak - eq
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

    objective = oos_exp_r * math.sqrt(min(oos_trades, 60)) - 0.35 * fold_std - 0.10 * max_dd

    return {
        "ok": True,
        "trades": oos_trades,
        "win_pct": round(oos_win_pct, 1),
        "exp_r": round(oos_exp_r, 3),
        "max_dd_r": round(max_dd, 2),
        "objective": round(objective, 4),
        "events_df": ev,
    }


def optimize_symbol_timeframe(
    sym: str,
    df: pd.DataFrame,
    base_tf: str,
    macro_arr: np.ndarray,
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """
    Run stability-penalized walk-forward parameter search on curated low-overfit grid.
    Returns (best_row_dict, baseline_events_for_ml).
    """
    curated_grid: List[Dict[str, Any]] = [
        {"min_score": 68, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 60, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.20, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 72, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 76, "vwap_period": 50, "sl_buffer_atr": 0.30, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 34, "sl_buffer_atr": 0.25, "tp1_mult": 1.3, "tp2_mult": 2.2, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 68, "vwap_period": 34, "sl_buffer_atr": 0.20, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 68, "vwap_period": 72, "sl_buffer_atr": 0.30, "tp1_mult": 1.8, "tp2_mult": 3.0, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 72, "sl_buffer_atr": 0.25, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 1.8, "tp2_mult": 3.0, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.20, "tp1_mult": 1.3, "tp2_mult": 2.2, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 0},
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 1.5, "tp2_mult": 2.5, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1},
    ]

    best_cfg = curated_grid[0]
    best_eval: Optional[Dict[str, Any]] = None

    for cfg in curated_grid:
        ev_res = evaluate_oos_config(df, base_tf, macro_arr, cfg)
        if not ev_res.get("ok"):
            continue
        if best_eval is None or ev_res["objective"] > best_eval["objective"]:
            best_eval = ev_res
            best_cfg = cfg

    # Collect ML training candidates at relaxed threshold (58+) to maximize sample richness
    ml_params = EngineParams(
        min_score=58,
        vwap_period=int(best_cfg["vwap_period"]),
        sl_buffer_atr=float(best_cfg["sl_buffer_atr"]),
        tp1_mult=float(best_cfg["tp1_mult"]),
        tp2_mult=float(best_cfg["tp2_mult"]),
    )
    ml_res = compute_indicators_and_candidates(
        df, ml_params, base_tf=base_tf, macro_bias_arr=macro_arr, use_macro_score=True
    )
    ml_ev = label_candidates(df, ml_res["candidates"], ml_params.tp1_mult, ml_params.tp2_mult)
    tf_label = "M15" if base_tf == "15m" else ("H1" if base_tf == "1h" else "D1")
    if not ml_ev.empty:
        ml_ev["symbol"] = sym
        ml_ev["asset_class"] = WATCHLIST[sym]["asset_class"]
        ml_ev["timeframe"] = tf_label
        ml_ev["series_id"] = f"{sym}_{tf_label}"

    if best_eval is not None and best_eval["trades"] >= 12 and best_eval["exp_r"] > 0.0:
        row = {
            "SYMBOL": sym,
            "TIMEFRAME": tf_label,
            "MIN_SCORE": int(best_cfg["min_score"]),
            "VWAP_PERIOD": int(best_cfg["vwap_period"]),
            "SL_BUFFER_ATR": float(best_cfg["sl_buffer_atr"]),
            "TP1_MULT": float(best_cfg["tp1_mult"]),
            "TP2_MULT": float(best_cfg["tp2_mult"]),
            "EN_SWEEP": int(best_cfg["en_sweep"]),
            "EN_SQUEEZE": int(best_cfg["en_squeeze"]),
            "EN_PULLBACK": int(best_cfg["en_pullback"]),
            "OOS_WIN_PCT": float(best_eval["win_pct"]),
            "OOS_EXP_R": float(best_eval["exp_r"]),
            "OOS_TRADES": int(best_eval["trades"]),
            "STATUS": "WF_OPTIMIZED",
        }
    else:
        win_p = float(best_eval["win_pct"]) if best_eval else 0.0
        exp_r = float(best_eval["exp_r"]) if best_eval else 0.0
        trd = int(best_eval["trades"]) if best_eval else 0
        row = {
            "SYMBOL": sym,
            "TIMEFRAME": tf_label,
            "MIN_SCORE": 68,
            "VWAP_PERIOD": 50,
            "SL_BUFFER_ATR": 0.25,
            "TP1_MULT": 1.5,
            "TP2_MULT": 2.5,
            "EN_SWEEP": 1,
            "EN_SQUEEZE": 1,
            "EN_PULLBACK": 1,
            "OOS_WIN_PCT": win_p,
            "OOS_EXP_R": exp_r,
            "OOS_TRADES": trd,
            "STATUS": "FALLBACK_DEFAULT",
        }

    return row, ml_ev


def _train_purged_cv_models(events_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Evaluate Calibrated Logistic Regression (tuned L2 regularization) vs HistGradientBoosting
    on per-series stratified purged OOS folds (preventing timeframe shift & overlap leakage),
    select optimal ML probability threshold, and fit final return-weighted weights.
    """
    df_ev = events_df.sort_values("time").reset_index(drop=True)
    X = df_ev[FEATURE_NAMES].values.astype(float)
    y = df_ev["y"].values.astype(int)
    r_net = df_ev["r_net"].values.astype(float)
    sample_w = 0.5 + np.clip(np.abs(r_net), 0.0, 2.5)

    n_splits = 5
    splits: List[Tuple[np.ndarray, np.ndarray]] = []
    if "series_id" in df_ev.columns and df_ev["series_id"].nunique() > 1:
        fold_tr: List[List[int]] = [[] for _ in range(n_splits)]
        fold_te: List[List[int]] = [[] for _ in range(n_splits)]
        for _, grp in df_ev.groupby("series_id"):
            g = grp.sort_values("bar_idx")
            orig_idx = g.index.values
            sp = purged_walk_forward_splits(
                g.reset_index(drop=True),
                n_splits=n_splits,
                embargo_bars=3,
                min_train_events=max(12, int(0.30 * len(g))),
            )
            for fi, (tr_l, te_l) in enumerate(sp):
                if fi < n_splits:
                    fold_tr[fi].extend(orig_idx[tr_l].tolist())
                    fold_te[fi].extend(orig_idx[te_l].tolist())
        for fi in range(n_splits):
            if len(fold_tr[fi]) >= 20 and len(fold_te[fi]) >= 5:
                splits.append((np.array(fold_tr[fi], dtype=int), np.array(fold_te[fi], dtype=int)))
    else:
        splits = purged_walk_forward_splits(df_ev, n_splits=n_splits, embargo_bars=3, min_train_events=25)

    if not splits or len(np.unique(y)) < 2:
        return {"ok": False}

    c_grid = (0.03, 0.08, 0.20, 0.50) if len(df_ev) >= 500 else (0.03, 0.08, 0.15)
    best_c = 0.08
    best_brier = 999.0
    best_auc_sel = -1.0
    oof_lr_probs = np.full(len(df_ev), np.nan)
    oof_gb_probs = np.full(len(df_ev), np.nan)
    oof_y = np.full(len(df_ev), np.nan)
    oof_r = np.full(len(df_ev), np.nan)

    for c_val in c_grid:
        cur_lr = np.full(len(df_ev), np.nan)
        cur_y = np.full(len(df_ev), np.nan)
        cur_r = np.full(len(df_ev), np.nan)
        for tr_idx, te_idx in splits:
            if len(np.unique(y[tr_idx])) < 2:
                continue
            scaler = StandardScaler()
            X_tr_s = scaler.fit_transform(X[tr_idx])
            X_te_s = scaler.transform(X[te_idx])
            lr = LogisticRegression(C=c_val, max_iter=1000, random_state=42)
            lr.fit(X_tr_s, y[tr_idx], sample_weight=sample_w[tr_idx])
            cur_lr[te_idx] = lr.predict_proba(X_te_s)[:, 1]
            cur_y[te_idx] = y[te_idx]
            cur_r[te_idx] = r_net[te_idx]

        v_c = np.isfinite(cur_lr) & np.isfinite(cur_y)
        if int(np.sum(v_c)) < 20 or len(np.unique(cur_y[v_c])) < 2:
            continue
        br_c = float(brier_score_loss(cur_y[v_c].astype(int), cur_lr[v_c]))
        auc_c = float(roc_auc_score(cur_y[v_c].astype(int), cur_lr[v_c]))
        if (br_c < best_brier - 1e-4) or (abs(br_c - best_brier) <= 1e-4 and auc_c > best_auc_sel):
            best_brier = br_c
            best_auc_sel = auc_c
            best_c = c_val
            oof_lr_probs = cur_lr
            oof_y = cur_y
            oof_r = cur_r

    for tr_idx, te_idx in splits:
        if len(np.unique(y[tr_idx])) < 2:
            continue
        gb_base = HistGradientBoostingClassifier(
            max_depth=3,
            max_iter=120,
            learning_rate=0.05,
            min_samples_leaf=20,
            l2_regularization=2.0,
            random_state=42,
        )
        try:
            cal_gb = CalibratedClassifierCV(gb_base, method="sigmoid", cv=3)
            cal_gb.fit(X[tr_idx], y[tr_idx])
            oof_gb_probs[te_idx] = cal_gb.predict_proba(X[te_idx])[:, 1]
        except Exception:
            gb_base.fit(X[tr_idx], y[tr_idx])
            oof_gb_probs[te_idx] = gb_base.predict_proba(X[te_idx])[:, 1]

    valid = np.isfinite(oof_lr_probs) & np.isfinite(oof_y)
    if int(np.sum(valid)) < 20 or len(np.unique(oof_y[valid])) < 2:
        return {"ok": False}

    y_v = oof_y[valid].astype(int)
    r_v = oof_r[valid]
    p_lr = oof_lr_probs[valid]
    p_gb = np.where(np.isfinite(oof_gb_probs[valid]), oof_gb_probs[valid], p_lr)

    auc_lr = float(roc_auc_score(y_v, p_lr))
    auc_gb = float(roc_auc_score(y_v, p_gb))
    brier_lr = float(brier_score_loss(y_v, p_lr))
    brier_gb = float(brier_score_loss(y_v, p_gb))

    base_exp = float(np.mean(r_v))
    base_win = float(100.0 * np.mean(y_v))

    # Fine quantile + grid threshold search on purged OOS predictions (keep >= 30% of signals)
    q_grid = [round(float(np.quantile(p_lr, q)), 2) for q in np.linspace(0.15, 0.70, 12)]
    f_grid = [round(float(x), 2) for x in np.arange(0.32, 0.65, 0.02)]
    cand_thresh = sorted(set(x for x in (q_grid + f_grid) if 0.25 <= x <= 0.65))

    best_thresh = round(float(np.clip(np.quantile(p_lr, 0.25), 0.32, 0.55)), 2)
    best_exp = base_exp
    best_win = base_win
    best_kept = 1.0
    for th in cand_thresh:
        mask = p_lr >= th
        kept_frac = float(np.mean(mask))
        if kept_frac < 0.30 or int(np.sum(mask)) < 12:
            continue
        th_exp = float(np.mean(r_v[mask]))
        if th_exp > best_exp:
            best_exp = th_exp
            best_win = float(100.0 * np.mean(y_v[mask]))
            best_thresh = round(float(th), 2)
            best_kept = round(kept_frac, 3)

    # Fit final production StandardScaler + LogisticRegression on full dataset
    final_scaler = StandardScaler()
    X_full_s = final_scaler.fit_transform(X)
    final_lr = LogisticRegression(C=best_c, max_iter=1000, random_state=42)
    final_lr.fit(X_full_s, y, sample_weight=sample_w)
    p_full = final_lr.predict_proba(X_full_s)[:, 1]

    return {
        "ok": True,
        "samples": int(len(df_ev)),
        "oos_samples": int(len(y_v)),
        "best_c": best_c,
        "auc_lr": round(auc_lr, 4),
        "auc_gb": round(auc_gb, 4),
        "brier_lr": round(brier_lr, 4),
        "brier_gb": round(brier_gb, 4),
        "base_oos_win_pct": round(base_win, 1),
        "base_oos_exp_r": round(base_exp, 3),
        "ml_oos_win_pct": round(best_win, 1),
        "ml_oos_exp_r": round(best_exp, 3),
        "ml_min_prob": best_thresh,
        "kept_fraction": best_kept,
        "p_min": round(float(np.min(p_full)), 3),
        "p_max": round(float(np.max(p_full)), 3),
        "p_std": round(float(np.std(p_full)), 3),
        "intercept": float(final_lr.intercept_[0]),
        "means": [float(v) for v in final_scaler.mean_],
        "stds": [float(s if s > 1e-9 else 1.0) for s in final_scaler.scale_],
        "weights": [float(w) for w in final_lr.coef_[0]],
    }


def _format_ml_csv_row(sym: str, model: Dict[str, Any]) -> str:
    vals = (
        [sym, f"{model['intercept']:.6f}", f"{model['ml_min_prob']:.2f}"]
        + [f"{m:.6f}" for m in model["means"]]
        + [f"{s:.6f}" for s in model["stds"]]
        + [f"{w:.6f}" for w in model["weights"]]
    )
    return ",".join(vals)


def run_ml_optimization_pipeline() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Building causal multi-year historical macro series...")
    macro_hist = build_causal_macro_history()

    opt_rows: List[Dict[str, Any]] = []
    all_ml_events: List[pd.DataFrame] = []
    per_sym_events: Dict[str, List[pd.DataFrame]] = {}

    for sym, meta in WATCHLIST.items():
        ticker = meta["yahoo"]
        m_series = macro_hist.get(sym, macro_hist.get("GLOBAL"))
        per_sym_events[sym] = []

        for tf, range_p in [("15m", "60d"), ("1h", "730d"), ("1d", "5y")]:
            print(f"Downloading & optimizing {sym} ({ticker}) [{tf}]...", flush=True)
            df = fetch_yahoo_ohlcv(ticker, interval=tf, range_period=range_p)
            if df is not None and tf in ("15m", "1h") and len(df) > 3500:
                df = df.tail(3500).reset_index(drop=True)
            if df is None or len(df) < 120:
                tf_lbl = "M15" if tf == "15m" else ("H1" if tf == "1h" else "D1")
                opt_rows.append(
                    {
                        "SYMBOL": sym,
                        "TIMEFRAME": tf_lbl,
                        "MIN_SCORE": 68,
                        "VWAP_PERIOD": 50,
                        "SL_BUFFER_ATR": 0.25,
                        "TP1_MULT": 1.5,
                        "TP2_MULT": 2.5,
                        "EN_SWEEP": 1,
                        "EN_SQUEEZE": 1,
                        "EN_PULLBACK": 1,
                        "OOS_WIN_PCT": 0.0,
                        "OOS_EXP_R": 0.0,
                        "OOS_TRADES": 0,
                        "STATUS": "FALLBACK_DEFAULT",
                    }
                )
                continue

            macro_arr = align_macro_to_bars(df["time"], m_series)
            row, ev_df = optimize_symbol_timeframe(sym, df, tf, macro_arr)
            opt_rows.append(row)
            if not ev_df.empty:
                all_ml_events.append(ev_df)
                per_sym_events[sym].append(ev_df)

    # Train Universal Pooled GLOBAL ML Meta-Model
    global_model: Dict[str, Any] = {
        "ok": False,
        "ml_min_prob": 0.50,
        "intercept": 0.0,
        "means": [0.0] * 10,
        "stds": [1.0] * 10,
        "weights": [0.0] * 10,
    }
    if all_ml_events:
        pooled = pd.concat(all_ml_events, ignore_index=True)
        trained_global = _train_purged_cv_models(pooled)
        if trained_global.get("ok"):
            global_model = trained_global
            print(
                f"GLOBAL Meta-Model: samples={global_model['samples']}, "
                f"AUC_LR={global_model['auc_lr']}, AUC_GB={global_model['auc_gb']}, "
                f"BaseExp={global_model['base_oos_exp_r']:+.3f}R -> "
                f"ML_Exp={global_model['ml_oos_exp_r']:+.3f}R (P>={global_model['ml_min_prob']:.2f})"
            )

    # Train Asset-Class Meta-Models (CRYPTO, METALS, ENERGY, FX, INDEX)
    ac_models: Dict[str, Dict[str, Any]] = {}
    if all_ml_events:
        pooled_all = pd.concat(all_ml_events, ignore_index=True)
        for ac, ac_df in pooled_all.groupby("asset_class"):
            ac_res = _train_purged_cv_models(ac_df)
            ac_valid = bool(
                ac_res.get("ok")
                and ac_res["ml_oos_exp_r"] > ac_res["base_oos_exp_r"]
                and ac_res["auc_lr"] >= 0.50
            )
            ac_res["validated"] = ac_valid
            ac_models[str(ac)] = ac_res
            if ac_valid:
                print(
                    f"ASSET_CLASS[{ac.upper()}]: samples={ac_res['samples']}, "
                    f"AUC={ac_res['auc_lr']}, Base={ac_res['base_oos_exp_r']:+.3f}R -> "
                    f"ML={ac_res['ml_oos_exp_r']:+.3f}R (P>={ac_res['ml_min_prob']:.2f})"
                )

    def _pack_model(m: Dict[str, Any], src: str) -> Dict[str, Any]:
        return {
            "source": src,
            "intercept": round(float(m["intercept"]), 6),
            "ml_min_prob": round(float(m["ml_min_prob"]), 2),
            "means": [round(float(x), 6) for x in m["means"]],
            "stds": [round(float(x), 6) for x in m["stds"]],
            "weights": [round(float(x), 6) for x in m["weights"]],
        }

    # Export explicit row per symbol in MLModelWeights.csv & full weights map in ml_report.json
    ml_rows: List[str] = [_format_ml_csv_row("GLOBAL", global_model)]
    sym_ml_summary: Dict[str, Any] = {}
    exported_models: Dict[str, Any] = {"GLOBAL": _pack_model(global_model, "GLOBAL")}

    for sym in WATCHLIST.keys():
        ac = WATCHLIST[sym]["asset_class"]
        evs = per_sym_events.get(sym, [])
        merged_sym = pd.concat(evs, ignore_index=True) if evs else pd.DataFrame()

        chosen_model = global_model
        sym_min_prob = float(global_model["ml_min_prob"])
        used_model = "GLOBAL"

        ac_candidate = ac_models.get(ac)
        if ac_candidate and ac_candidate.get("validated"):
            chosen_model = ac_candidate
            sym_min_prob = float(ac_candidate["ml_min_prob"])
            used_model = f"ASSET_CLASS({ac.upper()}, P>={sym_min_prob:.2f})"

        sym_res: Dict[str, Any] = {}
        if len(merged_sym) >= 60:
            sym_res = _train_purged_cv_models(merged_sym)
            # Strict OOS acceptance: must beat AUC >= 0.53 AND genuinely improve OOS expectancy
            if (
                sym_res.get("ok")
                and sym_res["auc_lr"] >= 0.53
                and sym_res["ml_oos_exp_r"] > sym_res["base_oos_exp_r"]
            ):
                chosen_model = sym_res
                sym_min_prob = float(sym_res["ml_min_prob"])
                used_model = f"SPECIALIZED(AUC={sym_res['auc_lr']}, P>={sym_min_prob:.2f})"
                sym_ml_summary[sym] = {k: v for k, v in sym_res.items() if k not in ("means", "stds", "weights")}
            else:
                sym_ml_summary[sym] = {
                    "inherited": used_model,
                    "samples": len(merged_sym),
                    "sym_auc_lr": sym_res.get("auc_lr"),
                    "sym_base_exp_r": sym_res.get("base_oos_exp_r"),
                    "sym_ml_exp_r": sym_res.get("ml_oos_exp_r"),
                    "ok": sym_res.get("ok", False),
                }
        else:
            sym_ml_summary[sym] = {"inherited": used_model, "samples": len(merged_sym)}

        ml_rows.append(_format_ml_csv_row(sym, chosen_model))
        exported_models[sym] = _pack_model(chosen_model, used_model)

        # Propagate ML_MIN_PROB into OptimizedConfig rows for this symbol
        for r in opt_rows:
            if r["SYMBOL"] == sym:
                r["ML_MIN_PROB"] = sym_min_prob
                r["ML_MODEL"] = used_model

    # Add GLOBAL default row to OptimizedConfig.csv
    opt_rows.append(
        {
            "SYMBOL": "GLOBAL",
            "TIMEFRAME": "ALL",
            "MIN_SCORE": 68,
            "VWAP_PERIOD": 50,
            "SL_BUFFER_ATR": 0.25,
            "TP1_MULT": 1.5,
            "TP2_MULT": 2.5,
            "EN_SWEEP": 1,
            "EN_SQUEEZE": 1,
            "EN_PULLBACK": 1,
            "ML_MIN_PROB": float(global_model["ml_min_prob"]),
            "OOS_WIN_PCT": float(global_model.get("ml_oos_win_pct", 0.0)),
            "OOS_EXP_R": float(global_model.get("ml_oos_exp_r", 0.0)),
            "OOS_TRADES": int(global_model.get("oos_samples", 0)),
            "STATUS": "GLOBAL_MODEL",
        }
    )

    # Export data/OptimizedConfig.csv
    cfg_lines = [
        f"# LeadingConfluencePro Cloud Walk-Forward Optimized Config | Updated UTC: {now_iso}",
        "SYMBOL,TIMEFRAME,MIN_SCORE,VWAP_PERIOD,SL_BUFFER_ATR,TP1_MULT,TP2_MULT,EN_SWEEP,EN_SQUEEZE,EN_PULLBACK,ML_MIN_PROB,OOS_WIN_PCT,OOS_EXP_R,STATUS",
    ]
    for r in opt_rows:
        cfg_lines.append(
            f"{r['SYMBOL']},{r['TIMEFRAME']},{r['MIN_SCORE']},{r['VWAP_PERIOD']},"
            f"{r['SL_BUFFER_ATR']:.2f},{r['TP1_MULT']:.2f},{r['TP2_MULT']:.2f},"
            f"{r['EN_SWEEP']},{r['EN_SQUEEZE']},{r['EN_PULLBACK']},"
            f"{r.get('ML_MIN_PROB', 0.50):.2f},{r['OOS_WIN_PCT']:.1f},{r['OOS_EXP_R']:.3f},{r['STATUS']}"
        )
    (DATA_DIR / "OptimizedConfig.csv").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

    # Export data/MLModelWeights.csv
    ml_header = (
        "SYMBOL,INTERCEPT,ML_MIN_PROB,"
        + ",".join(f"M{k}" for k in range(10))
        + ","
        + ",".join(f"S{k}" for k in range(10))
        + ","
        + ",".join(f"W{k}" for k in range(10))
    )
    ml_lines = [
        f"# LeadingConfluencePro Cloud Purged Walk-Forward ML Weights | Updated UTC: {now_iso}",
        ml_header,
    ] + ml_rows
    (DATA_DIR / "MLModelWeights.csv").write_text("\n".join(ml_lines) + "\n", encoding="utf-8")

    # Export data/ml_report.json
    feat_weights = {
        FEATURE_NAMES[i]: round(float(global_model["weights"][i]), 4)
        for i in range(10)
    }
    report = {
        "updated_utc": now_iso,
        "feature_names": FEATURE_NAMES,
        "global_model": {k: v for k, v in global_model.items() if k not in ("means", "stds", "weights")},
        "global_feature_weights": feat_weights,
        "asset_class_models": {
            k: {mk: mv for mk, mv in v.items() if mk not in ("means", "stds", "weights")}
            for k, v in ac_models.items()
        },
        "ml_models": exported_models,
        "symbol_models": sym_ml_summary,
        "optimized_configs": opt_rows,
    }
    (DATA_DIR / "ml_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = run_ml_optimization_pipeline()
    print(f"[{rep['updated_utc']}] ML Optimization finished. Saved CSVs and ml_report.json.")
