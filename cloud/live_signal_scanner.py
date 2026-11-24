"""
24/7 Cloud Live Signal Scanner & Optional Telegram / Discord Notifier.
- Loads MacroFlowBias.csv, OptimizedConfig.csv, and MLModelWeights.csv
- Fetches fresh 5m, 15m (resampled to 30m & 1h; H1->H4 for SSRN/HTF) for all 10 symbols
- Evaluates strictly closed bars using LeadingConfluencePro v2.10 logic + ML gate
- Writes data/latest_signals.json and deduplicates alerts via data/alert_state.json
- Only records alert_state keys after Telegram/Discord confirms HTTP 200 ok=True
"""

from __future__ import annotations

import html
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
        sym = parts[0].upper()
        out[sym] = {
            "intercept": float(parts[1]),
            "ml_min_prob": float(parts[2]),
            "means": [float(x) for x in parts[3:13]],
            "stds": [float(x) for x in parts[13:23]],
            "weights": [float(x) for x in parts[23:33]],
        }
    return out


def _eval_ml_prob(model: Optional[Dict[str, Any]], feats: List[float]) -> Optional[float]:
    if not model:
        return None
    z = float(model["intercept"])
    for i in range(10):
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


def _resample_30m(df_15m: pd.DataFrame) -> pd.DataFrame:
    tmp = df_15m.set_index("time")
    return (
        tmp.resample("30min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def _trim_to_closed_bars(df: pd.DataFrame, bar_minutes: int, now_utc: pd.Timestamp) -> pd.DataFrame:
    """Filter to strictly closed grid-aligned bars and strip flat zero-volume post-close ticks."""
    if df is None or df.empty:
        return pd.DataFrame()
    closed = df[df["time"] + pd.Timedelta(minutes=bar_minutes) <= now_utc].copy()
    if bar_minutes in (5, 15) and not closed.empty:
        closed = closed[(closed["time"].dt.second == 0) & (closed["time"].dt.minute % bar_minutes == 0)].copy()
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
    if score >= 80 and (ml_prob is None or ml_prob >= ml_thresh + 0.05) and macro_aligned:
        return "A+"
    if score >= 72 and (ml_prob is None or ml_prob >= ml_thresh + 0.02):
        return "A"
    return "B"


def _compute_live_bar_features_and_probs(
    tf_df: pd.DataFrame,
    st: Dict[str, Any],
    idx: int,
    macro_val: int,
    ml_model: Optional[Dict[str, Any]],
    latest_sig_dir: int = 0,
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
    at = float(st["atr"][idx])
    rvol_i = float(st["rvol"][idx])
    z_i = float(st["vwap_z"][idx])
    ker_i = float(st["ker"][idx])
    htf_i = int(st["htf_bias"][idx])
    atr_pct_i = float(st["atr_pct"][idx])
    sqz_i = int(st["sqz_on"][idx])

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
        ]

    p_buy = _eval_ml_prob(ml_model, _feats(1))
    p_sell = _eval_ml_prob(ml_model, _feats(-1))

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


def _send_notifications(new_alerts: List[Dict[str, Any]]) -> bool:
    if not new_alerts:
        return False

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if not (tg_token and tg_chat) and not discord_url:
        print("Telegram skipped (no credentials in env); alert_state.json NOT modified")
        return False

    any_sent = False

    if tg_token and tg_chat:
        sorted_alerts = sorted(new_alerts, key=lambda x: x.get("bar_time_utc", ""))
        chunk_size = 8
        tg_all_ok = True
        for start in range(0, len(sorted_alerts), chunk_size):
            batch = sorted_alerts[start : start + chunk_size]
            lines = ["<b>LeadingConfluencePro v2.10 — Confirmed Live Signals</b>"]
            for a in batch:
                sym_h = html.escape(str(a["symbol"]))
                tf_h = html.escape(str(a["timeframe"]))
                dir_h = html.escape(str(a["direction"]))
                pb_h = html.escape(str(a["playbook"]))
                gr_h = html.escape(str(a.get("grade", "B")))
                bar_h = html.escape(str(a.get("bar_time_utc", "")))
                prob_txt = f" | ML {a['ml_prob']*100:.0f}%" if a.get("ml_prob") is not None else ""
                ssrn_v = int(a.get("ssrn_regime", 0) or 0)
                ssrn_txt = "BULL" if ssrn_v > 0 else ("BEAR" if ssrn_v < 0 else "NEUT")
                lines.append(
                    f"• <b>{sym_h} {tf_h}</b> <code>{dir_h}</code> ({pb_h}) [{gr_h}] "
                    f"| Score <code>{a['score']}/100</code>{prob_txt} | Macro <code>{a['macro_bias']:+d}</code> | SSRN {ssrn_txt}\n"
                    f"  Entry: <code>{a['entry']:.5g}</code> | SL: <code>{a['sl']:.5g}</code> ({a.get('sl_dist_pct', 0):.2f}%) "
                    f"| TP1: <code>{a['tp1']:.5g}</code> | TP2: <code>{a['tp2']:.5g}</code> | Bar: <code>{bar_h}</code>"
                )
            text_msg = "\n".join(lines)
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={
                        "chat_id": tg_chat,
                        "text": text_msg,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=12,
                )
                ok_val = None
                desc = ""
                try:
                    j = resp.json()
                    ok_val = j.get("ok")
                    desc = str(j.get("description", "")) if not ok_val else ""
                except Exception:
                    pass
                print(f"Telegram notify HTTP {resp.status_code} ok={ok_val}" + (f" desc={desc}" if desc else ""))
                if not (resp.status_code == 200 and ok_val is True):
                    tg_all_ok = False
            except Exception as exc:
                print(f"Telegram notify error: {type(exc).__name__}")
                tg_all_ok = False
        if tg_all_ok:
            any_sent = True

    if discord_url:
        try:
            dlines = ["**LeadingConfluencePro v2.10 — Confirmed Live Signals**"]
            for a in new_alerts[:10]:
                prob_txt = f" | ML {a['ml_prob']*100:.0f}%" if a.get("ml_prob") is not None else ""
                dlines.append(
                    f"• **{a['symbol']} {a['timeframe']}** `{a['direction']}` ({a['playbook']}) [{a.get('grade', 'B')}] "
                    f"| Score `{a['score']}/100`{prob_txt} | Entry `{a['entry']:.5g}` | SL `{a['sl']:.5g}` | TP1 `{a['tp1']:.5g}` | Bar `{a['bar_time_utc']}`"
                )
            d_resp = requests.post(discord_url, json={"content": "\n".join(dlines)}, timeout=10)
            if d_resp.status_code in (200, 204):
                any_sent = True
        except Exception:
            pass

    return any_sent


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

    watchlist_telemetry: List[Dict[str, Any]] = []
    confirmed_signals: List[Dict[str, Any]] = []
    closed_market_signals: List[Dict[str, Any]] = []
    new_alerts: List[Dict[str, Any]] = []
    pending_alert_keys: List[str] = []
    pending_set = set()

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
        if len(df_15m) > 3600:
            df_15m = df_15m.tail(3600).reset_index(drop=True)
        df_1h = _trim_to_closed_bars(_resample_1h(df_15m), 60, now_dt)
        df_30m = _trim_to_closed_bars(_resample_30m(df_15m), 30, now_dt)

        df_5m_raw = fetch_yahoo_ohlcv(meta["yahoo"], interval="5m", range_period="5d")
        df_5m = _trim_to_closed_bars(df_5m_raw, 5, now_dt) if df_5m_raw is not None else pd.DataFrame()

        macro_val, macro_note = macro_map.get(sym, macro_map.get("GLOBAL", (0, "Neutral")))
        ml_model = ml_map.get(sym, ml_map.get("GLOBAL"))

        tf_specs = [
            ("M5", df_5m, "5m", df_30m),
            ("M15", df_15m, "15m", None),
            ("H1", df_1h, "1h", None),
        ]

        for tf_name, tf_df, base_tf, htf_override in tf_specs:
            if tf_df is None or len(tf_df) < 90:
                continue

            cfg = cfg_map.get(
                (sym, tf_name),
                cfg_map.get((sym, "ALL"), cfg_map.get(("GLOBAL", "ALL"), {})),
            )
            params = EngineParams(
                min_score=int(cfg.get("min_score", 68)),
                vwap_period=int(cfg.get("vwap_period", 50)),
                sl_buffer_atr=float(cfg.get("sl_buffer_atr", 0.25)),
                tp1_mult=float(cfg.get("tp1_mult", 1.5)),
                tp2_mult=float(cfg.get("tp2_mult", 2.5)),
                en_sweep=bool(cfg.get("en_sweep", True)),
                en_squeeze=bool(cfg.get("en_squeeze", True)),
                en_pullback=bool(cfg.get("en_pullback", True)),
            )
            ml_thresh = float(cfg.get("ml_min_prob", ml_model["ml_min_prob"] if ml_model else 0.50))

            macro_arr = np.full(len(tf_df), float(macro_val), dtype=float)
            res = compute_indicators_and_candidates(
                tf_df,
                params,
                base_tf=base_tf,
                macro_bias_arr=macro_arr,
                use_macro_score=True,
                include_last_bar=True,
                htf_df_override=htf_override,
            )

            last_closed_idx = len(tf_df) - 1
            st = res["state"]
            latest_sig_dir = 0

            if tf_name == "M5":
                max_dash_bars, max_dash_age = 36, 3.5 * 3600
                max_alert_bars, max_alert_age = 18, 105 * 60
            elif tf_name == "M15":
                max_dash_bars, max_dash_age = 24, 6.5 * 3600
                max_alert_bars, max_alert_age = 10, 165 * 60
            else:
                max_dash_bars, max_dash_age = 10, 12 * 3600
                max_alert_bars, max_alert_age = 3, 240 * 60

            for cand in res["candidates"]:
                prob = _eval_ml_prob(ml_model, cand["features"])
                ml_passed = True if (prob is None or prob >= ml_thresh) else False
                if not ml_passed:
                    continue

                if cand["bar_idx"] == last_closed_idx:
                    latest_sig_dir = int(cand["dir"])

                cand_ts = pd.Timestamp(cand["time"])
                if cand_ts.tzinfo is None:
                    cand_ts = cand_ts.tz_localize("UTC")
                bar_age_sec = (now_dt - cand_ts).total_seconds()
                dir_str = "BUY" if cand["dir"] == 1 else "SELL"
                entry_val = float(cand["entry"])
                sl_val = float(cand["sl"])
                sl_dist_pct = round(abs(entry_val - sl_val) / max(abs(entry_val), 1e-9) * 100.0, 2)
                grade = _signal_grade(int(cand["score"]), prob, ml_thresh, dir_str, int(macro_val))

                sig_obj = {
                    "symbol": sym,
                    "timeframe": tf_name,
                    "bar_time_utc": cand["time"].strftime("%Y-%m-%dT%H:%MZ"),
                    "is_fresh_closed_bar": bool(cand["bar_idx"] == last_closed_idx),
                    "market_open": market_open,
                    "direction": dir_str,
                    "playbook": cand["playbook"],
                    "grade": grade,
                    "score": int(cand["score"]),
                    "ml_prob": round(prob, 3) if prob is not None else None,
                    "macro_bias": int(macro_val),
                    "ssrn_regime": int(cand.get("ssrn_regime", 0)),
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
                        if alert_key not in sent_set and alert_key not in pending_set:
                            pending_set.add(alert_key)
                            pending_alert_keys.append(alert_key)
                            new_alerts.append(sig_obj)
                else:
                    if cand["bar_idx"] >= last_closed_idx - 2:
                        closed_market_signals.append(sig_obj)

            if tf_name == "M15":
                p_buy, p_sell, setup_status = _compute_live_bar_features_and_probs(
                    tf_df, st, last_closed_idx, int(macro_val), ml_model, latest_sig_dir=latest_sig_dir
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
                        "ssrn_regime": int(st["ssrn_regime"][last_closed_idx]),
                        "ssrn_sep_atr": round(float(st["ssrn_sep_atr"][last_closed_idx]), 2),
                        "min_score": params.min_score,
                        "ml_min_prob": round(ml_thresh, 2),
                        "ml_buy_prob": p_buy,
                        "ml_sell_prob": p_sell,
                        "setup_status": setup_status,
                    }
                )

    dispatched_ok = _send_notifications(new_alerts)
    if dispatched_ok and pending_alert_keys:
        sent_keys.extend(pending_alert_keys)
        sent_keys = sent_keys[-500:]
        state_file.write_text(json.dumps({"sent_keys": sent_keys}, indent=2), encoding="utf-8")

    confirmed_signals.sort(key=lambda x: x.get("bar_time_utc", ""), reverse=True)

    output = {
        "updated_utc": now_iso,
        "watchlist_count": len(watchlist_telemetry),
        "fresh_alerts_dispatched": len(new_alerts) if dispatched_ok else 0,
        "recent_confirmed_signals": confirmed_signals,
        "closed_market_last_signals": closed_market_signals,
        "telemetry": watchlist_telemetry,
    }
    (DATA_DIR / "latest_signals.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    res = run_live_scan()
    print(
        f"[{res['updated_utc']}] Scanned {res['watchlist_count']} symbols | "
        f"Recent signals: {len(res['recent_confirmed_signals'])} | "
        f"New fresh alerts: {res['fresh_alerts_dispatched']}"
    )
