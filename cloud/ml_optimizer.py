"""
Institutional Self-Evolving 1-Year Purged Walk-Forward Triple-Barrier ML & Parameter Optimizer (v3.5):
- Trains strictly on the rolling trailing 365 days of historical data (full H1 + 60d M15 + D1)
- Exponential recency decay (120d half-life) + elevated weighting on real graded live outcomes
- Protected-Runner asymmetric geometry (0.50R TP1 65% partial + -0.50R runner SL + 3.00R TP2)
- Strictly causal inner-fold threshold optimization targeting >= 70% OOS Win Rate with positive Exp(R)
- Champion-vs-Challenger OOS promotion gate & persistent ml_evolution_log.json
- Exports:
    data/OptimizedConfig.csv
    data/MLModelWeights.csv (57 cols: 18 standardized features)
    data/ml_report.json
    data/ml_evolution_log.json
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
from features import EngineParams, FEATURE_NAMES_18, compute_indicators_and_candidates

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

FEATURE_NAMES = FEATURE_NAMES_18

WINDOW_DAYS = 365
TIME_DECAY_HALF_LIFE_DAYS = 120.0
TP1_WEIGHT = 0.65
RUNNER_SL_R = -0.50
MAX_HOLD_BARS = 48
LIVE_FEEDBACK_WEIGHT = 2.5
TARGET_WIN_PCT = 70.0


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


def _to_utc_ts(val: Any) -> pd.Timestamp:
    ts = pd.Timestamp(val)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def label_candidates(
    df: pd.DataFrame,
    candidates: List[Dict[str, Any]],
    tp1_mult: float,
    tp2_mult: float,
    tp1_weight: float = TP1_WEIGHT,
    runner_sl_r: float = RUNNER_SL_R,
    max_hold: int = MAX_HOLD_BARS,
    cost_r: float = 0.05,
) -> pd.DataFrame:
    """
    Protected-Runner Triple-Barrier labeling with conservative SL-first intra-bar
    tie-breaking and realistic spread/slippage cost deduction (cost_r = 0.05R).
    On TP1 hit, closes tp1_weight (65%) and locks runner stop at runner_sl_r (-0.50R).
    """
    if not candidates:
        return pd.DataFrame()

    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(df)
    w2 = 1.0 - tp1_weight
    rows = []

    for cand in candidates:
        s = int(cand["bar_idx"])
        if s >= n - 2:
            continue
        direction = int(cand["dir"])
        entry = float(cand["entry"])
        sl = float(cand["sl"])
        risk = max(abs(entry - sl), 1e-10)
        tp1 = entry + direction * tp1_mult * risk
        tp2 = entry + direction * tp2_mult * risk

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
                    hit_tp1 = True
                    hit_tp2 = True
                    exit_idx = k
                    break
                if h[k] >= tp1 and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = entry + direction * runner_sl_r * risk
            else:
                if h[k] >= cur_sl:
                    exit_idx = k
                    resolved_sl = True
                    break
                if l[k] <= tp2:
                    hit_tp1 = True
                    hit_tp2 = True
                    exit_idx = k
                    break
                if l[k] <= tp1 and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = entry + direction * runner_sl_r * risk

        if hit_tp2:
            r_net = tp1_weight * tp1_mult + w2 * tp2_mult - cost_r
        elif hit_tp1:
            if resolved_sl:
                r_net = tp1_weight * tp1_mult + w2 * runner_sl_r - cost_r
            else:
                mtm = ((c[exit_idx] - entry) * direction) / risk
                mtm = max(runner_sl_r, min(tp2_mult, mtm))
                r_net = tp1_weight * tp1_mult + w2 * mtm - cost_r
        elif resolved_sl:
            r_net = -1.0 - cost_r
        else:
            mtm = ((c[exit_idx] - entry) * direction) / risk
            r_net = max(-1.0, min(tp1_mult, mtm)) - cost_r

        y = 1 if r_net > 0.0 else 0
        row = {
            "bar_idx": s,
            "exit_idx": exit_idx,
            "time": _to_utc_ts(cand["time"]),
            "dir": direction,
            "playbook": cand["playbook"],
            "score": cand["score"],
            "y": y,
            "hit_tp1": 1 if hit_tp1 else 0,
            "r_net": float(r_net),
            "is_live_feedback": 0,
        }
        feats = cand["features"]
        for fi, fn in enumerate(FEATURE_NAMES):
            row[fn] = float(feats[fi]) if fi < len(feats) else 0.0
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


def _score_segment(r_vals: np.ndarray) -> float:
    """Dual-objective stability-penalized expectancy + high-win-rate score."""
    n = len(r_vals)
    if n < 6:
        return -999.0
    exp_r = float(np.mean(r_vals))
    win_pct = 100.0 * float(np.mean(r_vals > 0.0))
    chunks = np.array_split(r_vals, min(4, max(2, n // 4)))
    fold_means = [float(np.mean(ch)) for ch in chunks if len(ch) > 0]
    fold_std = float(np.std(fold_means)) if len(fold_means) > 1 else 0.0
    eq = np.cumsum(r_vals)
    peak = np.maximum.accumulate(np.insert(eq, 0, 0.0))[1:]
    max_dd = float(np.max(peak - eq)) if len(eq) > 0 else 0.0
    win_bonus = (0.04 * (win_pct - 65.0)) if exp_r > 0.0 else 0.0
    return float(exp_r * math.sqrt(min(n, 60)) + win_bonus - 0.30 * fold_std - 0.08 * max_dd)


def optimize_symbol_timeframe(
    sym: str,
    df: pd.DataFrame,
    base_tf: str,
    macro_arr: np.ndarray,
    cutoff_1y: pd.Timestamp,
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """
    True 3-Fold Anchored Nested Walk-Forward Parameter Optimizer (v3.5 1-Year Protected-Runner):
    - Computes indicators on full df (with warmup buffer) and filters events strictly to time >= cutoff_1y
    - Each fold selects best config strictly on IS history (bar_idx < t_split)
    - Evaluates ONLY unseen future bars [t_split .. t_end) using that IS-chosen config
    - Assigns DEFENSIVE_FILTER when nested OOS expectancy <= 0
    """
    curated_grid: List[Dict[str, Any]] = [
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 62, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 68, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 72, "vwap_period": 50, "sl_buffer_atr": 0.30, "tp1_mult": 0.45, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 34, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 72, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 0.55, "tp2_mult": 3.00, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 68, "vwap_period": 50, "sl_buffer_atr": 0.30, "tp1_mult": 0.60, "tp2_mult": 2.80, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 65, "vwap_period": 50, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1},
        {"min_score": 68, "vwap_period": 72, "sl_buffer_atr": 0.30, "tp1_mult": 0.45, "tp2_mult": 3.00, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1},
    ]

    bar_times_utc = pd.to_datetime(df["time"], utc=True)
    in_1y_mask = bar_times_utc >= cutoff_1y
    b0 = int(np.argmax(in_1y_mask.values)) if bool(in_1y_mask.any()) else 0

    ind_cache: Dict[Tuple[int, int, float, int, int, int], List[Dict[str, Any]]] = {}
    ev_by_cfg: List[pd.DataFrame] = []
    for cfg in curated_grid:
        cache_key = (
            int(cfg["min_score"]),
            int(cfg["vwap_period"]),
            round(float(cfg["sl_buffer_atr"]), 2),
            int(cfg["en_sweep"]),
            int(cfg["en_squeeze"]),
            int(cfg["en_pullback"]),
        )
        if cache_key not in ind_cache:
            params = EngineParams(
                min_score=int(cfg["min_score"]),
                vwap_period=int(cfg["vwap_period"]),
                sl_buffer_atr=float(cfg["sl_buffer_atr"]),
                min_sl_atr=1.10,
                tp1_mult=0.50,
                tp2_mult=3.00,
                en_sweep=bool(cfg["en_sweep"]),
                en_squeeze=bool(cfg["en_squeeze"]),
                en_pullback=bool(cfg["en_pullback"]),
            )
            res = compute_indicators_and_candidates(
                df, params, base_tf=base_tf, macro_bias_arr=macro_arr, use_macro_score=True
            )
            ind_cache[cache_key] = res["candidates"]

        ev = label_candidates(
            df,
            ind_cache[cache_key],
            float(cfg["tp1_mult"]),
            float(cfg["tp2_mult"]),
        )
        if not ev.empty:
            ev = ev[ev["time"] >= cutoff_1y].reset_index(drop=True)
        ev_by_cfg.append(ev)

    n = len(df)
    span = max(10, n - b0)
    folds = [
        (b0 + int(0.45 * span), b0 + int(0.63 * span)),
        (b0 + int(0.63 * span), b0 + int(0.81 * span)),
        (b0 + int(0.81 * span), n),
    ]
    stitched_parts: List[pd.DataFrame] = []
    for t_split, t_end in folds:
        best_idx = 0
        best_is_obj = -999.0
        for idx, ev in enumerate(ev_by_cfg):
            if ev.empty:
                continue
            is_r = ev.loc[ev["bar_idx"] < t_split, "r_net"].values
            sc = _score_segment(is_r)
            if sc > best_is_obj:
                best_is_obj = sc
                best_idx = idx
        ev_sel = ev_by_cfg[best_idx]
        if not ev_sel.empty:
            te_df = ev_sel[(ev_sel["bar_idx"] >= t_split) & (ev_sel["bar_idx"] < t_end)]
            if not te_df.empty:
                stitched_parts.append(te_df)

    stitched_oos = pd.concat(stitched_parts, ignore_index=True) if stitched_parts else pd.DataFrame()
    oos_trades = int(len(stitched_oos))
    if oos_trades > 0:
        r_vals = stitched_oos["r_net"].values.astype(float)
        oos_exp_r = round(float(np.mean(r_vals)), 3)
        oos_win_pct = round(100.0 * float(np.mean(r_vals > 0.0)), 1)
        gross_p = float(np.sum(r_vals[r_vals > 0.0]))
        gross_l = float(abs(np.sum(r_vals[r_vals < 0.0])))
        oos_pf = round(min(9.99, gross_p / max(gross_l, 1e-6)), 2) if gross_p > 0.0 else 0.0
        eq = np.cumsum(r_vals)
        peak = np.maximum.accumulate(np.insert(eq, 0, 0.0))[1:]
        oos_max_dd = round(float(np.max(peak - eq)), 2)
    else:
        oos_exp_r = 0.0
        oos_win_pct = 0.0
        oos_pf = 0.0
        oos_max_dd = 0.0

    prod_cfg = curated_grid[0]
    best_full_obj = -999.0
    for idx, ev in enumerate(ev_by_cfg):
        if ev.empty:
            continue
        sc = _score_segment(ev["r_net"].values)
        if sc > best_full_obj:
            best_full_obj = sc
            prod_cfg = curated_grid[idx]

    # Collect standardized 18-feature ML candidates at relaxed threshold (58+) with uniform 0.50R/3.00R barriers
    ml_params = EngineParams(
        min_score=58,
        vwap_period=int(prod_cfg["vwap_period"]),
        sl_buffer_atr=float(prod_cfg["sl_buffer_atr"]),
        min_sl_atr=1.10,
        tp1_mult=0.50,
        tp2_mult=3.00,
    )
    ml_res = compute_indicators_and_candidates(
        df, ml_params, base_tf=base_tf, macro_bias_arr=macro_arr, use_macro_score=True
    )
    ml_ev = label_candidates(df, ml_res["candidates"], 0.50, 3.00)
    if not ml_ev.empty:
        ml_ev = ml_ev[ml_ev["time"] >= cutoff_1y].reset_index(drop=True)
    tf_label = "M15" if base_tf == "15m" else ("H1" if base_tf == "1h" else "D1")
    if not ml_ev.empty:
        ml_ev["symbol"] = sym
        ml_ev["asset_class"] = WATCHLIST[sym]["asset_class"]
        ml_ev["timeframe"] = tf_label
        ml_ev["series_id"] = f"{sym}_{tf_label}"

    if oos_trades >= 8 and oos_exp_r > 0.0:
        row = {
            "SYMBOL": sym,
            "TIMEFRAME": tf_label,
            "MIN_SCORE": int(prod_cfg["min_score"]),
            "VWAP_PERIOD": int(prod_cfg["vwap_period"]),
            "SL_BUFFER_ATR": float(prod_cfg["sl_buffer_atr"]),
            "TP1_MULT": float(prod_cfg["tp1_mult"]),
            "TP2_MULT": float(prod_cfg["tp2_mult"]),
            "EN_SWEEP": int(prod_cfg["en_sweep"]),
            "EN_SQUEEZE": int(prod_cfg["en_squeeze"]),
            "EN_PULLBACK": int(prod_cfg["en_pullback"]),
            "OOS_WIN_PCT": oos_win_pct,
            "OOS_EXP_R": oos_exp_r,
            "OOS_PF": oos_pf,
            "OOS_MAX_DD_R": oos_max_dd,
            "OOS_TRADES": oos_trades,
            "STATUS": "WF_OPTIMIZED",
        }
    else:
        row = {
            "SYMBOL": sym,
            "TIMEFRAME": tf_label,
            "MIN_SCORE": 76,
            "VWAP_PERIOD": 50,
            "SL_BUFFER_ATR": 0.25,
            "TP1_MULT": 0.50,
            "TP2_MULT": 3.00,
            "EN_SWEEP": 0,
            "EN_SQUEEZE": 1,
            "EN_PULLBACK": 1,
            "OOS_WIN_PCT": oos_win_pct,
            "OOS_EXP_R": oos_exp_r,
            "OOS_PF": oos_pf,
            "OOS_MAX_DD_R": oos_max_dd,
            "OOS_TRADES": oos_trades,
            "STATUS": "DEFENSIVE_FILTER",
        }

    return row, ml_ev


def _load_live_feedback_events(cutoff_1y: pd.Timestamp) -> pd.DataFrame:
    """
    Load resolved live signals from data/signal_outcomes_ledger.json within the 1-year window
    so the ML meta-model learns continuously from real realized market outcomes.
    """
    ledger_path = DATA_DIR / "signal_outcomes_ledger.json"
    if not ledger_path.exists():
        return pd.DataFrame()
    try:
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
    except Exception:
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        status = str(ent.get("status", "OPEN"))
        r_net = ent.get("r_net")
        feats = ent.get("features")
        if status == "OPEN" or r_net is None or not isinstance(feats, list) or len(feats) != len(FEATURE_NAMES):
            continue
        ts = _to_utc_ts(ent.get("bar_time_utc"))
        if ts < cutoff_1y:
            continue
        sym = str(ent.get("symbol", "")).upper()
        tf_lbl = str(ent.get("timeframe", "M15")).upper()
        ac = WATCHLIST.get(sym, {}).get("asset_class", str(ent.get("asset_class", "fx")))
        r_val = float(r_net)
        rows.append(
            {
                "bar_idx": 0,
                "exit_idx": 12,
                "time": ts,
                "dir": 1 if str(ent.get("direction", "BUY")).upper() == "BUY" else -1,
                "playbook": str(ent.get("playbook", "VWAPPull")),
                "score": int(ent.get("score", 65)),
                "y": 1 if r_val > 0.0 else 0,
                "hit_tp1": 1 if ent.get("hit_tp1") else 0,
                "r_net": r_val,
                "is_live_feedback": 1,
                "symbol": sym,
                "asset_class": ac,
                "timeframe": tf_lbl,
                "series_id": f"{sym}_{tf_lbl}",
                **{FEATURE_NAMES[fi]: float(feats[fi]) for fi in range(len(FEATURE_NAMES))},
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)


def _merge_live_feedback(base_df: pd.DataFrame, live_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge real graded live outcomes into historical training events:
    - Exact (symbol, timeframe, timestamp) matches replace simulated labels with real realized labels
    - Newer live events receive monotonic bar_idx after historical bars so purged WF stays causal
    """
    if live_df.empty:
        return base_df
    if base_df.empty:
        out = live_df.copy().sort_values("time").reset_index(drop=True)
        out["bar_idx"] = np.arange(len(out), dtype=int) * 20
        out["exit_idx"] = out["bar_idx"] + 12
        return out

    hist = base_df.copy()
    if "is_live_feedback" not in hist.columns:
        hist["is_live_feedback"] = 0
    hist["_k"] = (
        hist["symbol"].astype(str)
        + "|"
        + hist["timeframe"].astype(str)
        + "|"
        + pd.to_datetime(hist["time"], utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")
    )

    live = live_df.copy()
    live["_k"] = (
        live["symbol"].astype(str)
        + "|"
        + live["timeframe"].astype(str)
        + "|"
        + pd.to_datetime(live["time"], utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")
    )

    hist_idx_map = dict(zip(hist["_k"], zip(hist["bar_idx"], hist["exit_idx"])))
    max_bar_by_series = hist.groupby("series_id")["bar_idx"].max().to_dict()
    next_offset: Dict[str, int] = {}

    new_bars: List[int] = []
    new_exits: List[int] = []
    for _, r in live.iterrows():
        key = r["_k"]
        if key in hist_idx_map:
            b, ex = hist_idx_map[key]
        else:
            sid = str(r["series_id"])
            step = next_offset.get(sid, 0) + 1
            next_offset[sid] = step
            base_max = int(max_bar_by_series.get(sid, 1000))
            b = base_max + step * 20
            ex = b + 12
        new_bars.append(int(b))
        new_exits.append(int(ex))
    live["bar_idx"] = new_bars
    live["exit_idx"] = new_exits

    hist_filtered = hist[~hist["_k"].isin(set(live["_k"]))].drop(columns=["_k"])
    live_clean = live.drop(columns=["_k"])
    merged = pd.concat([hist_filtered, live_clean], ignore_index=True)
    return merged.sort_values("time").reset_index(drop=True)


def _train_purged_cv_models(events_df: pd.DataFrame, min_kept_count: int = 40) -> Dict[str, Any]:
    """
    Train & evaluate time-decayed Logistic Regression + HGB benchmark on 5-fold
    per-series purged walk-forward splits with strictly causal inner-fold quantile thresholding.
    """
    df_ev = events_df.sort_values("time").reset_index(drop=True)
    X = df_ev[FEATURE_NAMES].values.astype(float)
    y = df_ev["y"].values.astype(int)
    r_net = df_ev["r_net"].values.astype(float)

    times_utc = pd.to_datetime(df_ev["time"], utc=True)
    max_t = times_utc.max()
    age_days = np.maximum(0.0, (max_t - times_utc).dt.total_seconds().values / 86400.0)
    time_w = np.exp(-math.log(2.0) * age_days / TIME_DECAY_HALF_LIFE_DAYS)
    live_arr = (
        df_ev["is_live_feedback"].values.astype(float)
        if "is_live_feedback" in df_ev.columns
        else np.zeros(len(df_ev), dtype=float)
    )
    sample_w = time_w * np.where(live_arr == 1.0, LIVE_FEEDBACK_WEIGHT, 1.0)
    live_count = int(np.sum(live_arr == 1.0))

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

    c_grid = (0.02, 0.05, 0.12)
    best_c = 0.05
    best_brier = 999.0
    best_auc_sel = -1.0
    oof_lr_probs = np.full(len(df_ev), np.nan)
    oof_gb_probs = np.full(len(df_ev), np.nan)
    oof_y = np.full(len(df_ev), np.nan)
    oof_r = np.full(len(df_ev), np.nan)
    best_fold_preds: List[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []

    for c_val in c_grid:
        cur_lr = np.full(len(df_ev), np.nan)
        cur_y = np.full(len(df_ev), np.nan)
        cur_r = np.full(len(df_ev), np.nan)
        cur_fold_preds: List[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        for tr_idx, te_idx in splits:
            if len(np.unique(y[tr_idx])) < 2:
                continue
            scaler = StandardScaler()
            X_tr_s = scaler.fit_transform(X[tr_idx])
            X_te_s = scaler.transform(X[te_idx])
            lr = LogisticRegression(C=c_val, max_iter=1000, random_state=42)
            lr.fit(X_tr_s, y[tr_idx], sample_weight=sample_w[tr_idx])
            p_tr_f = lr.predict_proba(X_tr_s)[:, 1]
            p_te_f = lr.predict_proba(X_te_s)[:, 1]
            cur_lr[te_idx] = p_te_f
            cur_y[te_idx] = y[te_idx]
            cur_r[te_idx] = r_net[te_idx]
            cur_fold_preds.append((tr_idx, te_idx, p_tr_f, p_te_f))

        v_c = np.isfinite(cur_lr) & np.isfinite(cur_y)
        if int(np.sum(v_c)) < 20 or len(np.unique(cur_y[v_c])) < 2:
            continue
        br_c = float(brier_score_loss(cur_y[v_c].astype(int), cur_lr[v_c]))
        auc_c = float(roc_auc_score(cur_y[v_c].astype(int), cur_lr[v_c]))
        if (auc_c > best_auc_sel + 0.001) or (abs(auc_c - best_auc_sel) <= 0.001 and br_c < best_brier):
            best_brier = br_c
            best_auc_sel = auc_c
            best_c = c_val
            oof_lr_probs = cur_lr
            oof_y = cur_y
            oof_r = cur_r
            best_fold_preds = cur_fold_preds

    for tr_idx, te_idx in splits:
        if len(np.unique(y[tr_idx])) < 2:
            continue
        gb_base = HistGradientBoostingClassifier(
            max_depth=3,
            max_iter=90,
            learning_rate=0.04,
            min_samples_leaf=25,
            l2_regularization=4.0,
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
    base_gp = float(np.sum(r_v[r_v > 0.0]))
    base_gl = float(abs(np.sum(r_v[r_v < 0.0])))

    # Strictly causal inner-fold quantile thresholding (zero test-set leakage)
    q_grid = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.82, 0.85, 0.88]
    eff_min_kept = min(min_kept_count, max(12, int(0.12 * len(y_v))))

    tier1_best: Optional[Tuple[float, Dict[str, Any]]] = None
    tier2_best: Optional[Tuple[float, Dict[str, Any]]] = None

    for q in q_grid:
        causal_mask = np.zeros(len(df_ev), dtype=bool)
        pos_folds = 0
        eval_folds = 0
        for _, te_idx, p_tr_f, p_te_f in best_fold_preds:
            th_f = float(np.quantile(p_tr_f, q))
            m_f = p_te_f >= th_f
            causal_mask[te_idx] = m_f
            if int(np.sum(m_f)) >= 5:
                eval_folds += 1
                if float(np.mean(r_net[te_idx][m_f])) > 0.0:
                    pos_folds += 1

        fold_keep = causal_mask[valid]
        n_kept = int(np.sum(fold_keep))
        if n_kept < eff_min_kept:
            continue

        rm = r_v[fold_keep]
        ym = y_v[fold_keep]
        win_q = 100.0 * float(np.mean(ym))
        exp_q = float(np.mean(rm))
        gp_q = float(np.sum(rm[rm > 0.0]))
        gl_q = float(abs(np.sum(rm[rm < 0.0])))
        pf_q = min(9.99, gp_q / max(gl_q, 1e-6)) if gp_q > 0.0 else 0.0
        kept_frac = n_kept / float(len(y_v))

        cand_info = {
            "q": q,
            "win_pct": win_q,
            "exp_r": exp_q,
            "pf": pf_q,
            "n_kept": n_kept,
            "kept_frac": kept_frac,
            "pos_folds": pos_folds,
            "eval_folds": eval_folds,
        }
        if win_q >= TARGET_WIN_PCT and exp_q > 0.0:
            obj1 = exp_q * math.sqrt(min(n_kept, 250)) + 0.02 * pos_folds
            if tier1_best is None or obj1 > tier1_best[0]:
                tier1_best = (obj1, cand_info)
        elif exp_q > base_exp:
            obj2 = exp_q * math.sqrt(min(n_kept, 250)) + 0.005 * win_q
            if tier2_best is None or obj2 > tier2_best[0]:
                tier2_best = (obj2, cand_info)

    chosen = tier1_best[1] if tier1_best is not None else (tier2_best[1] if tier2_best is not None else None)
    if chosen is not None:
        best_q = float(chosen["q"])
        best_win = float(chosen["win_pct"])
        best_exp = float(chosen["exp_r"])
        best_pf = float(chosen["pf"])
        best_kept_cnt = int(chosen["n_kept"])
        best_kept = float(chosen["kept_frac"])
        best_pos_folds = int(chosen["pos_folds"])
        best_eval_folds = int(chosen["eval_folds"])
    else:
        best_q = 0.20
        best_win = base_win
        best_exp = base_exp
        best_pf = min(9.99, base_gp / max(base_gl, 1e-6)) if base_gp > 0.0 else 0.0
        best_kept_cnt = int(len(y_v))
        best_kept = 1.0
        best_pos_folds = 0
        best_eval_folds = len(best_fold_preds)

    # Fit final production StandardScaler + LogisticRegression on full 1-year (+ live feedback) pool
    final_scaler = StandardScaler()
    X_full_s = final_scaler.fit_transform(X)
    final_lr = LogisticRegression(C=best_c, max_iter=1000, random_state=42)
    final_lr.fit(X_full_s, y, sample_weight=sample_w)
    p_full = final_lr.predict_proba(X_full_s)[:, 1]
    best_thresh = round(float(np.clip(np.quantile(p_full, best_q), 0.35, 0.90)), 3)

    return {
        "ok": True,
        "samples": int(len(df_ev)),
        "oos_samples": int(len(y_v)),
        "live_feedback_used": live_count,
        "best_c": best_c,
        "selected_quantile": best_q,
        "auc_lr": round(auc_lr, 4),
        "auc_gb": round(auc_gb, 4),
        "brier_lr": round(brier_lr, 4),
        "brier_gb": round(brier_gb, 4),
        "base_oos_win_pct": round(base_win, 1),
        "base_oos_exp_r": round(base_exp, 3),
        "ml_oos_win_pct": round(best_win, 1),
        "ml_oos_exp_r": round(best_exp, 4),
        "ml_oos_pf": round(best_pf, 2),
        "ml_oos_trades": best_kept_cnt,
        "ml_pos_folds": best_pos_folds,
        "ml_eval_folds": best_eval_folds,
        "ml_min_prob": best_thresh,
        "kept_fraction": round(best_kept, 3),
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
        [sym, f"{model['intercept']:.6f}", f"{model['ml_min_prob']:.3f}"]
        + [f"{m:.6f}" for m in model["means"]]
        + [f"{s:.6f}" for s in model["stds"]]
        + [f"{w:.6f}" for w in model["weights"]]
    )
    return ",".join(vals)


def _check_live_drift() -> Dict[str, Any]:
    ledger_path = DATA_DIR / "signal_outcomes_ledger.json"
    if not ledger_path.exists():
        return {"graded_recent": 0, "win_pct": None, "exp_r": None, "drift_triggered": False}
    try:
        entries = json.loads(ledger_path.read_text(encoding="utf-8")).get("entries", [])
    except Exception:
        return {"graded_recent": 0, "win_pct": None, "exp_r": None, "drift_triggered": False}

    graded = [e for e in entries if e.get("status") != "OPEN" and e.get("r_net") is not None]
    graded.sort(key=lambda e: str(e.get("resolved_utc") or e.get("bar_time_utc") or ""))
    recent = graded[-20:]
    if not recent:
        return {"graded_recent": 0, "win_pct": None, "exp_r": None, "drift_triggered": False}
    r_vals = [float(e["r_net"]) for e in recent]
    win_pct = round(100.0 * float(np.mean([rv > 0.0 for rv in r_vals])), 1)
    exp_r = round(float(np.mean(r_vals)), 3)
    drift = bool(len(recent) >= 10 and (win_pct < 60.0 or exp_r < -0.05))
    return {"graded_recent": len(recent), "win_pct": win_pct, "exp_r": exp_r, "drift_triggered": drift}


def run_ml_optimization_pipeline() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_dt = pd.Timestamp(datetime.now(timezone.utc))
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_1y = now_dt - pd.Timedelta(days=WINDOW_DAYS)

    print(f"Building 1-year causal historical macro series (cutoff={cutoff_1y.strftime('%Y-%m-%d')})...")
    macro_hist = build_causal_macro_history()
    live_feedback_df = _load_live_feedback_events(cutoff_1y)
    if not live_feedback_df.empty:
        print(f"Loaded {len(live_feedback_df)} graded live outcomes from ledger for feedback training.")

    opt_rows: List[Dict[str, Any]] = []
    all_ml_events: List[pd.DataFrame] = []
    per_sym_events: Dict[str, List[pd.DataFrame]] = {}

    for sym, meta in WATCHLIST.items():
        ticker = meta["yahoo"]
        m_series = macro_hist.get(sym, macro_hist.get("GLOBAL"))
        per_sym_events[sym] = []

        for tf, range_p, buf_days in [("15m", "60d", 0), ("1h", "730d", 15), ("1d", "2y", 140)]:
            print(f"Downloading & optimizing {sym} ({ticker}) [{tf}]...", flush=True)
            df = fetch_yahoo_ohlcv(ticker, interval=tf, range_period=range_p)
            if df is not None and not df.empty:
                df_times = pd.to_datetime(df["time"], utc=True)
                min_keep = cutoff_1y - pd.Timedelta(days=buf_days)
                df = df[df_times >= min_keep].reset_index(drop=True)
            if df is None or len(df) < 120:
                tf_lbl = "M15" if tf == "15m" else ("H1" if tf == "1h" else "D1")
                opt_rows.append(
                    {
                        "SYMBOL": sym,
                        "TIMEFRAME": tf_lbl,
                        "MIN_SCORE": 76,
                        "VWAP_PERIOD": 50,
                        "SL_BUFFER_ATR": 0.25,
                        "TP1_MULT": 0.50,
                        "TP2_MULT": 3.00,
                        "EN_SWEEP": 0,
                        "EN_SQUEEZE": 1,
                        "EN_PULLBACK": 1,
                        "OOS_WIN_PCT": 0.0,
                        "OOS_EXP_R": 0.0,
                        "OOS_PF": 0.0,
                        "OOS_MAX_DD_R": 0.0,
                        "OOS_TRADES": 0,
                        "STATUS": "DEFENSIVE_FILTER",
                    }
                )
                continue

            macro_arr = align_macro_to_bars(df["time"], m_series)
            row, ev_df = optimize_symbol_timeframe(sym, df, tf, macro_arr, cutoff_1y)
            opt_rows.append(row)
            if not ev_df.empty:
                all_ml_events.append(ev_df)
                per_sym_events[sym].append(ev_df)

    # Train Universal Pooled GLOBAL ML Meta-Model on 1-year Intraday (M15+H1) + Live Graded Outcomes
    n_feats = len(FEATURE_NAMES)
    global_model: Dict[str, Any] = {
        "ok": False,
        "ml_min_prob": 0.65,
        "intercept": 0.0,
        "means": [0.0] * n_feats,
        "stds": [1.0] * n_feats,
        "weights": [0.0] * n_feats,
    }
    pooled_all_raw = pd.concat(all_ml_events, ignore_index=True) if all_ml_events else pd.DataFrame()
    intraday_ev = pooled_all_raw[pooled_all_raw["timeframe"].isin(["M15", "H1"])].copy() if not pooled_all_raw.empty else pd.DataFrame()
    live_intra = live_feedback_df[live_feedback_df["timeframe"].isin(["M15", "H1"])].copy() if not live_feedback_df.empty else pd.DataFrame()
    train_pool = _merge_live_feedback(intraday_ev if len(intraday_ev) >= 100 else pooled_all_raw, live_intra)

    if not train_pool.empty:
        trained_global = _train_purged_cv_models(train_pool, min_kept_count=40)
        if trained_global.get("ok"):
            global_model = trained_global
            print(
                f"GLOBAL Meta-Model (1Y Intraday): samples={global_model['samples']} "
                f"(live={global_model['live_feedback_used']}), "
                f"AUC_LR={global_model['auc_lr']}, AUC_GB={global_model['auc_gb']}, "
                f"Base={global_model['base_oos_win_pct']}%/{global_model['base_oos_exp_r']:+.3f}R -> "
                f"ML={global_model['ml_oos_win_pct']}%/{global_model['ml_oos_exp_r']:+.4f}R "
                f"PF={global_model['ml_oos_pf']} (n={global_model['ml_oos_trades']}, "
                f"Folds={global_model['ml_pos_folds']}/{global_model['ml_eval_folds']}, "
                f"P>={global_model['ml_min_prob']:.3f})",
                flush=True,
            )

    # Train Asset-Class Meta-Models (CRYPTO, METALS, ENERGY, FX, INDEX) on 1-year intraday events
    ac_models: Dict[str, Dict[str, Any]] = {}
    if not pooled_all_raw.empty:
        for ac, ac_full_df in pooled_all_raw.groupby("asset_class"):
            ac_intra = ac_full_df[ac_full_df["timeframe"].isin(["M15", "H1"])]
            live_ac = live_intra[live_intra["asset_class"] == ac] if not live_intra.empty else pd.DataFrame()
            ac_df = _merge_live_feedback(ac_intra if len(ac_intra) >= 100 else ac_full_df, live_ac)
            ac_res = _train_purged_cv_models(ac_df, min_kept_count=20)
            ac_valid = bool(
                ac_res.get("ok")
                and ac_res["ml_oos_exp_r"] > ac_res["base_oos_exp_r"]
                and ac_res["ml_oos_exp_r"] > 0.0
                and ac_res["auc_lr"] >= 0.505
            )
            ac_res["validated"] = ac_valid
            ac_models[str(ac)] = ac_res
            if ac_valid:
                print(
                    f"ASSET_CLASS[{ac.upper()}]: samples={ac_res['samples']}, "
                    f"AUC={ac_res['auc_lr']}, ML={ac_res['ml_oos_win_pct']}%/{ac_res['ml_oos_exp_r']:+.4f}R "
                    f"(P>={ac_res['ml_min_prob']:.3f})",
                    flush=True,
                )

    def _pack_model(m: Dict[str, Any], src: str) -> Dict[str, Any]:
        return {
            "source": src,
            "intercept": round(float(m["intercept"]), 6),
            "ml_min_prob": round(float(m["ml_min_prob"]), 3),
            "means": [round(float(x), 6) for x in m["means"]],
            "stds": [round(float(x), 6) for x in m["stds"]],
            "weights": [round(float(x), 6) for x in m["weights"]],
        }

    # Build Challenger models per symbol
    challenger_ml_map: Dict[str, Dict[str, Any]] = {"GLOBAL": global_model}
    challenger_sources: Dict[str, str] = {"GLOBAL": "GLOBAL"}
    sym_ml_summary: Dict[str, Any] = {}
    exported_models: Dict[str, Any] = {"GLOBAL": _pack_model(global_model, "GLOBAL")}

    for sym in WATCHLIST.keys():
        ac = WATCHLIST[sym]["asset_class"]
        evs = per_sym_events.get(sym, [])
        merged_sym = pd.concat(evs, ignore_index=True) if evs else pd.DataFrame()
        sym_intra = merged_sym[merged_sym["timeframe"].isin(["M15", "H1"])] if not merged_sym.empty else pd.DataFrame()
        live_sym = live_intra[live_intra["symbol"] == sym] if not live_intra.empty else pd.DataFrame()
        sym_train = _merge_live_feedback(sym_intra if len(sym_intra) >= 75 else merged_sym, live_sym)

        chosen_model = global_model
        sym_min_prob = float(global_model["ml_min_prob"])
        used_model = "GLOBAL"

        ac_candidate = ac_models.get(ac)
        if ac_candidate and ac_candidate.get("validated"):
            chosen_model = ac_candidate
            sym_min_prob = float(ac_candidate["ml_min_prob"])
            used_model = f"ASSET_CLASS({ac.upper()}, P>={sym_min_prob:.3f})"

        sym_res: Dict[str, Any] = {}
        if len(sym_train) >= 70:
            sym_res = _train_purged_cv_models(sym_train, min_kept_count=15)
            if (
                sym_res.get("ok")
                and sym_res["auc_lr"] >= 0.53
                and sym_res["ml_oos_exp_r"] > sym_res["base_oos_exp_r"]
                and sym_res["ml_oos_exp_r"] > 0.0
            ):
                chosen_model = sym_res
                sym_min_prob = float(sym_res["ml_min_prob"])
                used_model = f"SPECIALIZED(AUC={sym_res['auc_lr']}, P>={sym_min_prob:.3f})"
                sym_ml_summary[sym] = {k: v for k, v in sym_res.items() if k not in ("means", "stds", "weights")}
            else:
                sym_ml_summary[sym] = {
                    "inherited": used_model,
                    "samples": len(sym_train),
                    "sym_auc_lr": sym_res.get("auc_lr"),
                    "sym_base_exp_r": sym_res.get("base_oos_exp_r"),
                    "sym_ml_exp_r": sym_res.get("ml_oos_exp_r"),
                    "ok": sym_res.get("ok", False),
                }
        else:
            sym_ml_summary[sym] = {"inherited": used_model, "samples": len(sym_train)}

        challenger_ml_map[sym] = chosen_model
        challenger_sources[sym] = used_model
        exported_models[sym] = _pack_model(chosen_model, used_model)

        for r in opt_rows:
            if r["SYMBOL"] == sym:
                r["ML_MIN_PROB"] = sym_min_prob
                r["ML_MODEL"] = used_model

    # Champion-vs-Challenger Auto-Evolution Gate
    prev_report: Optional[Dict[str, Any]] = None
    report_path = DATA_DIR / "ml_report.json"
    if report_path.exists():
        try:
            prev_report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            prev_report = None

    drift_info = _check_live_drift()
    prev_is_v35 = bool(prev_report and len(prev_report.get("feature_names", [])) == n_feats)
    new_win = float(global_model.get("ml_oos_win_pct", 0.0))
    new_exp = float(global_model.get("ml_oos_exp_r", 0.0))
    new_cnt = int(global_model.get("ml_oos_trades", 0))
    new_score = new_exp * math.sqrt(max(1, new_cnt)) + 0.01 * (new_win - 65.0)

    promoted = True
    evolution_status = "PROMOTED_V35_UPGRADE"
    champ_exp = None
    champ_win = None
    champ_age_days = None

    if prev_is_v35 and prev_report is not None:
        pg = prev_report.get("global_model", {})
        champ_exp = float(pg.get("ml_oos_exp_r", 0.0))
        champ_win = float(pg.get("ml_oos_win_pct", 0.0))
        champ_cnt = int(pg.get("ml_oos_trades", 1))
        champ_score = champ_exp * math.sqrt(max(1, champ_cnt)) + 0.01 * (champ_win - 65.0)
        try:
            champ_age_days = round((now_dt - _to_utc_ts(prev_report.get("updated_utc"))).total_seconds() / 86400.0, 2)
        except Exception:
            champ_age_days = 0.0

        if not global_model.get("ok") or (champ_exp > 0.0 and new_exp <= 0.0):
            promoted = False
            evolution_status = "KEPT_CHAMPION_GUARD"
        elif drift_info["drift_triggered"]:
            promoted = True
            evolution_status = "PROMOTED_DRIFT_RETRAIN"
        elif champ_age_days >= 7.0:
            promoted = True
            evolution_status = "PROMOTED_SCHEDULED_REFRESH"
        elif new_score >= champ_score - 0.15:
            promoted = True
            evolution_status = "PROMOTED_IMPROVED_OOS"
        else:
            promoted = False
            evolution_status = "KEPT_CHAMPION_GUARD"

    # Respect Champion guard: if kept Champion, preserve prior 18-feature model weights
    if not promoted and prev_is_v35 and prev_report is not None and isinstance(prev_report.get("ml_models"), dict):
        kept_models = prev_report["ml_models"]
        ml_rows = []
        for k_sym in ["GLOBAL"] + list(WATCHLIST.keys()):
            km = kept_models.get(k_sym, exported_models.get(k_sym))
            if km and len(km.get("weights", [])) == n_feats:
                ml_rows.append(_format_ml_csv_row(k_sym, km))
                exported_models[k_sym] = km
                for r in opt_rows:
                    if r["SYMBOL"] == k_sym:
                        r["ML_MIN_PROB"] = float(km["ml_min_prob"])
    else:
        ml_rows = [_format_ml_csv_row("GLOBAL", global_model)] + [
            _format_ml_csv_row(s, challenger_ml_map[s]) for s in WATCHLIST.keys()
        ]

    # Compute Walk-Forward Portfolio Audit Summary & Per-Playbook Breakdown
    intra_cfgs = [r for r in opt_rows if r["TIMEFRAME"] in ("M15", "H1") and r.get("OOS_TRADES", 0) > 0]
    wf_opt_cnt = sum(1 for r in intra_cfgs if r["STATUS"] == "WF_OPTIMIZED")
    def_cnt = sum(1 for r in intra_cfgs if r["STATUS"] == "DEFENSIVE_FILTER")
    tot_intra_trades = sum(int(r.get("OOS_TRADES", 0)) for r in intra_cfgs)
    avg_intra_win = round(float(np.mean([r["OOS_WIN_PCT"] for r in intra_cfgs])), 1) if intra_cfgs else 0.0
    avg_intra_exp = round(float(np.mean([r["OOS_EXP_R"] for r in intra_cfgs])), 3) if intra_cfgs else 0.0
    avg_intra_pf = round(float(np.mean([r.get("OOS_PF", 1.0) for r in intra_cfgs])), 2) if intra_cfgs else 0.0

    wf_summary = {
        "v35_protected_runner": True,
        "training_window_days": WINDOW_DAYS,
        "intraday_configs_total": len(intra_cfgs),
        "wf_optimized_count": wf_opt_cnt,
        "defensive_filter_count": def_cnt,
        "intraday_oos_trades": tot_intra_trades,
        "intraday_avg_win_pct": avg_intra_win,
        "intraday_avg_exp_r": avg_intra_exp,
        "intraday_avg_pf": avg_intra_pf,
    }

    playbook_stats: Dict[str, Any] = {}
    if not intraday_ev.empty:
        for pb, p_df in intraday_ev.groupby("playbook"):
            rv = p_df["r_net"].values.astype(float)
            gp = float(np.sum(rv[rv > 0]))
            gl = float(abs(np.sum(rv[rv < 0])))
            playbook_stats[str(pb)] = {
                "trades": int(len(rv)),
                "win_pct": round(100.0 * float(np.mean(rv > 0)), 1),
                "exp_r": round(float(np.mean(rv)), 3),
                "pf": round(min(9.99, gp / max(gl, 1e-6)), 2) if gp > 0 else 0.0,
            }

    # Add GLOBAL default row to OptimizedConfig.csv
    global_thresh = float(exported_models.get("GLOBAL", global_model).get("ml_min_prob", 0.65))
    opt_rows.append(
        {
            "SYMBOL": "GLOBAL",
            "TIMEFRAME": "ALL",
            "MIN_SCORE": 65,
            "VWAP_PERIOD": 50,
            "SL_BUFFER_ATR": 0.25,
            "TP1_MULT": 0.50,
            "TP2_MULT": 3.00,
            "EN_SWEEP": 1,
            "EN_SQUEEZE": 1,
            "EN_PULLBACK": 1,
            "ML_MIN_PROB": global_thresh,
            "OOS_WIN_PCT": avg_intra_win,
            "OOS_EXP_R": avg_intra_exp,
            "OOS_PF": avg_intra_pf,
            "OOS_MAX_DD_R": 0.0,
            "OOS_TRADES": int(global_model.get("oos_samples", 0)),
            "STATUS": "GLOBAL_MODEL",
        }
    )

    # Export data/OptimizedConfig.csv
    cfg_lines = [
        f"# LeadingConfluencePro v3.5 1-Year Self-Evolving Walk-Forward Config | Updated UTC: {now_iso}",
        "SYMBOL,TIMEFRAME,MIN_SCORE,VWAP_PERIOD,SL_BUFFER_ATR,TP1_MULT,TP2_MULT,EN_SWEEP,EN_SQUEEZE,EN_PULLBACK,ML_MIN_PROB,OOS_WIN_PCT,OOS_EXP_R,STATUS",
    ]
    for r in opt_rows:
        cfg_lines.append(
            f"{r['SYMBOL']},{r['TIMEFRAME']},{r['MIN_SCORE']},{r['VWAP_PERIOD']},"
            f"{r['SL_BUFFER_ATR']:.2f},{r['TP1_MULT']:.2f},{r['TP2_MULT']:.2f},"
            f"{r['EN_SWEEP']},{r['EN_SQUEEZE']},{r['EN_PULLBACK']},"
            f"{r.get('ML_MIN_PROB', global_thresh):.3f},{r['OOS_WIN_PCT']:.1f},{r['OOS_EXP_R']:.3f},{r['STATUS']}"
        )
    (DATA_DIR / "OptimizedConfig.csv").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

    # Export data/MLModelWeights.csv (57 columns: 18 features)
    ml_header = (
        "SYMBOL,INTERCEPT,ML_MIN_PROB,"
        + ",".join(f"M{k}" for k in range(n_feats))
        + ","
        + ",".join(f"S{k}" for k in range(n_feats))
        + ","
        + ",".join(f"W{k}" for k in range(n_feats))
    )
    ml_lines = [
        f"# LeadingConfluencePro v3.5 1-Year Self-Evolving Purged Walk-Forward ML Weights | Updated UTC: {now_iso}",
        ml_header,
    ] + ml_rows
    (DATA_DIR / "MLModelWeights.csv").write_text("\n".join(ml_lines) + "\n", encoding="utf-8")

    evolution_decision = {
        "status": evolution_status,
        "promoted": promoted,
        "challenger_oos_win_pct": new_win,
        "challenger_oos_exp_r": new_exp,
        "challenger_oos_pf": global_model.get("ml_oos_pf", 0.0),
        "champion_prev_win_pct": champ_win,
        "champion_prev_exp_r": champ_exp,
        "champion_age_days": champ_age_days,
        "drift_check": drift_info,
    }

    # Export data/ml_report.json
    feat_weights = {
        FEATURE_NAMES[i]: round(float(global_model["weights"][i]), 4)
        for i in range(n_feats)
    }
    report = {
        "version": "3.5",
        "updated_utc": now_iso,
        "training_window_days": WINDOW_DAYS,
        "time_decay_half_life_days": TIME_DECAY_HALF_LIFE_DAYS,
        "live_feedback_count": len(live_feedback_df),
        "trade_geometry": {
            "tp1_mult": 0.50,
            "tp2_mult": 3.00,
            "tp1_weight": TP1_WEIGHT,
            "runner_sl_r": RUNNER_SL_R,
            "max_hold": MAX_HOLD_BARS,
        },
        "evolution_decision": evolution_decision,
        "feature_names": FEATURE_NAMES,
        "wf_portfolio_summary": wf_summary,
        "playbook_oos_stats": playbook_stats,
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
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Append to data/ml_evolution_log.json (capped at 180 entries)
    evo_path = DATA_DIR / "ml_evolution_log.json"
    evo_entries: List[Dict[str, Any]] = []
    if evo_path.exists():
        try:
            loaded = json.loads(evo_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("history"), list):
                evo_entries = loaded["history"]
            elif isinstance(loaded, list):
                evo_entries = loaded
        except Exception:
            evo_entries = []

    evo_entries.append(
        {
            "updated_utc": now_iso,
            "window_days": WINDOW_DAYS,
            "samples_1y": int(global_model.get("samples", 0)),
            "live_feedback_used": int(global_model.get("live_feedback_used", 0)),
            "auc_lr": global_model.get("auc_lr"),
            "base_oos_win_pct": global_model.get("base_oos_win_pct"),
            "ml_oos_win_pct": new_win,
            "ml_oos_exp_r": new_exp,
            "ml_oos_pf": global_model.get("ml_oos_pf"),
            "ml_oos_trades": new_cnt,
            "ml_pos_folds": global_model.get("ml_pos_folds"),
            "ml_eval_folds": global_model.get("ml_eval_folds"),
            "ml_min_prob": global_thresh,
            "decision": evolution_status,
            "promoted": promoted,
            "drift_triggered": drift_info["drift_triggered"],
        }
    )
    evo_entries = evo_entries[-180:]
    evo_path.write_text(
        json.dumps({"updated_utc": now_iso, "history": evo_entries}, indent=2),
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    rep = run_ml_optimization_pipeline()
    print(f"[{rep['updated_utc']}] ML Optimization finished. Saved CSVs, ml_report.json, and ml_evolution_log.json.")
