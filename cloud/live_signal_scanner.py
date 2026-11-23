"""
24/7 Cloud Live Signal Scanner, Live Outcome Grader & Optional Telegram / Discord Notifier (v3.5).
- Loads MacroFlowBias.csv, OptimizedConfig.csv, and 18-feature MLModelWeights.csv
- Fetches fresh 15m / 1h candles for all 10 watchlist symbols
- Evaluates strictly closed bars using identical MQL5 v3.50 logic + ML gate
- Persists & auto-grades live confirmed signals in data/signal_outcomes_ledger.json
- Writes data/latest_signals.json (with live_outcome_audit) and deduplicates alerts via data/alert_state.json
- Dispatches Telegram/Discord notifications only if secret env vars are configured
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from data_clients import WATCHLIST, fetch_yahoo_ohlcv
from features import EngineParams, compute_indicators_and_candidates

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


def _load_macro_bias_map() -> Dict[str, Tuple[int, str]]:
    path = DATA_DIR / "MacroFlowBias.csv"
    out: Dict[str, Tuple[int, str]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("SYMBOL"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            out[parts[0].upper()] = (int(round(float(parts[1]))), parts[2] if len(parts) >= 3 else "")
    return out


def _load_optimized_configs() -> Dict[Tuple[str, str], Dict[str, Any]]:
    path = DATA_DIR / "OptimizedConfig.csv"
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("SYMBOL"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 11:
            continue
        out[(parts[0].upper(), parts[1].upper())] = {
            "min_score": int(float(parts[2])),
            "vwap_period": int(float(parts[3])),
            "sl_buffer_atr": float(parts[4]),
            "tp1_mult": float(parts[5]),
            "tp2_mult": float(parts[6]),
            "en_sweep": int(float(parts[7])) == 1,
            "en_squeeze": int(float(parts[8])) == 1,
            "en_pullback": int(float(parts[9])) == 1,
            "ml_min_prob": float(parts[10]),
            "oos_exp_r": float(parts[12]) if len(parts) >= 13 else 0.0,
            "status": parts[13] if len(parts) >= 14 else "",
        }
    return out


def _load_ml_weights() -> Dict[str, Dict[str, Any]]:
    path = DATA_DIR / "MLModelWeights.csv"
    out: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("SYMBOL"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 33:
            continue
        n_feat = 18 if len(parts) >= 57 else 10
        sym = parts[0].upper()
        out[sym] = {
            "intercept": float(parts[1]),
            "ml_min_prob": float(parts[2]),
            "n_features": n_feat,
            "means": [float(x) for x in parts[3 : 3 + n_feat]],
            "stds": [float(x) for x in parts[3 + n_feat : 3 + 2 * n_feat]],
            "weights": [float(x) for x in parts[3 + 2 * n_feat : 3 + 3 * n_feat]],
        }
    return out


def _eval_ml_prob(model: Optional[Dict[str, Any]], feats: List[float]) -> Optional[float]:
    if not model:
        return None
    z = float(model["intercept"])
    n_f = min(len(feats), len(model["weights"]), len(model["means"]), len(model["stds"]))
    for i in range(n_f):
        s = model["stds"][i] if abs(model["stds"][i]) > 1e-9 else 1.0
        x_norm = (feats[i] - model["means"][i]) / s
        z += model["weights"][i] * x_norm
    z = max(-20.0, min(20.0, z))
    return float(1.0 / (1.0 + math.exp(-z)))


def _resample_1h(df_15m: pd.DataFrame) -> pd.DataFrame:
    tmp = df_15m.set_index("time")
    return (
        tmp.resample("1h", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def _trim_to_closed_bars(df: pd.DataFrame, bar_minutes: int, now_utc: pd.Timestamp) -> pd.DataFrame:
    """Filter to strictly closed grid-aligned bars and strip flat zero-volume post-close ticks."""
    if df is None or df.empty:
        return pd.DataFrame()
    closed = df[df["time"] + pd.Timedelta(minutes=bar_minutes) <= now_utc].copy()
    if bar_minutes == 15 and not closed.empty:
        closed = closed[(closed["time"].dt.second == 0) & (closed["time"].dt.minute % 15 == 0)].copy()
    closed = closed.reset_index(drop=True)
    trimmed = 0
    while trimmed < 3 and len(closed) > 95:
        last_row = closed.iloc[-1]
        if float(last_row["high"]) == float(last_row["low"]) and float(last_row["volume"]) <= 1.0:
            closed = closed.iloc[:-1]
            trimmed += 1
        else:
            break
    return closed.reset_index(drop=True)


def _signal_grade(
    score: int,
    ml_prob: Optional[float],
    ml_thresh: float,
    direction: str,
    macro_bias: int,
) -> str:
    macro_aligned = (direction == "BUY" and macro_bias >= 15) or (direction == "SELL" and macro_bias <= -15)
    if score >= 76 and (ml_prob is None or ml_prob >= ml_thresh + 0.035) and macro_aligned:
        return "A+"
    if score >= 72 and (ml_prob is None or ml_prob >= ml_thresh + 0.015):
        return "A"
    return "B"


def _suggested_risk_pct(
    grade: str,
    oos_exp_r: float,
    ml_prob: Optional[float],
    ml_thresh: float,
) -> float:
    """Quarter-Kelly conviction risk sizing in [0.25%, 1.25%] equity per trade."""
    base = 1.00 if grade == "A+" else (0.75 if grade == "A" else 0.50)
    if oos_exp_r >= 0.08 and (ml_prob is None or ml_prob >= ml_thresh + 0.05):
        base += 0.25
    elif oos_exp_r <= 0.0:
        base *= 0.50
    return round(max(0.25, min(1.25, base)), 2)


def _compute_live_bar_features_and_probs(
    tf_df: pd.DataFrame,
    st: Dict[str, Any],
    idx: int,
    macro_val: int,
    ml_model: Optional[Dict[str, Any]],
    latest_sig_dir: int = 0,
    latest_sig_prob: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float], str]:
    h = tf_df["high"].values.astype(float)
    l = tf_df["low"].values.astype(float)
    c = tf_df["close"].values.astype(float)
    o = tf_df["open"].values.astype(float)
    v = np.maximum(tf_df["volume"].values.astype(float), 1.0)

    rng = h[idx] - l[idx]
    clv_i = (((c[idx] - l[idx]) - (h[idx] - c[idx])) / rng) if rng > 1e-12 else 0.0
    ofi_arr = st["ofi"]
    s_i = max(0, idx - 2)
    sum_vol3 = float(np.sum(v[s_i : idx + 1]))
    net_delta3 = float(np.sum(v[s_i : idx + 1] * ofi_arr[s_i : idx + 1]) / max(sum_vol3, 1.0))

    body = c[idx] - o[idx]
    at = max(float(st["atr"][idx]), 1e-9)
    rvol_i = float(st["rvol"][idx])
    z_i = float(st["vwap_z"][idx])
    vw_i = float(st["vwap"][idx])
    ker_i = float(st["ker"][idx])
    htf_i = int(st["htf_bias"][idx])
    atr_pct_i = float(st["atr_pct"][idx])
    sqz_i = int(st["sqz_on"][idx])
    bar_ts = pd.Timestamp(tf_df["time"].iloc[idx])
    is_london_ny = 1.0 if (7 <= bar_ts.hour <= 20) else 0.0
    body_atr = float(np.clip(abs(body) / at, 0.0, 4.0))

    bull_fvg_disp = 1.0 if ((idx >= 2 and l[idx] > h[idx - 2]) or (body >= 0.65 * at and rvol_i >= 1.25 and clv_i >= 0.50)) else 0.0
    bear_fvg_disp = 1.0 if ((idx >= 2 and h[idx] < l[idx - 2]) or ((-body) >= 0.65 * at and rvol_i >= 1.25 and clv_i <= -0.50)) else 0.0

    def _feats(d: int) -> List[float]:
        return [
            float(d * float(ofi_arr[idx])),
            float(d * net_delta3),
            float(min(rvol_i, 5.0)),
            float(d * z_i),
            float(ker_i),
            float(d * htf_i),
            float(d * clv_i),
            float(atr_pct_i / 100.0),
            float(bull_fvg_disp if d == 1 else bear_fvg_disp),
            float((d * float(macro_val)) / 100.0),
            float(np.clip(d * ((c[idx] - vw_i) / at), -6.0, 6.0)),
            1.10,
            body_atr,
            0.70,
            0.0,
            is_london_ny,
            0.0,
            0.0,
        ]

    p_buy = _eval_ml_prob(ml_model, _feats(1))
    p_sell = _eval_ml_prob(ml_model, _feats(-1))

    if latest_sig_dir == 1 and latest_sig_prob is not None:
        p_buy = float(latest_sig_prob)
    elif latest_sig_dir == -1 and latest_sig_prob is not None:
        p_sell = float(latest_sig_prob)

    if latest_sig_dir == 1:
        status = "CONFIRMED BUY"
    elif latest_sig_dir == -1:
        status = "CONFIRMED SELL"
    elif sqz_i == 1:
        status = "SQZ COILING"
    elif abs(z_i) >= 1.1 and abs(float(ofi_arr[idx])) >= 0.15:
        status = "NEAR SWEEP"
    elif htf_i != 0 and ker_i >= 0.25:
        status = "TREND WATCH"
    else:
        status = "WAIT"

    return (
        round(p_buy, 3) if p_buy is not None else None,
        round(p_sell, 3) if p_sell is not None else None,
        status,
    )


def _grade_ledger_for_series(
    entries: List[Dict[str, Any]],
    sym: str,
    tf_name: str,
    tf_df: pd.DataFrame,
    tp1_weight: float = 0.65,
    runner_sl_r: float = -0.50,
    max_hold: int = 48,
    cost_r: float = 0.05,
) -> None:
    """
    Auto-grade OPEN live signal ledger entries using strictly subsequent closed bars
    with conservative SL-first intra-bar tie-breaking matching label_candidates().
    """
    if tf_df.empty:
        return
    tf_times = pd.to_datetime(tf_df["time"], utc=True)
    h_arr = tf_df["high"].values.astype(float)
    l_arr = tf_df["low"].values.astype(float)
    c_arr = tf_df["close"].values.astype(float)
    w2 = 1.0 - tp1_weight

    for ent in entries:
        if ent.get("status") != "OPEN":
            continue
        if str(ent.get("symbol", "")).upper() != sym or str(ent.get("timeframe", "")).upper() != tf_name:
            continue
        ent_ts = pd.Timestamp(ent["bar_time_utc"])
        if ent_ts.tzinfo is None:
            ent_ts = ent_ts.tz_localize("UTC")

        sub_idx = np.where(tf_times.values > np.datetime64(ent_ts.tz_convert("UTC").to_datetime64()))[0]
        if len(sub_idx) == 0:
            continue

        d = 1 if str(ent.get("direction", "BUY")).upper() == "BUY" else -1
        entry_p = float(ent["entry"])
        sl_p = float(ent["sl"])
        tp1_p = float(ent["tp1"])
        tp2_p = float(ent["tp2"])
        risk_p = max(float(ent.get("risk") or abs(entry_p - sl_p)), 1e-10)
        tp1_r = max(0.10, ((tp1_p - entry_p) * d) / risk_p)
        tp2_r = max(tp1_r, ((tp2_p - entry_p) * d) / risk_p)

        hit_tp1 = False
        hit_tp2 = False
        resolved_sl = False
        cur_sl = sl_p
        resolved_pos = None

        steps = min(max_hold, len(sub_idx))
        for j in range(steps):
            k = int(sub_idx[j])
            if d == 1:
                if l_arr[k] <= cur_sl:
                    resolved_sl = True
                    resolved_pos = k
                    break
                if h_arr[k] >= tp2_p:
                    hit_tp1 = True
                    hit_tp2 = True
                    resolved_pos = k
                    break
                if h_arr[k] >= tp1_p and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = entry_p + d * runner_sl_r * risk_p
            else:
                if h_arr[k] >= cur_sl:
                    resolved_sl = True
                    resolved_pos = k
                    break
                if l_arr[k] <= tp2_p:
                    hit_tp1 = True
                    hit_tp2 = True
                    resolved_pos = k
                    break
                if l_arr[k] <= tp1_p and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = entry_p + d * runner_sl_r * risk_p

        ent["hit_tp1"] = bool(hit_tp1)
        ent["cur_sl"] = round(float(cur_sl), 6)

        if hit_tp2:
            r_net = tp1_weight * tp1_r + w2 * tp2_r - cost_r
            ent["status"] = "WIN_TP2"
            ent["r_net"] = round(float(r_net), 4)
            ent["resolved_utc"] = pd.Timestamp(tf_df["time"].iloc[int(resolved_pos)]).strftime("%Y-%m-%dT%H:%MZ")
        elif resolved_sl:
            if hit_tp1:
                r_net = tp1_weight * tp1_r + w2 * runner_sl_r - cost_r
                ent["status"] = "WIN_TP1_PROTECTED" if r_net > 0 else "LOSS_SL"
            else:
                r_net = -1.0 - cost_r
                ent["status"] = "LOSS_SL"
            ent["r_net"] = round(float(r_net), 4)
            ent["resolved_utc"] = pd.Timestamp(tf_df["time"].iloc[int(resolved_pos)]).strftime("%Y-%m-%dT%H:%MZ")
        elif len(sub_idx) >= max_hold:
            last_k = int(sub_idx[max_hold - 1])
            mtm = ((c_arr[last_k] - entry_p) * d) / risk_p
            if hit_tp1:
                mtm = max(runner_sl_r, min(tp2_r, mtm))
                r_net = tp1_weight * tp1_r + w2 * mtm - cost_r
            else:
                mtm = max(-1.0, min(tp1_r, mtm))
                r_net = mtm - cost_r
            ent["status"] = "WIN_EXPIRY" if r_net > 0 else "LOSS_EXPIRY"
            ent["r_net"] = round(float(r_net), 4)
            ent["resolved_utc"] = pd.Timestamp(tf_df["time"].iloc[last_k]).strftime("%Y-%m-%dT%H:%MZ")


def _build_live_outcome_audit(entries: List[Dict[str, Any]], now_dt: pd.Timestamp) -> Dict[str, Any]:
    open_cnt = sum(1 for e in entries if e.get("status") == "OPEN")
    graded = [e for e in entries if e.get("status") != "OPEN" and e.get("r_net") is not None]
    graded.sort(key=lambda e: str(e.get("resolved_utc") or e.get("bar_time_utc") or ""))

    cutoff_30d = now_dt - pd.Timedelta(days=30)
    graded_30d = 0
    for e in graded:
        ts = pd.Timestamp(e.get("resolved_utc") or e.get("bar_time_utc"))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts >= cutoff_30d:
            graded_30d += 1

    if graded:
        r_vals = [float(e["r_net"]) for e in graded]
        wins = [rv > 0.0 for rv in r_vals]
        live_win_pct = round(100.0 * float(np.mean(wins)), 1)
        live_exp_r = round(float(np.mean(r_vals)), 3)
        gp = float(sum(rv for rv in r_vals if rv > 0.0))
        gl = float(abs(sum(rv for rv in r_vals if rv < 0.0)))
        live_pf = round(min(9.99, gp / max(gl, 1e-6)), 2) if gp > 0.0 else 0.0
        tp1_hit_pct = round(100.0 * float(np.mean([bool(e.get("hit_tp1")) for e in graded])), 1)
    else:
        live_win_pct = None
        live_exp_r = None
        live_pf = None
        tp1_hit_pct = None

    recent20 = graded[-20:]
    if len(recent20) < 10:
        drift_status = "AWAITING_SAMPLES"
    else:
        r20 = [float(e["r_net"]) for e in recent20]
        w20 = 100.0 * float(np.mean([rv > 0.0 for rv in r20]))
        e20 = float(np.mean(r20))
        drift_status = "DRIFT_ALERT" if (w20 < 60.0 or e20 < -0.05) else "NOMINAL"

    recent_trimmed = [
        {
            "symbol": e.get("symbol"),
            "timeframe": e.get("timeframe"),
            "bar_time_utc": e.get("bar_time_utc"),
            "direction": e.get("direction"),
            "playbook": e.get("playbook"),
            "ml_prob": e.get("ml_prob"),
            "status": e.get("status"),
            "r_net": e.get("r_net"),
            "resolved_utc": e.get("resolved_utc"),
        }
        for e in reversed(graded[-10:])
    ]

    return {
        "open_count": open_cnt,
        "graded_total": len(graded),
        "graded_30d": graded_30d,
        "live_win_pct": live_win_pct,
        "live_exp_r": live_exp_r,
        "live_pf": live_pf,
        "tp1_hit_pct": tp1_hit_pct,
        "drift_status": drift_status,
        "recent_graded": recent_trimmed,
    }


def _send_notifications(new_alerts: List[Dict[str, Any]]) -> None:
    if not new_alerts:
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    lines = ["*LeadingConfluencePro v3.6 — Confirmed Live Signals*"]
    for a in new_alerts:
        prob_txt = f" | ML {a['ml_prob']*100:.0f}%" if a.get("ml_prob") is not None else ""
        grade_txt = f" [{a.get('grade', 'B')}]"
        risk_txt = f" | Risk: `{a.get('suggested_risk_pct', 0.50):.2f}%`" if a.get("suggested_risk_pct") is not None else ""
        lines.append(
            f"• *{a['symbol']} {a['timeframe']}* `{a['direction']}` ({a['playbook']}){grade_txt} "
            f"| Score `{a['score']}/100`{prob_txt} | Macro `{a['macro_bias']:+d}`{risk_txt}\n"
            f"  Entry: `{a['entry']:.5g}` | SL: `{a['sl']:.5g}` ({a.get('sl_dist_pct', 0):.2f}%) | TP1(65%+rSL): `{a['tp1']:.5g}` | TP2: `{a['tp2']:.5g}`"
        )
    text_msg = "\n".join(lines)

    if tg_token and tg_chat:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": text_msg, "parse_mode": "Markdown"},
                timeout=10,
            )
            ok_val = None
            try:
                ok_val = resp.json().get("ok")
            except Exception:
                pass
            print(f"Telegram notify HTTP {resp.status_code} ok={ok_val}")
        except Exception as exc:
            print(f"Telegram notify error: {type(exc).__name__}")

    if discord_url:
        try:
            requests.post(discord_url, json={"content": text_msg.replace("*", "**")}, timeout=10)
        except Exception:
            pass


def run_live_scan() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_dt = pd.Timestamp(datetime.now(timezone.utc))
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    macro_map = _load_macro_bias_map()
    cfg_map = _load_optimized_configs()
    ml_map = _load_ml_weights()

    state_file = DATA_DIR / "alert_state.json"
    sent_keys: List[str] = []
    if state_file.exists():
        try:
            sent_keys = json.loads(state_file.read_text(encoding="utf-8")).get("sent_keys", [])
        except Exception:
            sent_keys = []
    sent_set = set(sent_keys)

    ledger_file = DATA_DIR / "signal_outcomes_ledger.json"
    ledger_entries: List[Dict[str, Any]] = []
    if ledger_file.exists():
        try:
            loaded_l = json.loads(ledger_file.read_text(encoding="utf-8"))
            if isinstance(loaded_l, dict) and isinstance(loaded_l.get("entries"), list):
                ledger_entries = [e for e in loaded_l["entries"] if isinstance(e, dict) and e.get("sig_id")]
        except Exception:
            ledger_entries = []
    ledger_map: Dict[str, Dict[str, Any]] = {str(e["sig_id"]): e for e in ledger_entries}

    watchlist_telemetry: List[Dict[str, Any]] = []
    confirmed_signals: List[Dict[str, Any]] = []
    closed_market_signals: List[Dict[str, Any]] = []
    new_alerts: List[Dict[str, Any]] = []

    for sym, meta in WATCHLIST.items():
        df_15m_raw = fetch_yahoo_ohlcv(meta["yahoo"], interval="15m", range_period="60d")
        if df_15m_raw is None or df_15m_raw.empty:
            continue

        raw_last_time = pd.Timestamp(df_15m_raw["time"].iloc[-1])
        if raw_last_time.tzinfo is None:
            raw_last_time = raw_last_time.tz_localize("UTC")
        live_price = round(float(df_15m_raw["close"].iloc[-1]), 5)
        live_time_utc = raw_last_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        market_open = bool((now_dt - raw_last_time).total_seconds() <= 35 * 60)

        df_15m = _trim_to_closed_bars(df_15m_raw, 15, now_dt)
        if len(df_15m) < 160:
            continue
        if len(df_15m) > 1400:
            df_15m = df_15m.tail(1400).reset_index(drop=True)
        df_1h = _trim_to_closed_bars(_resample_1h(df_15m), 60, now_dt)

        macro_val, macro_note = macro_map.get(sym, macro_map.get("GLOBAL", (0, "Neutral")))
        ml_model = ml_map.get(sym, ml_map.get("GLOBAL"))

        for tf_name, tf_df, base_tf in [("M15", df_15m, "15m"), ("H1", df_1h, "1h")]:
            if len(tf_df) < 90:
                continue

            # Grade OPEN ledger outcomes on this (sym, tf_name) series first
            _grade_ledger_for_series(ledger_entries, sym, tf_name, tf_df)

            cfg = cfg_map.get(
                (sym, tf_name),
                cfg_map.get((sym, "M15"), cfg_map.get((sym, "H1"), cfg_map.get(("GLOBAL", "ALL"), {}))),
            )
            params = EngineParams(
                min_score=int(cfg.get("min_score", 65)),
                vwap_period=int(cfg.get("vwap_period", 50)),
                sl_buffer_atr=float(cfg.get("sl_buffer_atr", 0.25)),
                cooldown_bars=4,
                allow_consecutive=True,
                min_sl_atr=1.10,
                tp1_mult=float(cfg.get("tp1_mult", 0.50)),
                tp2_mult=float(cfg.get("tp2_mult", 3.00)),
                en_sweep=bool(cfg.get("en_sweep", True)),
                en_squeeze=bool(cfg.get("en_squeeze", True)),
                en_pullback=bool(cfg.get("en_pullback", True)),
            )
            oos_exp_r = float(cfg.get("oos_exp_r", 0.0))
            wf_status = str(cfg.get("status", "GLOBAL_MODEL"))
            ml_thresh = float(cfg.get("ml_min_prob", ml_model["ml_min_prob"] if ml_model else 0.65))

            macro_arr = np.full(len(tf_df), float(macro_val), dtype=float)
            res = compute_indicators_and_candidates(
                tf_df,
                params,
                base_tf=base_tf,
                macro_bias_arr=macro_arr,
                use_macro_score=True,
                include_last_bar=True,
            )

            last_closed_idx = len(tf_df) - 1
            st = res["state"]
            latest_sig_dir = 0
            latest_sig_prob: Optional[float] = None

            max_dash_bars = 5 if tf_name == "M15" else 2
            max_dash_age = (3 * 3600) if tf_name == "M15" else (6 * 3600)
            max_alert_bars = 3 if tf_name == "M15" else 1
            max_alert_age = (90 * 60) if tf_name == "M15" else (180 * 60)

            for cand in res["candidates"]:
                prob = _eval_ml_prob(ml_model, cand["features"])
                ml_passed = True if (prob is None or prob >= ml_thresh) else False
                if not ml_passed:
                    continue

                if cand["bar_idx"] == last_closed_idx:
                    latest_sig_dir = int(cand["dir"])
                    latest_sig_prob = prob

                cand_ts = pd.Timestamp(cand["time"])
                if cand_ts.tzinfo is None:
                    cand_ts = cand_ts.tz_localize("UTC")
                bar_age_sec = (now_dt - cand_ts).total_seconds()
                dir_str = "BUY" if cand["dir"] == 1 else "SELL"
                entry_val = float(cand["entry"])
                sl_val = float(cand["sl"])
                sl_dist_pct = round(abs(entry_val - sl_val) / max(abs(entry_val), 1e-9) * 100.0, 2)
                grade = _signal_grade(int(cand["score"]), prob, ml_thresh, dir_str, int(macro_val))
                sug_risk = _suggested_risk_pct(grade, oos_exp_r, prob, ml_thresh)

                sig_obj = {
                    "symbol": sym,
                    "timeframe": tf_name,
                    "bar_time_utc": cand["time"].strftime("%Y-%m-%dT%H:%MZ"),
                    "is_fresh_closed_bar": bool(cand["bar_idx"] == last_closed_idx),
                    "market_open": market_open,
                    "direction": dir_str,
                    "playbook": cand["playbook"],
                    "grade": grade,
                    "suggested_risk_pct": sug_risk,
                    "wf_status": wf_status,
                    "score": int(cand["score"]),
                    "ml_prob": round(prob, 3) if prob is not None else None,
                    "macro_bias": int(macro_val),
                    "entry": round(entry_val, 6),
                    "sl": round(sl_val, 6),
                    "sl_dist_pct": sl_dist_pct,
                    "tp1": round(float(cand["tp1"]), 6),
                    "tp2": round(float(cand["tp2"]), 6),
                }

                if market_open:
                    if cand["bar_idx"] >= last_closed_idx - max_dash_bars and bar_age_sec <= max_dash_age:
                        confirmed_signals.append(sig_obj)
                    if cand["bar_idx"] >= last_closed_idx - max_alert_bars and bar_age_sec <= max_alert_age:
                        alert_key = f"{sym}|{tf_name}|{sig_obj['bar_time_utc']}|{dir_str}"
                        if alert_key not in ledger_map:
                            new_l_ent = {
                                "sig_id": alert_key,
                                "symbol": sym,
                                "asset_class": meta["asset_class"],
                                "timeframe": tf_name,
                                "bar_time_utc": sig_obj["bar_time_utc"],
                                "direction": dir_str,
                                "playbook": cand["playbook"],
                                "score": int(cand["score"]),
                                "grade": grade,
                                "ml_prob": sig_obj["ml_prob"],
                                "ml_thresh": round(ml_thresh, 3),
                                "entry": sig_obj["entry"],
                                "sl": sig_obj["sl"],
                                "tp1": sig_obj["tp1"],
                                "tp2": sig_obj["tp2"],
                                "risk": round(float(cand.get("risk", abs(entry_val - sl_val))), 6),
                                "features": [round(float(x), 6) for x in cand["features"]],
                                "status": "OPEN",
                                "hit_tp1": False,
                                "cur_sl": sig_obj["sl"],
                                "r_net": None,
                                "resolved_utc": None,
                            }
                            ledger_map[alert_key] = new_l_ent
                            ledger_entries.append(new_l_ent)
                            # Grade immediately if later bars already closed after this signal within max_alert_bars
                            _grade_ledger_for_series([new_l_ent], sym, tf_name, tf_df)
                        if alert_key not in sent_set:
                            sent_set.add(alert_key)
                            sent_keys.append(alert_key)
                            new_alerts.append(sig_obj)
                else:
                    if cand["bar_idx"] >= last_closed_idx - 2:
                        closed_market_signals.append(sig_obj)

            if tf_name == "M15":
                p_buy, p_sell, setup_status = _compute_live_bar_features_and_probs(
                    tf_df,
                    st,
                    last_closed_idx,
                    int(macro_val),
                    ml_model,
                    latest_sig_dir=latest_sig_dir,
                    latest_sig_prob=latest_sig_prob,
                )
                watchlist_telemetry.append(
                    {
                        "symbol": sym,
                        "timeframe": "M15",
                        "asset_class": meta["asset_class"],
                        "close": round(float(tf_df["close"].iloc[last_closed_idx]), 5),
                        "live_price": live_price,
                        "live_time_utc": live_time_utc,
                        "market_open": market_open,
                        "bar_time_utc": pd.Timestamp(tf_df["time"].iloc[last_closed_idx]).strftime("%Y-%m-%dT%H:%MZ"),
                        "macro_bias": int(macro_val),
                        "macro_note": macro_note,
                        "ofi": round(float(st["ofi"][last_closed_idx]), 3),
                        "rvol": round(float(st["rvol"][last_closed_idx]), 2),
                        "vwap_z": round(float(st["vwap_z"][last_closed_idx]), 2),
                        "ker": round(float(st["ker"][last_closed_idx]), 2),
                        "atr_pct": round(float(st["atr_pct"][last_closed_idx]), 1),
                        "sqz_on": int(st["sqz_on"][last_closed_idx]),
                        "htf_bias": int(st["htf_bias"][last_closed_idx]),
                        "min_score": params.min_score,
                        "ml_min_prob": round(ml_thresh, 3),
                        "ml_buy_prob": p_buy,
                        "ml_sell_prob": p_sell,
                        "setup_status": setup_status,
                    }
                )

    ledger_entries.sort(key=lambda e: str(e.get("bar_time_utc", "")))
    ledger_entries = ledger_entries[-1500:]
    ledger_file.write_text(
        json.dumps({"updated_utc": now_iso, "entries": ledger_entries}, indent=2),
        encoding="utf-8",
    )
    live_outcome_audit = _build_live_outcome_audit(ledger_entries, now_dt)

    _send_notifications(new_alerts)
    sent_keys = sent_keys[-500:]
    state_file.write_text(json.dumps({"sent_keys": sent_keys}, indent=2), encoding="utf-8")

    output = {
        "updated_utc": now_iso,
        "watchlist_count": len(watchlist_telemetry),
        "fresh_alerts_dispatched": len(new_alerts),
        "recent_confirmed_signals": confirmed_signals,
        "closed_market_last_signals": closed_market_signals,
        "live_outcome_audit": live_outcome_audit,
        "telemetry": watchlist_telemetry,
    }
    (DATA_DIR / "latest_signals.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    res = run_live_scan()
    audit = res.get("live_outcome_audit", {})
    print(
        f"[{res['updated_utc']}] Scanned {res['watchlist_count']} symbols | "
        f"Recent signals: {len(res['recent_confirmed_signals'])} | "
        f"New alerts: {res['fresh_alerts_dispatched']} | "
        f"Ledger: open={audit.get('open_count', 0)}, graded={audit.get('graded_total', 0)}"
    )
