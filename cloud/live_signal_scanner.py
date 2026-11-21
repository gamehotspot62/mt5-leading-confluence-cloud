"""
24/7 Cloud Live Signal Scanner & Optional Telegram / Discord Notifier.
- Loads MacroFlowBias.csv, OptimizedConfig.csv, and MLModelWeights.csv
- Fetches fresh 1h (also resampled to 4h) candles for all 10 watchlist symbols
- Evaluates strictly closed bars (iloc[-2]) using identical MQL5 v2.00 logic + ML gate
- Writes data/latest_signals.json and deduplicates alerts via data/alert_state.json
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


def _resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    tmp = df_1h.set_index("time")
    return (
        tmp.resample("4h", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def _send_notifications(new_alerts: List[Dict[str, Any]]) -> None:
    if not new_alerts:
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    lines = ["*LeadingConfluencePro Cloud — Confirmed Signals*"]
    for a in new_alerts:
        prob_txt = f" | ML {a['ml_prob']*100:.0f}%" if a.get("ml_prob") is not None else ""
        lines.append(
            f"• *{a['symbol']} {a['timeframe']}* `{a['direction']}` ({a['playbook']}) "
            f"| Score `{a['score']}/100`{prob_txt} | Macro `{a['macro_bias']:+d}`\n"
            f"  Entry: `{a['entry']:.5g}` | SL: `{a['sl']:.5g}` | TP1: `{a['tp1']:.5g}` | TP2: `{a['tp2']:.5g}`"
        )
    text_msg = "\n".join(lines)

    if tg_token and tg_chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": text_msg, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception:
            pass

    if discord_url:
        try:
            requests.post(discord_url, json={"content": text_msg.replace("*", "**")}, timeout=10)
        except Exception:
            pass


def run_live_scan() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    new_alerts: List[Dict[str, Any]] = []

    for sym, meta in WATCHLIST.items():
        df_1h = fetch_yahoo_ohlcv(meta["yahoo"], interval="1h", range_period="6mo")
        if df_1h is None or len(df_1h) < 120:
            continue
        df_4h = _resample_4h(df_1h)

        macro_val, macro_note = macro_map.get(sym, macro_map.get("GLOBAL", (0, "Neutral")))
        ml_model = ml_map.get(sym, ml_map.get("GLOBAL"))

        for tf_name, tf_df in [("H1", df_1h), ("H4", df_4h)]:
            if len(tf_df) < 90:
                continue

            cfg = cfg_map.get(
                (sym, tf_name),
                cfg_map.get((sym, "H1"), cfg_map.get(("GLOBAL", "ALL"), {})),
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
                base_tf=("1h" if tf_name == "H1" else "4h"),
                macro_bias_arr=macro_arr,
                use_macro_score=True,
            )

            last_closed_idx = len(tf_df) - 2
            st = res["state"]
            if tf_name == "H1":
                watchlist_telemetry.append(
                    {
                        "symbol": sym,
                        "asset_class": meta["asset_class"],
                        "close": round(float(tf_df["close"].iloc[last_closed_idx]), 5),
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
                        "ml_min_prob": round(ml_thresh, 2),
                    }
                )

            # Check signals on the latest closed bar or within last 3 closed bars for dashboard visibility
            for cand in res["candidates"]:
                prob = _eval_ml_prob(ml_model, cand["features"])
                ml_passed = True if (prob is None or prob >= ml_thresh) else False
                if not ml_passed:
                    continue

                if cand["bar_idx"] >= last_closed_idx - 2:
                    sig_obj = {
                        "symbol": sym,
                        "timeframe": tf_name,
                        "bar_time_utc": cand["time"].strftime("%Y-%m-%dT%H:%MZ"),
                        "is_fresh_closed_bar": bool(cand["bar_idx"] == last_closed_idx),
                        "direction": "BUY" if cand["dir"] == 1 else "SELL",
                        "playbook": cand["playbook"],
                        "score": int(cand["score"]),
                        "ml_prob": round(prob, 3) if prob is not None else None,
                        "macro_bias": int(macro_val),
                        "entry": round(float(cand["entry"]), 6),
                        "sl": round(float(cand["sl"]), 6),
                        "tp1": round(float(cand["tp1"]), 6),
                        "tp2": round(float(cand["tp2"]), 6),
                    }
                    confirmed_signals.append(sig_obj)

                    if cand["bar_idx"] == last_closed_idx:
                        alert_key = f"{sym}|{tf_name}|{sig_obj['bar_time_utc']}|{sig_obj['direction']}"
                        if alert_key not in sent_set:
                            sent_set.add(alert_key)
                            sent_keys.append(alert_key)
                            new_alerts.append(sig_obj)

    _send_notifications(new_alerts)
    sent_keys = sent_keys[-500:]
    state_file.write_text(json.dumps({"sent_keys": sent_keys}, indent=2), encoding="utf-8")

    output = {
        "updated_utc": now_iso,
        "watchlist_count": len(watchlist_telemetry),
        "fresh_alerts_dispatched": len(new_alerts),
        "recent_confirmed_signals": confirmed_signals,
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
