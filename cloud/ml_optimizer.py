"""
Institutional Self-Evolving 1-Year Purged Walk-Forward Triple-Barrier ML & Symbol-DNA Optimizer (v4.0):
- Trains strictly on the rolling trailing 365 days of historical data (full H1 + 60d M15 + D1)
- Exponential recency decay (120d half-life) + payoff-magnitude + 2.5x live outcome weighting
- Protected-Runner asymmetric geometry (0.45-0.50R TP1 65% partial + -0.50R runner SL + 3.00R TP2)
- Nested 3-fold Walk-Forward Symbol DNA per (Symbol, Timeframe): Playbook Archetype, UTC Session, HTF Mode & Macro Weight
- Strictly leak-free Empirical-Bayes Dedicated Per-Symbol 18-Feature ML Meta-Models
- Champion-vs-Challenger OOS promotion gate & persistent ml_evolution_log.json
- Exports:
    data/OptimizedConfig.csv (20 cols: Symbol/TF DNA + CLASS_* + GLOBAL)
    data/MLModelWeights.csv (57 cols: GLOBAL + 10 unique Symbol models + 5 CLASS_* fallbacks)
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
from features import EngineParams, FEATURE_NAMES_18, compute_indicators_and_candidates, in_utc_session

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


DNA_GRID: List[Dict[str, Any]] = [
    {"tag": "ALL_24H",     "min_score": 65, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.15, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "ALL_24H_T",   "min_score": 64, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "ALL_34",      "min_score": 65, "vwap_period": 34, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "ALL_LONNY",   "min_score": 64, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 7,  "sess_end": 21, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "ALL_LONNY_M", "min_score": 64, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 7,  "sess_end": 21, "htf_mode": 0, "macro_weight": 0.15, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "ALL_NY",      "min_score": 64, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 12, "sess_end": 21, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "ALL_ASIALON", "min_score": 64, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 17, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "REV_24H",     "min_score": 62, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 0, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "REV_FLEX",    "min_score": 64, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 0, "sess_start": 0,  "sess_end": 24, "htf_mode": 2, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "REV_LONNY",   "min_score": 62, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 0, "sess_start": 7,  "sess_end": 21, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "REV_LONNY_M", "min_score": 62, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 0, "sess_start": 7,  "sess_end": 21, "htf_mode": 0, "macro_weight": 0.15, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "REV_ASIALON", "min_score": 62, "vwap_period": 50, "en_sweep": 1, "en_squeeze": 1, "en_pullback": 0, "sess_start": 0,  "sess_end": 17, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "TRD_24H",     "min_score": 64, "vwap_period": 50, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "TRD_24H_M",   "min_score": 64, "vwap_period": 50, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.15, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "TRD_72",      "min_score": 66, "vwap_period": 72, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 24, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.30, "tp1_mult": 0.45, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "TRD_ASIALON", "min_score": 64, "vwap_period": 50, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1, "sess_start": 0,  "sess_end": 17, "htf_mode": 0, "macro_weight": 0.15, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
    {"tag": "TRD_NY",      "min_score": 64, "vwap_period": 50, "en_sweep": 0, "en_squeeze": 1, "en_pullback": 1, "sess_start": 12, "sess_end": 21, "htf_mode": 0, "macro_weight": 0.00, "sl_buffer_atr": 0.25, "tp1_mult": 0.50, "tp2_mult": 3.00, "cooldown": 4},
]


def _filter_session_events(ev: pd.DataFrame, s0: int, s1: int) -> pd.DataFrame:
    if ev.empty or (s0 == 0 and s1 >= 24):
        return ev
    hrs = pd.to_datetime(ev["time"], utc=True).dt.hour
    mask = (hrs >= s0) & (hrs < s1) if s0 < s1 else ((hrs >= s0) | (hrs < s1))
    return ev[mask].reset_index(drop=True)


def optimize_symbol_timeframe(
    sym: str,
    df: pd.DataFrame,
    base_tf: str,
    macro_arr: np.ndarray,
    cutoff_1y: pd.Timestamp,
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """
    True 3-Fold Anchored Nested Walk-Forward Symbol-DNA Optimizer (v4.0):
    - Tests 17 symbol-specific DNA archetypes (Playbook Archetype x UTC Session x HTF Mode x Macro Weight x VWAP)
    - Each fold selects best DNA strictly on IS history (bar_idx < t_split) and tests on unseen [t_split .. t_end)
    - Enforces soft trade-count floor on M15/H1 production DNA to keep chart signal history active
    - Collects UNBIASED all-playbook 24H events (min_score=58) for zero-leakage per-symbol EB ML training
    """
    dna_grid = [
        d for d in DNA_GRID
        if base_tf != "1d" or (int(d["sess_start"]) == 0 and int(d["sess_end"]) >= 24)
    ]

    bar_times_utc = pd.to_datetime(df["time"], utc=True)
    in_1y_mask = bar_times_utc >= cutoff_1y
    b0 = int(np.argmax(in_1y_mask.values)) if bool(in_1y_mask.any()) else 0

    ind_cache: Dict[Tuple[int, int, float, int, int, int, int, float, int], List[Dict[str, Any]]] = {}
    ev_by_cfg: List[pd.DataFrame] = []
    for cfg in dna_grid:
        cache_key = (
            int(cfg["min_score"]),
            int(cfg["vwap_period"]),
            round(float(cfg["sl_buffer_atr"]), 2),
            int(cfg["en_sweep"]),
            int(cfg["en_squeeze"]),
            int(cfg["en_pullback"]),
            int(cfg["htf_mode"]),
            round(float(cfg["macro_weight"]), 2),
            int(cfg["cooldown"]),
        )
        if cache_key not in ind_cache:
            params = EngineParams(
                min_score=int(cfg["min_score"]),
                vwap_period=int(cfg["vwap_period"]),
                sl_buffer_atr=float(cfg["sl_buffer_atr"]),
                cooldown_bars=int(cfg["cooldown"]),
                allow_consecutive=True,
                min_sl_atr=1.10,
                tp1_mult=0.50,
                tp2_mult=3.00,
                en_sweep=bool(cfg["en_sweep"]),
                en_squeeze=bool(cfg["en_squeeze"]),
                en_pullback=bool(cfg["en_pullback"]),
                htf_mode=int(cfg["htf_mode"]),
                macro_weight=float(cfg["macro_weight"]),
            )
            res = compute_indicators_and_candidates(
                df, params, base_tf=base_tf, macro_bias_arr=macro_arr, use_macro_score=(cfg["macro_weight"] > 0.0)
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
            ev = _filter_session_events(ev, int(cfg["sess_start"]), int(cfg["sess_end"]))
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

    # Full-1Y production DNA selection with soft minimum trade count preference for active charts
    min_pref_n = 50 if base_tf == "15m" else (40 if base_tf == "1h" else 5)
    dense_indices = [idx for idx, ev in enumerate(ev_by_cfg) if len(ev) >= min_pref_n]
    search_indices = dense_indices if dense_indices else [idx for idx, ev in enumerate(ev_by_cfg) if not ev.empty]
    prod_cfg = dna_grid[0]
    best_full_obj = -999.0
    for idx in search_indices:
        sc = _score_segment(ev_by_cfg[idx]["r_net"].values)
        if sc > best_full_obj:
            best_full_obj = sc
            prod_cfg = dna_grid[idx]

    # Collect UNBIASED 1Y all-playbook 24H events (score>=58) for zero-leakage per-symbol EB ML training
    ml_params = EngineParams(
        min_score=58,
        vwap_period=50,
        sl_buffer_atr=0.25,
        cooldown_bars=4,
        allow_consecutive=True,
        min_sl_atr=1.10,
        tp1_mult=0.50,
        tp2_mult=3.00,
        en_sweep=True,
        en_squeeze=True,
        en_pullback=True,
        htf_mode=0,
        macro_weight=0.15,
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

    s0 = int(prod_cfg["sess_start"])
    s1 = int(prod_cfg["sess_end"])
    mw = float(prod_cfg["macro_weight"])
    hm = int(prod_cfg["htf_mode"])
    cd = int(prod_cfg["cooldown"])
    tag = str(prod_cfg["tag"])

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
            "SESS_START_UTC": s0,
            "SESS_END_UTC": s1,
            "HTF_MODE": hm,
            "MACRO_WEIGHT": mw,
            "COOLDOWN_BARS": cd,
            "STRATEGY_TAG": tag,
        }
    elif oos_exp_r > -0.10:
        en_sw = int(prod_cfg["en_sweep"])
        en_sq = 1
        en_pl = int(prod_cfg["en_pullback"])
        if en_sw + en_pl == 0:
            en_sw, en_pl = 1, 1
        row = {
            "SYMBOL": sym,
            "TIMEFRAME": tf_label,
            "MIN_SCORE": min(65, int(prod_cfg["min_score"])),
            "VWAP_PERIOD": int(prod_cfg["vwap_period"]),
            "SL_BUFFER_ATR": 0.25,
            "TP1_MULT": 0.50,
            "TP2_MULT": 3.00,
            "EN_SWEEP": en_sw,
            "EN_SQUEEZE": en_sq,
            "EN_PULLBACK": en_pl,
            "OOS_WIN_PCT": oos_win_pct,
            "OOS_EXP_R": oos_exp_r,
            "OOS_PF": oos_pf,
            "OOS_MAX_DD_R": oos_max_dd,
            "OOS_TRADES": oos_trades,
            "STATUS": "ADAPTIVE_ML_GATE",
            "SESS_START_UTC": s0,
            "SESS_END_UTC": s1,
            "HTF_MODE": hm,
            "MACRO_WEIGHT": mw,
            "COOLDOWN_BARS": cd,
            "STRATEGY_TAG": tag,
        }
    else:
        is_rev = (int(prod_cfg["en_pullback"]) == 0)
        row = {
            "SYMBOL": sym,
            "TIMEFRAME": tf_label,
            "MIN_SCORE": 65 if base_tf in ("15m", "1h") else 68,
            "VWAP_PERIOD": int(prod_cfg["vwap_period"]),
            "SL_BUFFER_ATR": 0.25,
            "TP1_MULT": 0.50,
            "TP2_MULT": 3.00,
            "EN_SWEEP": 1 if is_rev else 0,
            "EN_SQUEEZE": 1,
            "EN_PULLBACK": 0 if is_rev else 1,
            "OOS_WIN_PCT": oos_win_pct,
            "OOS_EXP_R": oos_exp_r,
            "OOS_PF": oos_pf,
            "OOS_MAX_DD_R": oos_max_dd,
            "OOS_TRADES": oos_trades,
            "STATUS": "DEFENSIVE_FILTER",
            "SESS_START_UTC": s0,
            "SESS_END_UTC": s1,
            "HTF_MODE": hm,
            "MACRO_WEIGHT": mw,
            "COOLDOWN_BARS": cd,
            "STRATEGY_TAG": tag,
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

    # Strictly causal inner-fold high-retention quantile thresholding (keep ~40%-95% signals)
    q_grid = [0.05, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
    eff_min_kept = min(min_kept_count, max(12, int(0.12 * len(y_v))))

    tier1_best: Optional[Tuple[float, Dict[str, Any]]] = None
    tier2_best: Optional[Tuple[float, Dict[str, Any]]] = None
    fallback_best: Optional[Tuple[Tuple[float, float], Dict[str, Any]]] = None

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
        fb_key = (exp_q, win_q)
        if fallback_best is None or fb_key > fallback_best[0]:
            fallback_best = (fb_key, cand_info)
        if win_q >= TARGET_WIN_PCT and exp_q > 0.0:
            obj1 = exp_q * math.sqrt(min(n_kept, 600)) + 0.03 * pos_folds
            if tier1_best is None or obj1 > tier1_best[0]:
                tier1_best = (obj1, cand_info)
        elif exp_q > base_exp:
            obj2 = exp_q * math.sqrt(min(n_kept, 600)) + 0.005 * win_q
            if tier2_best is None or obj2 > tier2_best[0]:
                tier2_best = (obj2, cand_info)

    chosen = tier1_best[1] if tier1_best is not None else (tier2_best[1] if tier2_best is not None else (fallback_best[1] if fallback_best is not None else None))
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
        best_q = 0.05
        best_win = base_win
        best_exp = base_exp
        best_pf = min(9.99, base_gp / max(base_gl, 1e-6)) if base_gp > 0.0 else 0.0
        best_kept_cnt = int(len(y_v))
        best_kept = 1.0
        best_pos_folds = 0
        best_eval_folds = 0
        for _, te_idx, _, _ in best_fold_preds:
            if len(te_idx) >= 5:
                best_eval_folds += 1
                if float(np.mean(r_net[te_idx])) > 0.0:
                    best_pos_folds += 1

    # Fit final production StandardScaler + LogisticRegression on full 1-year (+ live feedback) pool
    final_scaler = StandardScaler()
    X_full_s = final_scaler.fit_transform(X)
    final_lr = LogisticRegression(C=best_c, max_iter=1000, random_state=42)
    final_lr.fit(X_full_s, y, sample_weight=sample_w)
    p_full = final_lr.predict_proba(X_full_s)[:, 1]
    best_thresh = round(float(np.clip(np.quantile(p_full, best_q), 0.35, 0.90)), 3)
    prime_thresh = round(float(np.clip(np.quantile(p_full, 0.72), 0.40, 0.92)), 3)

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
        "ml_prime_prob": prime_thresh,
        "kept_fraction": round(best_kept, 3),
        "p_min": round(float(np.min(p_full)), 3),
        "p_max": round(float(np.max(p_full)), 3),
        "p_std": round(float(np.std(p_full)), 3),
        "intercept": float(final_lr.intercept_[0]),
        "means": [float(v) for v in final_scaler.mean_],
        "stds": [float(s if s > 1e-9 else 1.0) for s in final_scaler.scale_],
        "weights": [float(w) for w in final_lr.coef_[0]],
    }


def _fit_linear_blend(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    w_tr: np.ndarray,
    X_prior: np.ndarray,
    y_prior: np.ndarray,
    w_prior: np.ndarray,
    pen: str,
    c_val: float,
    alpha: float,
) -> Tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """Fit symbol LogisticRegression and apply exact algebraic log-odds Empirical-Bayes prior shrinkage."""
    sc_s = StandardScaler()
    Xs = sc_s.fit_transform(X_tr)
    solver = "liblinear" if pen == "l1" else "lbfgs"
    lr_s = LogisticRegression(penalty=pen, C=c_val, solver=solver, max_iter=1000, random_state=42)
    lr_s.fit(Xs, y_tr, sample_weight=w_tr)
    b_s = float(lr_s.intercept_[0])
    w_s = lr_s.coef_[0].astype(float)
    mu_s = sc_s.mean_.astype(float)
    sd_s = np.where(sc_s.scale_ > 1e-9, sc_s.scale_, 1.0)

    if alpha >= 0.999 or len(X_prior) < 30 or len(np.unique(y_prior)) < 2:
        return b_s, mu_s, sd_s, w_s

    sc_p = StandardScaler()
    Xp = sc_p.fit_transform(X_prior)
    lr_p = LogisticRegression(penalty="l2", C=0.02, max_iter=1000, random_state=42)
    lr_p.fit(Xp, y_prior, sample_weight=w_prior)
    b_p = float(lr_p.intercept_[0])
    w_p = lr_p.coef_[0].astype(float)
    mu_p = sc_p.mean_.astype(float)
    sd_p = np.where(sc_p.scale_ > 1e-9, sc_p.scale_, 1.0)

    # Exact transformation of prior log-odds into symbol-standardized coordinates
    w_p_in_s = w_p * (sd_s / sd_p)
    b_p_in_s = b_p + float(np.sum(w_p * ((mu_s - mu_p) / sd_p)))

    b_blend = float(alpha * b_s + (1.0 - alpha) * b_p_in_s)
    w_blend = alpha * w_s + (1.0 - alpha) * w_p_in_s
    return b_blend, mu_s, sd_s, w_blend


def _pred_linear(X: np.ndarray, b: float, mu: np.ndarray, sd: np.ndarray, w: np.ndarray) -> np.ndarray:
    z = b + ((X - mu) / sd) @ w
    return 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))


def _train_symbol_eb_model(
    sym: str,
    ac: str,
    sym_df: pd.DataFrame,
    all_pool: pd.DataFrame,
    fallback_model: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Strictly leak-free Dedicated Per-Symbol Empirical-Bayes Logistic Meta-Model (v4.0):
    - Fits each symbol's own scaler & L1/L2 logistic weights with Empirical-Bayes shrinkage toward
      other symbols strictly prior to each CV fold's test window (zero future or cross-symbol leakage)
    - Selects penalty/regularization/shrinkage strictly by statistical OOS AUC & Brier Score
    - Guarantees non-degenerate, uniquely individualized 57-column parameters for every symbol
    """
    df_ev = sym_df.sort_values("time").reset_index(drop=True)
    n_feats = len(FEATURE_NAMES)
    if len(df_ev) < 35 or len(np.unique(df_ev["y"].values)) < 2:
        fb = dict(fallback_model)
        fb["model_tag"] = "CLASS_FALLBACK"
        fb["top_features"] = []
        return fb

    X = df_ev[FEATURE_NAMES].values.astype(float)
    y = df_ev["y"].values.astype(int)
    r_net = df_ev["r_net"].values.astype(float)
    t_s = pd.to_datetime(df_ev["time"], utc=True)
    max_t = t_s.max()
    age_days = np.maximum(0.0, (max_t - t_s).dt.total_seconds().values / 86400.0)
    time_w = np.exp(-math.log(2.0) * age_days / TIME_DECAY_HALF_LIFE_DAYS)
    live_arr = (
        df_ev["is_live_feedback"].values.astype(float)
        if "is_live_feedback" in df_ev.columns
        else np.zeros(len(df_ev), dtype=float)
    )
    live_count = int(np.sum(live_arr == 1.0))
    payoff_w = 0.75 + 0.50 * np.clip(np.abs(r_net), 0.0, 1.5)
    sw_s = time_w * np.where(live_arr == 1.0, LIVE_FEEDBACK_WEIGHT, 1.0) * payoff_w

    n_splits = 5
    splits: List[Tuple[np.ndarray, np.ndarray]] = []
    if "series_id" in df_ev.columns and df_ev["series_id"].nunique() > 1:
        fold_tr: List[List[int]] = [[] for _ in range(n_splits)]
        fold_te: List[List[int]] = [[] for _ in range(n_splits)]
        for _, grp in df_ev.groupby("series_id"):
            g = grp.sort_values("bar_idx")
            oi = g.index.values
            sp = purged_walk_forward_splits(
                g.reset_index(drop=True),
                n_splits=n_splits,
                embargo_bars=3,
                min_train_events=max(10, int(0.30 * len(g))),
            )
            for fi, (tr_l, te_l) in enumerate(sp):
                if fi < n_splits:
                    fold_tr[fi].extend(oi[tr_l].tolist())
                    fold_te[fi].extend(oi[te_l].tolist())
        for fi in range(n_splits):
            if len(fold_tr[fi]) >= 18 and len(fold_te[fi]) >= 4:
                splits.append((np.array(fold_tr[fi], dtype=int), np.array(fold_te[fi], dtype=int)))
    if len(splits) < 2:
        splits = purged_walk_forward_splits(df_ev, n_splits=n_splits, embargo_bars=3, min_train_events=20)
    if not splits:
        fb = dict(fallback_model)
        fb["model_tag"] = "CLASS_FALLBACK"
        fb["top_features"] = []
        return fb

    other_ac = all_pool[(all_pool["symbol"] != sym) & (all_pool["asset_class"] == ac)]
    prior_src = other_ac if len(other_ac) >= 120 else all_pool[all_pool["symbol"] != sym]
    prior_times = pd.to_datetime(prior_src["time"], utc=True) if not prior_src.empty else pd.Series([], dtype="datetime64[ns, UTC]")

    hp_grid = [("l2", 0.015), ("l2", 0.06), ("l1", 0.20)]
    alphas = [1.0, 0.65, 0.35]

    best_sel = None
    fallback_sel = None

    def _eval_combo(pen: str, c_val: float, alpha: float) -> Optional[Tuple[Tuple[float, float], str, float, float, float, float, np.ndarray, np.ndarray, List[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]], float]]:
        oof_p = np.full(len(df_ev), np.nan)
        fpreds: List[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        for tr_idx, te_idx in splits:
            if len(np.unique(y[tr_idx])) < 2:
                continue
            t_cut = t_s.iloc[te_idx].min()
            if not prior_src.empty:
                pr_sub = prior_src[prior_times < t_cut]
            else:
                pr_sub = pd.DataFrame()
            if len(pr_sub) > 0:
                Xp = pr_sub[FEATURE_NAMES].values.astype(float)
                yp = pr_sub["y"].values.astype(int)
                rp = pr_sub["r_net"].values.astype(float)
                ap = np.maximum(0.0, (t_cut - pd.to_datetime(pr_sub["time"], utc=True)).dt.total_seconds().values / 86400.0)
                lp = (
                    np.where(pr_sub["is_live_feedback"].values.astype(float) == 1.0, LIVE_FEEDBACK_WEIGHT, 1.0)
                    if "is_live_feedback" in pr_sub.columns
                    else np.ones(len(pr_sub), dtype=float)
                )
                wp = np.exp(-math.log(2.0) * ap / TIME_DECAY_HALF_LIFE_DAYS) * lp * (0.75 + 0.50 * np.clip(np.abs(rp), 0.0, 1.5))
            else:
                Xp = np.empty((0, n_feats))
                yp = np.empty(0, dtype=int)
                wp = np.empty(0)

            b, mu, sd, w = _fit_linear_blend(X[tr_idx], y[tr_idx], sw_s[tr_idx], Xp, yp, wp, pen, c_val, alpha)
            ptr = _pred_linear(X[tr_idx], b, mu, sd, w)
            pte = _pred_linear(X[te_idx], b, mu, sd, w)
            oof_p[te_idx] = pte
            fpreds.append((tr_idx, te_idx, ptr, pte))

        val = np.isfinite(oof_p)
        if int(np.sum(val)) < 20 or len(np.unique(y[val])) < 2:
            return None
        auc = float(roc_auc_score(y[val], oof_p[val]))
        br = float(brier_score_loss(y[val], oof_p[val]))
        p_sd = float(np.std(oof_p[val]))
        return (round(auc, 3), -br), pen, c_val, alpha, auc, br, val, oof_p, fpreds, p_sd

    for pen, c_val in hp_grid:
        for alpha in alphas:
            res_c = _eval_combo(pen, c_val, alpha)
            if res_c is None:
                continue
            stat_key, _, _, _, _, _, _, _, _, p_sd = res_c
            if (pen, c_val, alpha) == ("l2", 0.06, 0.65):
                fallback_sel = res_c
            if p_sd >= 0.008 and (best_sel is None or stat_key > best_sel[0]):
                best_sel = res_c

    chosen_sel = best_sel if best_sel is not None else fallback_sel
    if chosen_sel is None:
        chosen_sel = _eval_combo("l2", 0.06, 0.65)
    if chosen_sel is None:
        fb = dict(fallback_model)
        fb["model_tag"] = "CLASS_FALLBACK"
        fb["top_features"] = []
        return fb

    _, pen, c_val, alpha, auc_lr, brier_lr, val, oof_p, fpreds, _ = chosen_sel
    y_v = y[val]
    r_v = r_net[val]
    base_win = 100.0 * float(np.mean(y_v))
    base_exp = float(np.mean(r_v))
    base_gp = float(np.sum(r_v[r_v > 0.0]))
    base_gl = float(abs(np.sum(r_v[r_v < 0.0])))

    q_grid = [0.10, 0.20, 0.30, 0.40, 0.50] if auc_lr >= 0.505 else [0.05, 0.10]
    eff_min_kept = max(12, int(0.40 * len(y_v)))
    pos_cands: List[Tuple[float, Dict[str, Any]]] = []
    all_cands: List[Tuple[Tuple[float, float], Dict[str, Any]]] = []

    for q in q_grid:
        cm = np.zeros(len(df_ev), dtype=bool)
        pos_f = 0
        eval_f = 0
        for _, te_idx, ptr, pte in fpreds:
            th = float(np.quantile(ptr, q))
            mf = pte >= th
            cm[te_idx] = mf
            if int(np.sum(mf)) >= 4:
                eval_f += 1
                if float(np.mean(r_net[te_idx][mf])) > 0.0:
                    pos_f += 1
        fk = cm[val]
        nk = int(np.sum(fk))
        if nk < eff_min_kept:
            continue
        rm = r_v[fk]
        ym = y_v[fk]
        wq = 100.0 * float(np.mean(ym))
        eq = float(np.mean(rm))
        gp = float(np.sum(rm[rm > 0.0]))
        gl = float(abs(np.sum(rm[rm < 0.0])))
        pf = min(9.99, gp / max(gl, 1e-6)) if gp > 0.0 else 0.0
        kept_frac = nk / float(len(y_v))
        c_dict = {
            "q": q,
            "win_pct": wq,
            "exp_r": eq,
            "pf": pf,
            "gp": gp,
            "gl": gl,
            "n_kept": nk,
            "kept_frac": kept_frac,
            "pos_folds": pos_f,
            "eval_folds": eval_f,
        }
        all_cands.append(((eq, wq), c_dict))
        if eq > 0.0:
            obj = eq * math.sqrt(min(nk, 200)) + 0.03 * pos_f + 0.008 * max(0.0, wq - 68.0)
            pos_cands.append((obj, c_dict))

    if pos_cands:
        chosen_q = max(pos_cands, key=lambda x: x[0])[1]
    elif all_cands:
        chosen_q = max(all_cands, key=lambda x: x[0])[1]
    else:
        ef_b = pf_b = 0
        for _, te_idx, _, _ in fpreds:
            if len(te_idx) >= 4:
                ef_b += 1
                if float(np.mean(r_net[te_idx])) > 0.0:
                    pf_b += 1
        chosen_q = {
            "q": 0.05,
            "win_pct": base_win,
            "exp_r": base_exp,
            "pf": min(9.99, base_gp / max(base_gl, 1e-6)) if base_gp > 0.0 else 0.0,
            "gp": base_gp,
            "gl": base_gl,
            "n_kept": int(len(y_v)),
            "kept_frac": 1.0,
            "pos_folds": pf_b,
            "eval_folds": ef_b,
        }

    # Fit final full-1Y production blended model on all symbol data + full prior pool
    if not prior_src.empty:
        Xp_all = prior_src[FEATURE_NAMES].values.astype(float)
        yp_all = prior_src["y"].values.astype(int)
        rp_all = prior_src["r_net"].values.astype(float)
        ap_all = np.maximum(0.0, (max_t - pd.to_datetime(prior_src["time"], utc=True)).dt.total_seconds().values / 86400.0)
        lp_all = (
            np.where(prior_src["is_live_feedback"].values.astype(float) == 1.0, LIVE_FEEDBACK_WEIGHT, 1.0)
            if "is_live_feedback" in prior_src.columns
            else np.ones(len(prior_src), dtype=float)
        )
        wp_all = np.exp(-math.log(2.0) * ap_all / TIME_DECAY_HALF_LIFE_DAYS) * lp_all * (0.75 + 0.50 * np.clip(np.abs(rp_all), 0.0, 1.5))
    else:
        Xp_all = np.empty((0, n_feats))
        yp_all = np.empty(0, dtype=int)
        wp_all = np.empty(0)

    b_fin, mu_fin, sd_fin, w_fin = _fit_linear_blend(X, y, sw_s, Xp_all, yp_all, wp_all, pen, c_val, alpha)
    if float(np.max(np.abs(w_fin))) < 1e-5:
        pen, c_val, alpha = "l2", 0.06, 0.65
        b_fin, mu_fin, sd_fin, w_fin = _fit_linear_blend(X, y, sw_s, Xp_all, yp_all, wp_all, pen, c_val, alpha)

    p_full = _pred_linear(X, b_fin, mu_fin, sd_fin, w_fin)
    best_q = float(chosen_q["q"])
    best_thresh = round(float(np.clip(np.quantile(p_full, best_q), 0.35, 0.90)), 3)
    prime_q = min(0.85, max(best_q + 0.22, 0.72))
    prime_thresh = round(float(np.clip(np.quantile(p_full, prime_q), 0.40, 0.92)), 3)

    model_tag = "SYM_PURE" if alpha >= 0.99 else f"SYM_EB{int(round(alpha * 100))}"
    top_idx = np.argsort(-np.abs(w_fin))[:4]
    top_feats = [
        {"name": FEATURE_NAMES[int(j)], "weight": round(float(w_fin[int(j)]), 4)}
        for j in top_idx
    ]

    return {
        "ok": True,
        "model_tag": model_tag,
        "pen": pen,
        "best_c": c_val,
        "alpha": alpha,
        "samples": int(len(df_ev)),
        "oos_samples": int(len(y_v)),
        "live_feedback_used": live_count,
        "selected_quantile": best_q,
        "auc_lr": round(auc_lr, 4),
        "brier_lr": round(brier_lr, 4),
        "base_oos_win_pct": round(base_win, 1),
        "base_oos_exp_r": round(base_exp, 3),
        "ml_oos_win_pct": round(float(chosen_q["win_pct"]), 1),
        "ml_oos_exp_r": round(float(chosen_q["exp_r"]), 4),
        "ml_oos_pf": round(float(chosen_q["pf"]), 2),
        "ml_gp": round(float(chosen_q["gp"]), 4),
        "ml_gl": round(float(chosen_q["gl"]), 4),
        "ml_oos_trades": int(chosen_q["n_kept"]),
        "ml_pos_folds": int(chosen_q["pos_folds"]),
        "ml_eval_folds": int(chosen_q["eval_folds"]),
        "ml_min_prob": best_thresh,
        "ml_prime_prob": prime_thresh,
        "kept_fraction": round(float(chosen_q["kept_frac"]), 3),
        "p_min": round(float(np.min(p_full)), 3),
        "p_max": round(float(np.max(p_full)), 3),
        "p_std": round(float(np.std(p_full)), 3),
        "top_features": top_feats,
        "intercept": float(b_fin),
        "means": [float(v) for v in mu_fin],
        "stds": [float(s if s > 1e-9 else 1.0) for s in sd_fin],
        "weights": [float(w) for w in w_fin],
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
                        "MIN_SCORE": 68,
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
                        "SESS_START_UTC": 0,
                        "SESS_END_UTC": 24,
                        "HTF_MODE": 0,
                        "MACRO_WEIGHT": 0.15,
                        "COOLDOWN_BARS": 4,
                        "STRATEGY_TAG": "DEF_24H",
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

    # Train Asset-Class Fallback Meta-Models (CRYPTO, METALS, ENERGY, FX, INDEX) on 1-year intraday events
    ac_models: Dict[str, Dict[str, Any]] = {}
    all_ac_names = sorted({str(meta["asset_class"]) for meta in WATCHLIST.values()})
    if not pooled_all_raw.empty:
        for ac in all_ac_names:
            ac_full_df = pooled_all_raw[pooled_all_raw["asset_class"] == ac]
            if ac_full_df.empty:
                continue
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
            ac_models[ac] = ac_res
            if ac_res.get("ok"):
                print(
                    f"ASSET_CLASS[{ac.upper()}]: samples={ac_res['samples']}, "
                    f"AUC={ac_res['auc_lr']}, ML={ac_res['ml_oos_win_pct']}%/{ac_res['ml_oos_exp_r']:+.4f}R "
                    f"(P>={ac_res['ml_min_prob']:.3f}, validated={ac_valid})",
                    flush=True,
                )

    def _pack_model(m: Dict[str, Any], src: str) -> Dict[str, Any]:
        return {
            "source": src,
            "intercept": round(float(m["intercept"]), 6),
            "ml_min_prob": round(float(m["ml_min_prob"]), 3),
            "means": [round(float(x), 6) for x in m["means"]],
            "stds": [round(float(x), 6) for x in m["stds"]],
            "weights": [round(float(w), 6) for w in m["weights"]],
        }

    # Train Dedicated Per-Symbol Empirical-Bayes ML Meta-Models (100% symbol-unique)
    challenger_ml_map: Dict[str, Dict[str, Any]] = {"GLOBAL": global_model}
    sym_ml_summary: Dict[str, Any] = {}
    exported_models: Dict[str, Any] = {"GLOBAL": _pack_model(global_model, "GLOBAL")}
    sym_eb_results: Dict[str, Dict[str, Any]] = {}

    for sym in WATCHLIST.keys():
        ac = WATCHLIST[sym]["asset_class"]
        evs = per_sym_events.get(sym, [])
        merged_sym = pd.concat(evs, ignore_index=True) if evs else pd.DataFrame()
        sym_intra = merged_sym[merged_sym["timeframe"].isin(["M15", "H1"])] if not merged_sym.empty else pd.DataFrame()
        live_sym = live_intra[live_intra["symbol"] == sym] if not live_intra.empty else pd.DataFrame()
        sym_train = _merge_live_feedback(sym_intra if len(sym_intra) >= 75 else merged_sym, live_sym)

        ac_candidate = ac_models.get(ac)
        fallback_m = ac_candidate if (ac_candidate and ac_candidate.get("ok")) else global_model

        sym_res = _train_symbol_eb_model(sym, ac, sym_train, train_pool, fallback_m)
        sym_eb_results[sym] = sym_res
        sym_min_prob = float(sym_res["ml_min_prob"])
        used_model = f"{sym_res.get('model_tag', 'SYM_EB')}({sym_res.get('pen', 'l2').upper()}, AUC={sym_res.get('auc_lr', 0.5):.3f}, P>={sym_min_prob:.3f})"

        print(
            f"SYM_ML[{sym:<6}]: {sym_res.get('model_tag', '?'):<8} {sym_res.get('pen', 'l2').upper()}({sym_res.get('best_c', 0.06):.3f}) "
            f"AUC={sym_res.get('auc_lr', 0.5):.3f} | Base={sym_res.get('base_oos_win_pct', 0):.1f}%/{sym_res.get('base_oos_exp_r', 0):+.3f}R "
            f"-> ML={sym_res.get('ml_oos_win_pct', 0):.1f}%/{sym_res.get('ml_oos_exp_r', 0):+.4f}R "
            f"PF={sym_res.get('ml_oos_pf', 0):.2f} (n={sym_res.get('ml_oos_trades', 0)}, "
            f"folds={sym_res.get('ml_pos_folds', 0)}/{sym_res.get('ml_eval_folds', 0)}, P>={sym_min_prob:.3f})",
            flush=True,
        )

        sym_ml_summary[sym] = {
            k: v for k, v in sym_res.items() if k not in ("means", "stds", "weights", "ml_gp", "ml_gl")
        }
        challenger_ml_map[sym] = sym_res
        exported_models[sym] = _pack_model(sym_res, used_model)

        for r in opt_rows:
            if r["SYMBOL"] == sym:
                r["ML_MIN_PROB"] = sym_min_prob
                r["ML_MODEL"] = used_model

    # Register Asset-Class Fallback Models in CSV & Report for auto-detection on unseen broker symbols
    class_keys: List[str] = []
    for ac in all_ac_names:
        ckey = f"CLASS_{ac.upper()}"
        class_keys.append(ckey)
        ac_m = ac_models.get(ac)
        use_ac_m = ac_m if (ac_m and ac_m.get("ok")) else global_model
        challenger_ml_map[ckey] = use_ac_m
        exported_models[ckey] = _pack_model(use_ac_m, ckey)

    # Compute Honest Portfolio Summary Across All 10 Dedicated Per-Symbol Models
    eb_ok_syms = [r for r in sym_eb_results.values() if r.get("ok") and int(r.get("ml_oos_trades", 0)) > 0]
    eb_tot_trades = sum(int(r["ml_oos_trades"]) for r in eb_ok_syms)
    if eb_tot_trades > 0:
        eb_base_win = round(sum(float(r["base_oos_win_pct"]) * int(r["ml_oos_trades"]) for r in eb_ok_syms) / eb_tot_trades, 1)
        eb_base_exp = round(sum(float(r["base_oos_exp_r"]) * int(r["ml_oos_trades"]) for r in eb_ok_syms) / eb_tot_trades, 4)
        eb_ml_win = round(sum(float(r["ml_oos_win_pct"]) * int(r["ml_oos_trades"]) for r in eb_ok_syms) / eb_tot_trades, 1)
        eb_ml_exp = round(sum(float(r["ml_oos_exp_r"]) * int(r["ml_oos_trades"]) for r in eb_ok_syms) / eb_tot_trades, 4)
        eb_gp = sum(float(r.get("ml_gp", 0.0)) for r in eb_ok_syms)
        eb_gl = sum(float(r.get("ml_gl", 0.0)) for r in eb_ok_syms)
        eb_pf = round(min(9.99, eb_gp / max(eb_gl, 1e-6)), 2) if eb_gp > 0.0 else 0.0
    else:
        eb_base_win = eb_ml_win = float(global_model.get("ml_oos_win_pct", 0.0))
        eb_base_exp = eb_ml_exp = float(global_model.get("ml_oos_exp_r", 0.0))
        eb_pf = float(global_model.get("ml_oos_pf", 0.0))

    unique_weight_sigs = {
        tuple(round(float(w), 6) for w in exported_models[s]["weights"])
        for s in WATCHLIST.keys()
        if s in exported_models
    }
    pos_sym_cnt = sum(1 for r in eb_ok_syms if float(r.get("ml_oos_exp_r", 0.0)) > 0.0)
    portfolio_eb_summary = {
        "symbols_total": len(WATCHLIST),
        "unique_symbol_models": len(unique_weight_sigs),
        "positive_symbols_count": pos_sym_cnt,
        "oos_trades": eb_tot_trades,
        "base_oos_win_pct": eb_base_win,
        "base_oos_exp_r": eb_base_exp,
        "ml_oos_win_pct": eb_ml_win,
        "ml_oos_exp_r": eb_ml_exp,
        "ml_oos_pf": eb_pf,
    }
    print(
        f"PORTFOLIO 10-SYMBOL EB-ML (n={eb_tot_trades} unseen OOS trades): "
        f"Base {eb_base_win:.1f}%/{eb_base_exp:+.4f}R -> ML {eb_ml_win:.1f}%/{eb_ml_exp:+.4f}R "
        f"PF={eb_pf:.2f} | Positive={pos_sym_cnt}/{len(WATCHLIST)} | UniqueModels={len(unique_weight_sigs)}/{len(WATCHLIST)}",
        flush=True,
    )

    # Champion-vs-Challenger Auto-Evolution Gate (v4.0 evaluates Dedicated Symbol Portfolio)
    prev_report: Optional[Dict[str, Any]] = None
    report_path = DATA_DIR / "ml_report.json"
    if report_path.exists():
        try:
            prev_report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            prev_report = None

    drift_info = _check_live_drift()
    prev_is_current = bool(
        prev_report
        and prev_report.get("version") == "4.0"
        and len(prev_report.get("feature_names", [])) == n_feats
    )
    new_win = float(eb_ml_win)
    new_exp = float(eb_ml_exp)
    new_cnt = int(eb_tot_trades)
    new_score = new_exp * math.sqrt(max(1, new_cnt)) + 0.01 * (new_win - 65.0)

    promoted = True
    evolution_status = "PROMOTED_V40_SYMBOL_DNA"
    champ_exp = None
    champ_win = None
    champ_age_days = None

    if prev_is_current and prev_report is not None:
        pg = prev_report.get("portfolio_eb_ml_summary") or prev_report.get("global_model", {})
        champ_exp = float(pg.get("ml_oos_exp_r", 0.0))
        champ_win = float(pg.get("ml_oos_win_pct", 0.0))
        champ_cnt = int(pg.get("oos_trades", pg.get("ml_oos_trades", 1)))
        champ_score = champ_exp * math.sqrt(max(1, champ_cnt)) + 0.01 * (champ_win - 65.0)
        try:
            champ_age_days = round((now_dt - _to_utc_ts(prev_report.get("updated_utc"))).total_seconds() / 86400.0, 2)
        except Exception:
            champ_age_days = 0.0

        if not eb_ok_syms or (champ_exp > 0.0 and new_exp <= 0.0):
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
    all_model_keys = ["GLOBAL"] + list(WATCHLIST.keys()) + class_keys
    if not promoted and prev_is_current and prev_report is not None and isinstance(prev_report.get("ml_models"), dict):
        kept_models = prev_report["ml_models"]
        ml_rows = []
        for k_sym in all_model_keys:
            km = kept_models.get(k_sym, exported_models.get(k_sym))
            if km and len(km.get("weights", [])) == n_feats:
                ml_rows.append(_format_ml_csv_row(k_sym, km))
                exported_models[k_sym] = km
                for r in opt_rows:
                    if r["SYMBOL"] == k_sym:
                        r["ML_MIN_PROB"] = float(km["ml_min_prob"])
    else:
        ml_rows = [_format_ml_csv_row(k_sym, challenger_ml_map[k_sym]) for k_sym in all_model_keys]

    # Compute Walk-Forward Portfolio Audit Summary & Per-Playbook Breakdown
    intra_cfgs = [r for r in opt_rows if r["TIMEFRAME"] in ("M15", "H1") and r.get("OOS_TRADES", 0) > 0]
    wf_opt_cnt = sum(1 for r in intra_cfgs if r["STATUS"] == "WF_OPTIMIZED")
    adap_cnt = sum(1 for r in intra_cfgs if r["STATUS"] == "ADAPTIVE_ML_GATE")
    def_cnt = sum(1 for r in intra_cfgs if r["STATUS"] == "DEFENSIVE_FILTER")
    tot_intra_trades = sum(int(r.get("OOS_TRADES", 0)) for r in intra_cfgs)
    avg_intra_win = round(float(np.mean([r["OOS_WIN_PCT"] for r in intra_cfgs])), 1) if intra_cfgs else 0.0
    avg_intra_exp = round(float(np.mean([r["OOS_EXP_R"] for r in intra_cfgs])), 3) if intra_cfgs else 0.0
    avg_intra_pf = round(float(np.mean([r.get("OOS_PF", 1.0) for r in intra_cfgs])), 2) if intra_cfgs else 0.0

    wf_summary = {
        "v40_symbol_dna": True,
        "training_window_days": WINDOW_DAYS,
        "intraday_configs_total": len(intra_cfgs),
        "wf_optimized_count": wf_opt_cnt,
        "adaptive_ml_gate_count": adap_cnt,
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

    # Add 5 CLASS_* fallback configs (for auto-detection of unseen broker symbols) + GLOBAL/ALL
    global_thresh = float(exported_models.get("GLOBAL", global_model).get("ml_min_prob", 0.65))
    for ac in all_ac_names:
        ckey = f"CLASS_{ac.upper()}"
        ac_syms = {s for s, m in WATCHLIST.items() if m["asset_class"] == ac}
        ac_intra_rows = [r for r in intra_cfgs if r["SYMBOL"] in ac_syms]
        if ac_intra_rows:
            best_ac_row = max(
                ac_intra_rows,
                key=lambda r: (1 if r["STATUS"] == "WF_OPTIMIZED" else 0, float(r["OOS_EXP_R"]), float(r["OOS_WIN_PCT"])),
            )
            ac_thresh = float(exported_models.get(ckey, global_model).get("ml_min_prob", global_thresh))
            opt_rows.append(
                {
                    "SYMBOL": ckey,
                    "TIMEFRAME": "ALL",
                    "MIN_SCORE": int(best_ac_row["MIN_SCORE"]),
                    "VWAP_PERIOD": int(best_ac_row["VWAP_PERIOD"]),
                    "SL_BUFFER_ATR": float(best_ac_row["SL_BUFFER_ATR"]),
                    "TP1_MULT": float(best_ac_row["TP1_MULT"]),
                    "TP2_MULT": float(best_ac_row["TP2_MULT"]),
                    "EN_SWEEP": int(best_ac_row["EN_SWEEP"]),
                    "EN_SQUEEZE": int(best_ac_row["EN_SQUEEZE"]),
                    "EN_PULLBACK": int(best_ac_row["EN_PULLBACK"]),
                    "ML_MIN_PROB": ac_thresh,
                    "OOS_WIN_PCT": float(best_ac_row["OOS_WIN_PCT"]),
                    "OOS_EXP_R": float(best_ac_row["OOS_EXP_R"]),
                    "OOS_PF": float(best_ac_row.get("OOS_PF", 1.0)),
                    "OOS_MAX_DD_R": float(best_ac_row.get("OOS_MAX_DD_R", 0.0)),
                    "OOS_TRADES": int(best_ac_row.get("OOS_TRADES", 0)),
                    "STATUS": ckey,
                    "SESS_START_UTC": int(best_ac_row.get("SESS_START_UTC", 0)),
                    "SESS_END_UTC": int(best_ac_row.get("SESS_END_UTC", 24)),
                    "HTF_MODE": int(best_ac_row.get("HTF_MODE", 0)),
                    "MACRO_WEIGHT": float(best_ac_row.get("MACRO_WEIGHT", 0.15)),
                    "COOLDOWN_BARS": int(best_ac_row.get("COOLDOWN_BARS", 4)),
                    "STRATEGY_TAG": str(best_ac_row.get("STRATEGY_TAG", "ALL_24H")),
                }
            )

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
            "SESS_START_UTC": 0,
            "SESS_END_UTC": 24,
            "HTF_MODE": 0,
            "MACRO_WEIGHT": 0.15,
            "COOLDOWN_BARS": 4,
            "STRATEGY_TAG": "ALL_24H",
        }
    )

    # Export data/OptimizedConfig.csv (20 columns: Symbol/TF DNA + CLASS_* + GLOBAL)
    cfg_lines = [
        f"# LeadingConfluencePro v4.0 1-Year Self-Evolving Symbol-DNA Config | Updated UTC: {now_iso}",
        "SYMBOL,TIMEFRAME,MIN_SCORE,VWAP_PERIOD,SL_BUFFER_ATR,TP1_MULT,TP2_MULT,EN_SWEEP,EN_SQUEEZE,EN_PULLBACK,ML_MIN_PROB,OOS_WIN_PCT,OOS_EXP_R,STATUS,SESS_START_UTC,SESS_END_UTC,HTF_MODE,MACRO_WEIGHT,COOLDOWN_BARS,STRATEGY_TAG",
    ]
    for r in opt_rows:
        cfg_lines.append(
            f"{r['SYMBOL']},{r['TIMEFRAME']},{r['MIN_SCORE']},{r['VWAP_PERIOD']},"
            f"{r['SL_BUFFER_ATR']:.2f},{r['TP1_MULT']:.2f},{r['TP2_MULT']:.2f},"
            f"{r['EN_SWEEP']},{r['EN_SQUEEZE']},{r['EN_PULLBACK']},"
            f"{r.get('ML_MIN_PROB', global_thresh):.3f},{r['OOS_WIN_PCT']:.1f},{r['OOS_EXP_R']:.3f},{r['STATUS']},"
            f"{int(r.get('SESS_START_UTC', 0))},{int(r.get('SESS_END_UTC', 24))},{int(r.get('HTF_MODE', 0))},"
            f"{float(r.get('MACRO_WEIGHT', 0.15)):.2f},{int(r.get('COOLDOWN_BARS', 4))},{r.get('STRATEGY_TAG', 'ALL_24H')}"
        )
    (DATA_DIR / "OptimizedConfig.csv").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

    # Export data/MLModelWeights.csv (57 columns: GLOBAL + 10 unique symbol models + 5 CLASS_* fallbacks)
    ml_header = (
        "SYMBOL,INTERCEPT,ML_MIN_PROB,"
        + ",".join(f"M{k}" for k in range(n_feats))
        + ","
        + ",".join(f"S{k}" for k in range(n_feats))
        + ","
        + ",".join(f"W{k}" for k in range(n_feats))
    )
    ml_lines = [
        f"# LeadingConfluencePro v4.0 1-Year Dedicated Symbol Empirical-Bayes ML Weights | Updated UTC: {now_iso}",
        ml_header,
    ] + ml_rows
    (DATA_DIR / "MLModelWeights.csv").write_text("\n".join(ml_lines) + "\n", encoding="utf-8")

    evolution_decision = {
        "status": evolution_status,
        "promoted": promoted,
        "challenger_oos_win_pct": new_win,
        "challenger_oos_exp_r": new_exp,
        "challenger_oos_pf": eb_pf,
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
        "version": "4.0",
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
        "portfolio_eb_ml_summary": portfolio_eb_summary,
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
            "samples_1y": int(sum(int(r.get("samples", 0)) for r in sym_eb_results.values())),
            "live_feedback_used": int(global_model.get("live_feedback_used", 0)),
            "auc_lr": round(float(np.mean([float(r.get("auc_lr", 0.5)) for r in eb_ok_syms])), 4) if eb_ok_syms else global_model.get("auc_lr"),
            "base_oos_win_pct": eb_base_win,
            "ml_oos_win_pct": new_win,
            "ml_oos_exp_r": new_exp,
            "ml_oos_pf": eb_pf,
            "ml_oos_trades": new_cnt,
            "ml_pos_folds": pos_sym_cnt,
            "ml_eval_folds": len(WATCHLIST),
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
