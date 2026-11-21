"""
Exact Causal Python Mirror of LeadingConfluencePro v2.00 Feature & Signal Engine.
Zero look-ahead:
  - Swing pivots confirm strictly at i = p + pivot_right
  - Higher-timeframe bias aligns strictly on completed HTF close timestamps
  - Signal evaluation uses only bars <= N - 2 (or evaluates closed bar index i)
  - Computes 10 standardized directional ML meta-features [f0..f9]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.special import ndtr


@dataclass
class EngineParams:
    min_score: int = 68
    vwap_period: int = 50
    vwap_band_mult: float = 2.0
    vol_period: int = 20
    atr_period: int = 14
    sqz_period: int = 20
    bb_mult: float = 2.0
    kc_mult: float = 1.5
    min_sqz_bars: int = 4
    ker_period: int = 14
    pivot_left: int = 5
    pivot_right: int = 3
    cooldown_bars: int = 6
    allow_consecutive: bool = False
    en_sweep: bool = True
    en_squeeze: bool = True
    en_pullback: bool = True
    en_fvg_disp: bool = True
    use_htf: bool = True
    sl_buffer_atr: float = 0.25
    min_sl_atr: float = 0.80
    max_sl_atr: float = 2.50
    tp1_mult: float = 1.5
    tp2_mult: float = 2.5
    macro_hard_veto: bool = False


def resample_htf(df: pd.DataFrame, tf: str) -> Tuple[pd.DataFrame, int]:
    """
    Resample base OHLCV ('15m' -> '1h', '1h' -> '4h', '4h' -> '1D', or '1d' -> '1W')
    and return (htf_df, htf_seconds).
    """
    if tf == "15m":
        rule, htf_sec = "1h", 3600
    elif tf == "1h":
        rule, htf_sec = "4h", 4 * 3600
    elif tf == "4h":
        rule, htf_sec = "1D", 86400
    else:
        rule, htf_sec = "1W-FRI", 7 * 86400
    tmp = df.set_index("time")
    agg = (
        tmp.resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return agg, htf_sec


def compute_causal_htf_bias(
    df: pd.DataFrame, base_tf: str = "1h"
) -> np.ndarray:
    """
    Compute higher-timeframe bias (+1, 0, -1) aligned strictly on completed HTF close timestamps.
    """
    n = len(df)
    out = np.zeros(n, dtype=np.int32)
    if n < 30:
        return out

    htf_df, htf_sec = resample_htf(df, base_tf)
    if len(htf_df) < 10:
        return out

    bar_sec = 900 if base_tf == "15m" else (3600 if base_tf == "1h" else (4 * 3600 if base_tf == "4h" else 86400))
    hc = htf_df["close"].values.astype(float)
    hh = htf_df["high"].values.astype(float)
    hl = htf_df["low"].values.astype(float)
    hv = np.maximum(htf_df["volume"].values.astype(float), 1.0)
    htf_times = htf_df["time"].astype("int64").values // 10**9

    m = len(htf_df)
    ema20 = hc[0]
    ema50 = hc[0]
    a20 = 2.0 / 21.0
    a50 = 2.0 / 51.0
    htf_bias = np.zeros(m, dtype=np.int32)
    htf_close_ts = np.zeros(m, dtype=np.int64)

    for h in range(m):
        if h > 0:
            ema20 = a20 * hc[h] + (1.0 - a20) * ema20
            ema50 = a50 * hc[h] + (1.0 - a50) * ema50
        w_start = max(0, h - 29)
        tp = (hh[w_start : h + 1] + hl[w_start : h + 1] + hc[w_start : h + 1]) / 3.0
        vk = hv[w_start : h + 1]
        sv = float(np.sum(vk))
        vwap = float(np.sum(tp * vk) / sv) if sv > 0 else hc[h]
        if hc[h] > vwap and ema20 >= ema50:
            htf_bias[h] = 1
        elif hc[h] < vwap and ema20 <= ema50:
            htf_bias[h] = -1
        htf_close_ts[h] = htf_times[h] + htf_sec

    base_times = (df["time"].astype("int64").values // 10**9) + bar_sec
    h_ptr = 0
    for i in range(n):
        bt = base_times[i]
        while h_ptr + 1 < m and htf_close_ts[h_ptr + 1] <= bt:
            h_ptr += 1
        if htf_close_ts[h_ptr] <= bt:
            out[i] = htf_bias[h_ptr]
    return out


def compute_indicators_and_candidates(
    df: pd.DataFrame,
    params: EngineParams,
    base_tf: str = "1h",
    macro_bias_arr: Optional[np.ndarray] = None,
    use_macro_score: bool = False,
) -> Dict[str, Any]:
    """
    Compute all LeadingConfluencePro v2.00 continuous state arrays AND closed-bar
    candidate signals + 10 standardized ML features.
    Note: Candidate generation skips duplicate cooldown gating ONLY IF requested; here
    it mirrors MQL5 strictly on bars [warmup .. N-2].
    """
    n = len(df)
    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    v = np.maximum(df["volume"].values.astype(float), 1.0)
    times = df["time"].values

    if macro_bias_arr is None:
        macro_arr = np.zeros(n, dtype=float)
    else:
        macro_arr = np.asarray(macro_bias_arr, dtype=float)

    htf_bias = compute_causal_htf_bias(df, base_tf) if params.use_htf else np.zeros(n, dtype=np.int32)

    atr = np.zeros(n, dtype=float)
    atr_pct = np.full(n, 50.0, dtype=float)
    rvol = np.ones(n, dtype=float)
    ema_fast = np.zeros(n, dtype=float)
    ema_slow = np.zeros(n, dtype=float)
    ofi = np.zeros(n, dtype=float)
    delta = np.zeros(n, dtype=float)
    cvd = np.zeros(n, dtype=float)
    vwap_val = np.zeros(n, dtype=float)
    vwap_std = np.zeros(n, dtype=float)
    vwap_z = np.zeros(n, dtype=float)
    kc_upper = np.zeros(n, dtype=float)
    kc_lower = np.zeros(n, dtype=float)
    sqz_on = np.zeros(n, dtype=np.int32)
    sqz_count = np.zeros(n, dtype=np.int32)
    sqz_mom = np.zeros(n, dtype=float)
    ker_raw = np.zeros(n, dtype=float)
    ker_smooth = np.zeros(n, dtype=float)

    sw_high = np.zeros(n, dtype=float)
    sw_high_bar = np.full(n, -1, dtype=np.int32)
    sw_high_cvd = np.zeros(n, dtype=float)
    sw_low = np.zeros(n, dtype=float)
    sw_low_bar = np.full(n, -1, dtype=np.int32)
    sw_low_cvd = np.zeros(n, dtype=float)

    alpha_fast = 2.0 / (max(2, params.sqz_period) + 1.0)
    alpha_slow = 2.0 / 51.0

    for i in range(n):
        if i > 0:
            vs = max(0, i - params.vol_period)
            avg_v = float(np.mean(v[vs:i])) if i > vs else v[i]
            rvol[i] = v[i] / max(avg_v, 1.0)

        tr = h[i] - l[i]
        if i > 0:
            tr = max(tr, abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if i == 0:
            atr[i] = tr
        elif i < params.atr_period:
            atr[i] = (atr[i - 1] * i + tr) / (i + 1)
        else:
            atr[i] = (atr[i - 1] * (params.atr_period - 1) + tr) / params.atr_period

        if i >= 20:
            aw = atr[max(0, i - 99) : i + 1]
            atr_pct[i] = 100.0 * float(np.mean(aw <= atr[i]))

        if i == 0:
            ema_fast[i] = c[i]
            ema_slow[i] = c[i]
        else:
            ema_fast[i] = alpha_fast * c[i] + (1.0 - alpha_fast) * ema_fast[i - 1]
            ema_slow[i] = alpha_slow * c[i] + (1.0 - alpha_slow) * ema_slow[i - 1]

        bvc_net = 0.0
        if i > 1:
            dp_s = max(1, i - 19)
            dp = c[dp_s : i + 1] - c[dp_s - 1 : i]
            sig_dp = float(np.std(dp))
            if sig_dp > 1e-12:
                z_dp = float(np.clip((c[i] - c[i - 1]) / sig_dp, -8.0, 8.0))
                bvc_net = 2.0 * float(ndtr(z_dp)) - 1.0

        rng = h[i] - l[i]
        clv_i = (((c[i] - l[i]) - (h[i] - c[i])) / rng) if rng > 1e-12 else 0.0
        ofi[i] = 0.60 * bvc_net + 0.40 * clv_i
        delta[i] = v[i] * ofi[i]
        cvd[i] = (cvd[i - 1] + delta[i]) if i > 0 else delta[i]

        vw_s = max(0, i - max(5, params.vwap_period) + 1)
        tp_w = (h[vw_s : i + 1] + l[vw_s : i + 1] + c[vw_s : i + 1]) / 3.0
        vk_w = v[vw_s : i + 1]
        sum_vw = float(np.sum(vk_w))
        vw = float(np.sum(tp_w * vk_w) / sum_vw) if sum_vw > 0 else tp_w[-1]
        vstd = float(np.sqrt(np.sum(vk_w * (tp_w - vw) ** 2) / sum_vw)) if sum_vw > 0 else 0.0
        vwap_val[i] = vw
        vwap_std[i] = vstd
        z_den = max(vstd, 0.25 * atr[i])
        vwap_z[i] = ((c[i] - vw) / z_den) if z_den > 1e-12 else 0.0

        sq_s = max(0, i - params.sqz_period + 1)
        sq_n = i - sq_s + 1
        c_w = c[sq_s : i + 1]
        sma_sq = float(np.mean(c_w))
        bb_std = float(np.std(c_w))
        bb_u = sma_sq + params.bb_mult * bb_std
        bb_l = sma_sq - params.bb_mult * bb_std
        kc_u = ema_fast[i] + params.kc_mult * atr[i]
        kc_l = ema_fast[i] - params.kc_mult * atr[i]
        kc_upper[i] = kc_u
        kc_lower[i] = kc_l
        sqz_on[i] = 1 if (sq_n >= params.sqz_period and bb_u < kc_u and bb_l > kc_l) else 0
        sqz_count[i] = ((sqz_count[i - 1] if i > 0 else 0) + 1) if sqz_on[i] == 1 else 0

        if sq_n >= 2:
            hh = float(np.max(h[sq_s : i + 1]))
            ll = float(np.min(l[sq_s : i + 1]))
            mid_base = 0.5 * (0.5 * (hh + ll) + sma_sq)
            y_val = c_w - mid_base
            x_idx = np.arange(sq_n, dtype=float)
            x_bar = 0.5 * (sq_n - 1)
            sum_xx = sq_n * (sq_n * sq_n - 1.0) / 12.0
            if sum_xx > 1e-12:
                slope = float(np.sum((x_idx - x_bar) * y_val) / sum_xx)
                intercept = float(np.mean(y_val)) - slope * x_bar
                sqz_mom[i] = intercept + slope * (sq_n - 1)

        if i >= params.ker_period:
            net_dir = abs(c[i] - c[i - params.ker_period])
            path_vol = float(np.sum(np.abs(np.diff(c[i - params.ker_period : i + 1]))))
            ker_raw[i] = (net_dir / path_vol) if path_vol > 1e-12 else 0.0
        ker_smooth[i] = (0.5 * ker_raw[i] + 0.5 * ker_smooth[i - 1]) if i > 0 else ker_raw[i]

        if i > 0:
            sw_high[i] = sw_high[i - 1]
            sw_high_bar[i] = sw_high_bar[i - 1]
            sw_high_cvd[i] = sw_high_cvd[i - 1]
            sw_low[i] = sw_low[i - 1]
            sw_low_bar[i] = sw_low_bar[i - 1]
            sw_low_cvd[i] = sw_low_cvd[i - 1]

            if i >= params.pivot_left + params.pivot_right:
                p = i - params.pivot_right
                is_ph = bool(
                    np.all(h[p - params.pivot_left : p] <= h[p])
                    and np.all(h[p + 1 : i + 1] < h[p])
                )
                is_pl = bool(
                    np.all(l[p - params.pivot_left : p] >= l[p])
                    and np.all(l[p + 1 : i + 1] > l[p])
                )
                if is_ph:
                    sw_high[i] = h[p]
                    sw_high_bar[i] = p
                    sw_high_cvd[i] = cvd[p]
                if is_pl:
                    sw_low[i] = l[p]
                    sw_low_bar[i] = p
                    sw_low_cvd[i] = cvd[p]

    # Signal evaluation across closed bars [warmup .. n - 2]
    warmup = max(
        params.vwap_period, params.sqz_period, params.vol_period, params.atr_period, params.ker_period
    ) + params.pivot_left + params.pivot_right + 10
    warmup = max(65, warmup)

    candidates: List[Dict[str, Any]] = []
    last_sig_bar = -10000
    last_sig_dir = 0
    buy_reset = 1
    sell_reset = 1

    for i in range(1, max(1, n - 1)):
        if c[i] < vwap_val[i]:
            buy_reset = 1
        if c[i] > vwap_val[i]:
            sell_reset = 1

        if i < warmup:
            continue

        at = atr[i]
        rng = h[i] - l[i]
        if at <= 1e-10 or rng < 0.25 * at:
            continue

        recent_sqz = (
            sqz_count[i - 1] >= params.min_sqz_bars
            or sqz_count[i - 2] >= params.min_sqz_bars
            or sqz_count[i - 3] >= params.min_sqz_bars
        )
        if atr_pct[i] < 8.0 and not recent_sqz:
            continue

        if (i - last_sig_bar) < params.cooldown_bars:
            continue

        clv_i = (((c[i] - l[i]) - (h[i] - c[i])) / rng) if rng > 1e-12 else 0.0
        body = c[i] - o[i]
        lower_wick = min(o[i], c[i]) - l[i]
        upper_wick = h[i] - max(o[i], c[i])

        bull_vsa = (rvol[i] >= 1.30 and clv_i >= 0.35 and (lower_wick >= 0.35 * rng or body >= 0.50 * at))
        bear_vsa = (rvol[i] >= 1.30 and clv_i <= -0.35 and (upper_wick >= 0.35 * rng or (-body) >= 0.50 * at))

        bull_fvg = i >= 2 and (l[i] > h[i - 2])
        bear_fvg = i >= 2 and (h[i] < l[i - 2])
        bull_disp = (body >= 0.65 * at) and (rvol[i] >= 1.25) and (clv_i >= 0.50)
        bear_disp = ((-body) >= 0.65 * at) and (rvol[i] >= 1.25) and (clv_i <= -0.50)
        bull_fvg_disp = 1.0 if (bull_fvg or bull_disp) else 0.0
        bear_fvg_disp = 1.0 if (bear_fvg or bear_disp) else 0.0

        sum_vol3 = v[i] + v[i - 1] + v[i - 2]
        net_delta3 = (delta[i] + delta[i - 1] + delta[i - 2]) / max(sum_vol3, 1.0)
        vwap_slope = vwap_val[i] - vwap_val[i - 3]
        ker = ker_smooth[i]
        htf_b = int(htf_bias[i])
        z = vwap_z[i]
        mb = float(macro_arr[i])

        # Bullish Playbooks
        trig_b_sw = False
        trig_b_sq = False
        trig_b_pl = False
        b_ext = l[i]

        if params.en_sweep and sw_low[i - 1] > 0.0 and (i - sw_low_bar[i - 1]) <= 80:
            swl = sw_low[i - 1]
            sw_now = l[i] < swl - 0.05 * at
            sw_prv = (l[i - 1] < swl - 0.05 * at) and (l[i - 1] > swl - 2.5 * at)
            if (
                (sw_now or sw_prv)
                and c[i] > swl
                and (c[i] > o[i] or clv_i >= 0.45)
                and ofi[i] >= 0.15
                and z <= 1.2
                and not (htf_b == -1 and ker >= 0.45)
            ):
                trig_b_sw = True
                b_ext = min(l[i], l[i - 1]) if sw_prv else l[i]

        if params.en_squeeze:
            max_h5 = float(np.max(h[i - 5 : i]))
            if (
                recent_sqz
                and sqz_on[i] == 0
                and c[i] > kc_upper[i]
                and c[i] > max_h5
                and sqz_mom[i] > 0.0
                and sqz_mom[i] > sqz_mom[i - 1]
                and rvol[i] >= 1.20
                and clv_i >= 0.35
                and ofi[i] >= 0.25
                and z <= 2.2
                and (not params.use_htf or htf_b >= 0)
            ):
                trig_b_sq = True
                b_ext = min(l[i], l[i - 1]) if not trig_b_sw else min(b_ext, min(l[i], l[i - 1]))

        if params.en_pullback:
            trend_sup = (params.use_htf and htf_b == 1) or (
                ker >= 0.25 and vwap_slope > 0.0 and ema_fast[i] > ema_slow[i]
            )
            touched = (l[i] <= vwap_val[i] + 0.25 * at) or (l[i - 1] <= vwap_val[i - 1] + 0.25 * at)
            held = l[i] >= vwap_val[i] - 2.2 * vwap_std[i]
            conf_c = c[i] > o[i] and c[i] > vwap_val[i] and clv_i >= 0.35 and (
                c[i] > h[i - 1] or body >= 0.40 * at
            )
            if trend_sup and touched and held and conf_c and ofi[i] >= 0.20 and rvol[i] >= 1.00 and z <= 1.8:
                trig_b_pl = True
                p_min = min(l[i], l[i - 1])
                b_ext = p_min if (not trig_b_sw and not trig_b_sq) else min(b_ext, p_min)

        # Bearish Playbooks
        trig_s_sw = False
        trig_s_sq = False
        trig_s_pl = False
        s_ext = h[i]

        if params.en_sweep and sw_high[i - 1] > 0.0 and (i - sw_high_bar[i - 1]) <= 80:
            swh = sw_high[i - 1]
            sw_now = h[i] > swh + 0.05 * at
            sw_prv = (h[i - 1] > swh + 0.05 * at) and (h[i - 1] < swh + 2.5 * at)
            if (
                (sw_now or sw_prv)
                and c[i] < swh
                and (c[i] < o[i] or clv_i <= -0.45)
                and ofi[i] <= -0.15
                and z >= -1.2
                and not (htf_b == 1 and ker >= 0.45)
            ):
                trig_s_sw = True
                s_ext = max(h[i], h[i - 1]) if sw_prv else h[i]

        if params.en_squeeze:
            min_l5 = float(np.min(l[i - 5 : i]))
            if (
                recent_sqz
                and sqz_on[i] == 0
                and c[i] < kc_lower[i]
                and c[i] < min_l5
                and sqz_mom[i] < 0.0
                and sqz_mom[i] < sqz_mom[i - 1]
                and rvol[i] >= 1.20
                and clv_i <= -0.35
                and ofi[i] <= -0.25
                and z >= -2.2
                and (not params.use_htf or htf_b <= 0)
            ):
                trig_s_sq = True
                s_ext = max(h[i], h[i - 1]) if not trig_s_sw else max(s_ext, max(h[i], h[i - 1]))

        if params.en_pullback:
            trend_sup = (params.use_htf and htf_b == -1) or (
                ker >= 0.25 and vwap_slope < 0.0 and ema_fast[i] < ema_slow[i]
            )
            touched = (h[i] >= vwap_val[i] - 0.25 * at) or (h[i - 1] >= vwap_val[i - 1] - 0.25 * at)
            held = h[i] <= vwap_val[i] + 2.2 * vwap_std[i]
            conf_c = c[i] < o[i] and c[i] < vwap_val[i] and clv_i <= -0.35 and (
                c[i] < l[i - 1] or (-body) >= 0.40 * at
            )
            if trend_sup and touched and held and conf_c and ofi[i] <= -0.20 and rvol[i] >= 1.00 and z >= -1.8:
                trig_s_pl = True
                p_max = max(h[i], h[i - 1])
                s_ext = p_max if (not trig_s_sw and not trig_s_sq) else max(s_ext, p_max)

        # Bull Score
        b_score = 0
        b_pb = "None"
        b_cnt = int(trig_b_sw) + int(trig_b_sq) + int(trig_b_pl)
        if b_cnt > 0:
            b_pb = "MultiConf" if b_cnt > 1 else ("Sweep/SFP" if trig_b_sw else ("SqzBreak" if trig_b_sq else "VWAPPull"))
            b_score += 25
            b_score += 10 if ofi[i] >= 0.35 else (6 if ofi[i] >= 0.15 else 0)
            b_score += 5 if net_delta3 >= 0.12 else (2 if net_delta3 > 0.0 else 0)
            b_cvd_div = trig_b_sw and sw_low[i - 1] > 0.0 and cvd[i] > sw_low_cvd[i - 1]
            if b_cvd_div or bull_vsa:
                b_score += 5
            b_score += 10 if rvol[i] >= 1.50 else (7 if rvol[i] >= 1.20 else (4 if rvol[i] >= 1.00 else 0))
            if bull_vsa:
                b_score += 5
            if trig_b_sw:
                b_score += 15 if z <= 0.25 else (10 if z <= 0.85 else 4)
            else:
                b_score += 15 if (-0.25 <= z <= 1.25) else (10 if z <= 1.85 else 4)
            if not params.use_htf:
                b_score += 5
            elif htf_b == 1:
                b_score += 10
            elif htf_b == 0:
                b_score += 5
            if trig_b_sw and ker <= 0.38:
                b_score += 5
            elif (trig_b_sq or trig_b_pl) and ker >= 0.28:
                b_score += 5
            else:
                b_score += 3
            b_score += 5 if clv_i >= 0.55 else (3 if clv_i >= 0.35 else 0)
            if c[i] > h[i - 1]:
                b_score += 5
            if params.en_fvg_disp and bull_fvg_disp > 0.5:
                b_score += 4
            if use_macro_score:
                if params.macro_hard_veto and mb <= -50.0:
                    b_score = 0
                else:
                    b_score += int(round(0.15 * mb))
            b_score = max(0, min(100, b_score))

        # Bear Score
        s_score = 0
        s_pb = "None"
        s_cnt = int(trig_s_sw) + int(trig_s_sq) + int(trig_s_pl)
        if s_cnt > 0:
            s_pb = "MultiConf" if s_cnt > 1 else ("Sweep/SFP" if trig_s_sw else ("SqzBreak" if trig_s_sq else "VWAPPull"))
            s_score += 25
            s_score += 10 if ofi[i] <= -0.35 else (6 if ofi[i] <= -0.15 else 0)
            s_score += 5 if net_delta3 <= -0.12 else (2 if net_delta3 < 0.0 else 0)
            s_cvd_div = trig_s_sw and sw_high[i - 1] > 0.0 and cvd[i] < sw_high_cvd[i - 1]
            if s_cvd_div or bear_vsa:
                s_score += 5
            s_score += 10 if rvol[i] >= 1.50 else (7 if rvol[i] >= 1.20 else (4 if rvol[i] >= 1.00 else 0))
            if bear_vsa:
                s_score += 5
            if trig_s_sw:
                s_score += 15 if z >= -0.25 else (10 if z >= -0.85 else 4)
            else:
                s_score += 15 if (-1.25 <= z <= 0.25) else (10 if z >= -1.85 else 4)
            if not params.use_htf:
                s_score += 5
            elif htf_b == -1:
                s_score += 10
            elif htf_b == 0:
                s_score += 5
            if trig_s_sw and ker <= 0.38:
                s_score += 5
            elif (trig_s_sq or trig_s_pl) and ker >= 0.28:
                s_score += 5
            else:
                s_score += 3
            s_score += 5 if clv_i <= -0.55 else (3 if clv_i <= -0.35 else 0)
            if c[i] < l[i - 1]:
                s_score += 5
            if params.en_fvg_disp and bear_fvg_disp > 0.5:
                s_score += 4
            if use_macro_score:
                if params.macro_hard_veto and mb >= 50.0:
                    s_score = 0
                else:
                    s_score -= int(round(0.15 * mb))
            s_score = max(0, min(100, s_score))

        allow_buy = params.allow_consecutive or (last_sig_dir != 1) or (buy_reset == 1)
        allow_sell = params.allow_consecutive or (last_sig_dir != -1) or (sell_reset == 1)
        b_ok = (b_score >= params.min_score) and allow_buy
        s_ok = (s_score >= params.min_score) and allow_sell

        final_dir = 0
        if b_ok and not s_ok:
            final_dir = 1
        elif s_ok and not b_ok:
            final_dir = -1
        elif b_ok and s_ok:
            final_dir = 1 if b_score > s_score else (-1 if s_score > b_score else 0)

        if final_dir != 0:
            entry = c[i]
            if final_dir == 1:
                raw_risk = abs(entry - (b_ext - params.sl_buffer_atr * at))
                risk = max(params.min_sl_atr * at, min(params.max_sl_atr * at, raw_risk))
                sl = entry - risk
                tp1 = entry + params.tp1_mult * risk
                tp2 = entry + params.tp2_mult * risk
                score_val = b_score
                pb_name = b_pb
                fvg_disp_aligned = bull_fvg_disp
                buy_reset = 0
                sell_reset = 1
            else:
                raw_risk = abs((s_ext + params.sl_buffer_atr * at) - entry)
                risk = max(params.min_sl_atr * at, min(params.max_sl_atr * at, raw_risk))
                sl = entry + risk
                tp1 = entry - params.tp1_mult * risk
                tp2 = entry - params.tp2_mult * risk
                score_val = s_score
                pb_name = s_pb
                fvg_disp_aligned = bear_fvg_disp
                sell_reset = 0
                buy_reset = 1

            last_sig_bar = i
            last_sig_dir = final_dir

            # Exact 10 directional ML features matching LeadingConfluencePro.mq5 v2.00
            feat = [
                float(final_dir * ofi[i]),
                float(final_dir * net_delta3),
                float(min(rvol[i], 5.0)),
                float(final_dir * z),
                float(ker),
                float(final_dir * htf_b),
                float(final_dir * clv_i),
                float(atr_pct[i] / 100.0),
                float(fvg_disp_aligned),
                float((final_dir * mb) / 100.0),
            ]
            candidates.append(
                {
                    "bar_idx": i,
                    "time": pd.Timestamp(times[i]),
                    "dir": final_dir,
                    "playbook": pb_name,
                    "score": score_val,
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "features": feat,
                }
            )

    return {
        "candidates": candidates,
        "state": {
            "ofi": ofi,
            "rvol": rvol,
            "vwap_z": vwap_z,
            "vwap": vwap_val,
            "ker": ker_smooth,
            "htf_bias": htf_bias,
            "sqz_on": sqz_on,
            "sqz_count": sqz_count,
            "atr_pct": atr_pct,
            "atr": atr,
        },
    }
