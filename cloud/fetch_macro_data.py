"""
Autonomous Cloud Macro / On-Chain / ETF / COT / Volatility Term-Structure Aggregator.
Outputs:
  - data/MacroFlowBias.csv (consumed by MT5 Cloud Bridge Service & Indicator v2)
  - data/macro_snapshot.json (consumed by Live Web Dashboard)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from data_clients import (
    WATCHLIST,
    fetch_coinmetrics,
    fetch_cftc_cot,
    fetch_defillama_stablecoins,
    fetch_deribit_perp,
    fetch_fear_greed,
    fetch_fred_batch,
    fetch_yahoo_ohlcv,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


def _clamp(val: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, val)))


def _etf_flow_score(ticker: str) -> Tuple[float, Dict[str, Any]]:
    """
    Compute 10-day Chaikin-style volume-weighted Money Flow & trend proxy [-25, +25]
    for an ETF ticker from Yahoo Finance daily OHLCV.
    """
    df = fetch_yahoo_ohlcv(ticker, interval="1d", range_period="6mo")
    if df is None or len(df) < 25:
        return 0.0, {"ticker": ticker, "ok": False}

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    v = df["volume"].values

    rng = np.maximum(h - l, 1e-9)
    clv = ((c - l) - (h - c)) / rng
    cmf10 = float(np.sum(clv[-10:] * v[-10:]) / max(np.sum(v[-10:]), 1.0))
    ret10 = float((c[-1] / max(c[-11], 1e-9) - 1.0) * 100.0)

    raw = cmf10 * 45.0 + np.clip(ret10 * 1.5, -12.0, 12.0)
    score = _clamp(raw, -25.0, 25.0)
    return score, {
        "ticker": ticker,
        "ok": True,
        "cmf10": round(cmf10, 3),
        "ret10_pct": round(ret10, 2),
        "score": round(score, 1),
    }


def _ratio_momentum_score(num_ticker: str, den_ticker: str, scale: float = 400.0) -> Tuple[float, Optional[float]]:
    """Compute 20-day return spread of numerator/denominator pair (e.g. RSP/SPY, HYG/TLT)."""
    d1 = fetch_yahoo_ohlcv(num_ticker, interval="1d", range_period="3mo")
    d2 = fetch_yahoo_ohlcv(den_ticker, interval="1d", range_period="3mo")
    if d1 is None or d2 is None or len(d1) < 22 or len(d2) < 22:
        return 0.0, None

    s1 = d1.set_index("time")["close"]
    s2 = d2.set_index("time")["close"]
    joined = pd.concat([s1.rename("n"), s2.rename("d")], axis=1).dropna()
    if len(joined) < 21:
        return 0.0, None
    ratio = joined["n"] / joined["d"].clip(lower=1e-9)
    chg20 = float(ratio.iloc[-1] / ratio.iloc[-21] - 1.0)
    return _clamp(chg20 * scale, -20.0, 20.0), round(chg20 * 100.0, 2)


def _cot_symbol_score(cot_df: Optional[pd.DataFrame], invert: bool = False) -> Tuple[float, Dict[str, Any]]:
    """
    Blend institutional flow momentum with contrarian speculative crowding penalty [-25, +25].
    """
    if cot_df is None or len(cot_df) < 5:
        return 0.0, {"ok": False}

    last = cot_df.iloc[-1]
    inst_pct = float(last["inst_pct"])
    spec_pct = float(last["spec_pct"])
    inst_chg = float(last["inst_chg4w"])

    # Institutional alignment (centered around 50th percentile) + 4w accumulation momentum
    inst_bias = (inst_pct - 50.0) * 0.30 + np.clip(inst_chg * 0.45, -10.0, 10.0)

    # Contrarian speculative crowding adjustment at extremes
    crowd_adj = 0.0
    if spec_pct >= 90.0:
        crowd_adj = -12.0 * ((spec_pct - 90.0) / 10.0 + 0.5)
    elif spec_pct <= 10.0:
        crowd_adj = 12.0 * ((10.0 - spec_pct) / 10.0 + 0.5)

    raw = inst_bias + crowd_adj
    if invert:
        raw = -raw

    score = _clamp(raw, -25.0, 25.0)
    return score, {
        "ok": True,
        "report_date": str(last["date"])[:10],
        "inst_pct": round(inst_pct, 1),
        "spec_pct": round(spec_pct, 1),
        "inst_chg4w": round(inst_chg, 1),
        "inverted": invert,
        "score": round(score, 1),
    }


def _compute_onchain_score(
    cm_df: Optional[pd.DataFrame],
    deribit: Optional[Dict[str, float]],
    stable: Optional[Dict[str, float]],
    fng: Optional[Dict[str, float]],
) -> Tuple[float, List[str], Dict[str, Any]]:
    """
    Compute composite On-Chain + Derivatives + Liquidity score [-70, +70] for BTC/ETH.
    """
    score = 0.0
    tags: List[str] = []
    details: Dict[str, Any] = {}

    if cm_df is not None and len(cm_df) >= 35:
        last = cm_df.iloc[-1]
        mvrv = float(last.get("CapMVRVCur", np.nan))
        if np.isfinite(mvrv):
            if mvrv < 1.15:
                mvrv_pts = 30.0
                tags.append("MVRV_Value")
            elif mvrv < 1.80:
                mvrv_pts = 15.0
                tags.append("MVRV_Healthy")
            elif mvrv <= 2.60:
                mvrv_pts = 0.0
            else:
                mvrv_pts = -30.0
                tags.append("MVRV_Hot")
            score += mvrv_pts
            details["mvrv"] = round(mvrv, 3)
            details["mvrv_pts"] = mvrv_pts

        if "FlowInExUSD" in cm_df.columns and "FlowOutExUSD" in cm_df.columns:
            netflow7 = (cm_df["FlowInExUSD"] - cm_df["FlowOutExUSD"]).tail(7).sum()
            totflow7 = (cm_df["FlowInExUSD"] + cm_df["FlowOutExUSD"]).tail(7).sum()
            nf_ratio = float(netflow7 / max(totflow7, 1.0))
            # Negative netflow (outflows > inflows) = accumulation -> bullish
            nf_pts = _clamp(-nf_ratio * 180.0, -20.0, 20.0)
            score += nf_pts
            details["netflow7_ratio"] = round(nf_ratio, 4)
            details["netflow_pts"] = round(nf_pts, 1)
            tags.append("ExOutflow" if nf_pts >= 5 else ("ExInflow" if nf_pts <= -5 else "ExNeut"))

        if "SplyExNtv" in cm_df.columns and len(cm_df) >= 31:
            cur_sply = float(cm_df["SplyExNtv"].iloc[-1])
            prev_sply = float(cm_df["SplyExNtv"].iloc[-31])
            if np.isfinite(cur_sply) and np.isfinite(prev_sply) and prev_sply > 0:
                sply_chg30 = (cur_sply / prev_sply - 1.0) * 100.0
                sply_pts = _clamp(-sply_chg30 * 3.0, -15.0, 15.0)
                score += sply_pts
                details["ex_supply_30d_pct"] = round(sply_chg30, 2)

    if stable is not None:
        chg30 = float(stable.get("chg_30d_pct", 0.0))
        st_pts = _clamp(chg30 * 3.5, -15.0, 15.0)
        score += st_pts
        details["stablecoins_30d_pct"] = chg30
        if st_pts >= 4:
            tags.append("StableMint")

    if deribit is not None:
        f8 = float(deribit.get("funding_8h", 0.0))
        # Contrarian funding extreme: deeply negative -> squeeze potential (+), extreme positive -> crowded (-)
        if f8 <= -0.00015:
            f_pts = 15.0
            tags.append("NegFundSqueeze")
        elif f8 >= 0.00045:
            f_pts = -15.0
            tags.append("OverheatedFund")
        else:
            f_pts = _clamp(-f8 * 20000.0, -8.0, 8.0)
        score += f_pts
        details["funding_8h"] = round(f8, 6)
        details["funding_pts"] = round(f_pts, 1)

    if fng is not None:
        val = float(fng.get("value", 50.0))
        if val <= 20.0:
            fng_pts = 15.0
            tags.append("ExtremeFearContrarian")
        elif val >= 80.0:
            fng_pts = -15.0
            tags.append("ExtremeGreedCaution")
        else:
            fng_pts = 0.0
        score += fng_pts
        details["fear_greed"] = val

    return _clamp(score, -75.0, 75.0), tags, details


def run_macro_pipeline() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1) Global Volatility Term Structure (VIX / VIX3M)
    vix_df = fetch_yahoo_ohlcv("^VIX", interval="1d", range_period="3mo")
    vix3m_df = fetch_yahoo_ohlcv("^VIX3M", interval="1d", range_period="3mo")
    vix_val = float(vix_df["close"].iloc[-1]) if vix_df is not None and not vix_df.empty else None
    vix3m_val = float(vix3m_df["close"].iloc[-1]) if vix3m_df is not None and not vix3m_df.empty else None
    vix_ratio = (vix_val / vix3m_val) if (vix_val and vix3m_val and vix3m_val > 0) else None

    vix_score = 0.0
    if vix_ratio is not None:
        if vix_ratio < 0.92:
            vix_score = 25.0
        elif vix_ratio <= 1.00:
            vix_score = 10.0
        elif vix_ratio <= 1.08:
            vix_score = -20.0
        else:
            vix_score = -40.0

    # 2) FRED Macro Series (concurrent 8s bounded fetch + Yahoo Treasury/Credit proxies if FRED stalls)
    fred = fetch_fred_batch(
        ["BAMLH0A0HYM2", "DFII10", "T10Y2Y", "WALCL", "RRPONTSYD", "WTREGEN"],
        timeout=8,
    )
    hy_oas = fred.get("BAMLH0A0HYM2")
    dfii10 = fred.get("DFII10")
    t10y2y = fred.get("T10Y2Y")
    walcl = fred.get("WALCL")
    rrp = fred.get("RRPONTSYD")
    tga = fred.get("WTREGEN")

    if hy_oas is not None and len(hy_oas) >= 21:
        hy_latest = float(hy_oas.iloc[-1])
        hy_chg20 = float(hy_oas.iloc[-1] - hy_oas.iloc[-21])
        hy_score = _clamp(-hy_chg20 * 40.0, -25.0, 25.0)
    else:
        hy_latest = None
        hy_score, hy_proxy_chg = _ratio_momentum_score("HYG", "IEI", scale=500.0)
        hy_chg20 = float(-hy_proxy_chg / 20.0) if hy_proxy_chg is not None else 0.0

    if dfii10 is not None and len(dfii10) >= 21:
        real10_latest = float(dfii10.iloc[-1])
        real10_chg20 = float(dfii10.iloc[-1] - dfii10.iloc[-21])
        real10_risk_score = _clamp(-real10_chg20 * 55.0, -25.0, 25.0)
    else:
        real10_latest = None
        tip_df = fetch_yahoo_ohlcv("TIP", interval="1d", range_period="3mo")
        if tip_df is not None and len(tip_df) >= 21:
            tip_ret20 = float((tip_df["close"].iloc[-1] / tip_df["close"].iloc[-21] - 1.0) * 100.0)
            real10_risk_score = _clamp(tip_ret20 * 7.5, -25.0, 25.0)
            real10_chg20 = float(-tip_ret20 * 0.12)
        else:
            real10_chg20 = 0.0
            real10_risk_score = 0.0

    if t10y2y is not None and not t10y2y.empty:
        t10y2y_latest = float(t10y2y.iloc[-1])
    else:
        tnx_df = fetch_yahoo_ohlcv("^TNX", interval="1d", range_period="1mo")
        irx_df = fetch_yahoo_ohlcv("^IRX", interval="1d", range_period="1mo")
        if tnx_df is not None and irx_df is not None and not tnx_df.empty and not irx_df.empty:
            t10y2y_latest = float(tnx_df["close"].iloc[-1] - irx_df["close"].iloc[-1])
        else:
            t10y2y_latest = None

    # Net Fed Liquidity = WALCL (M$) - RRPONTSYD (B$ * 1000) - WTREGEN (M$)
    fed_liq_chg4w_pct = 0.0
    fed_liq_latest_t = None
    if walcl is not None and rrp is not None and tga is not None:
        liq_df = pd.concat(
            [walcl.rename("walcl"), (rrp * 1000.0).rename("rrp"), tga.rename("tga")],
            axis=1,
        ).sort_index().ffill().dropna()
        if len(liq_df) >= 25:
            net_liq = liq_df["walcl"] - liq_df["rrp"] - liq_df["tga"]
            fed_liq_latest_t = round(float(net_liq.iloc[-1]) / 1e6, 3)
            base_liq = float(net_liq.iloc[-21])
            if abs(base_liq) > 1.0:
                fed_liq_chg4w_pct = round((float(net_liq.iloc[-1]) / base_liq - 1.0) * 100.0, 2)
    fed_liq_score = _clamp(fed_liq_chg4w_pct * 6.0, -20.0, 20.0)

    # 3) Cross-Asset Breadth, Credit Risk Appetite, & USD Trend
    breadth_score, breadth_chg = _ratio_momentum_score("RSP", "SPY", scale=450.0)
    hygtlt_score, hygtlt_chg = _ratio_momentum_score("HYG", "TLT", scale=400.0)
    qqqxlp_score, qqqxlp_chg = _ratio_momentum_score("QQQ", "XLP", scale=300.0)

    dxy_df = fetch_yahoo_ohlcv("DX-Y.NYB", interval="1d", range_period="3mo")
    dxy_ret20 = 0.0
    dxy_latest = None
    if dxy_df is not None and len(dxy_df) >= 21:
        dxy_latest = float(dxy_df["close"].iloc[-1])
        dxy_ret20 = float((dxy_df["close"].iloc[-1] / dxy_df["close"].iloc[-21] - 1.0) * 100.0)
    dxy_bull_score = _clamp(dxy_ret20 * 7.0, -25.0, 25.0)

    # 4) Global Composite Liquidity/Risk Regime Score
    global_bias = int(
        round(
            _clamp(
                0.35 * vix_score
                + 0.25 * hy_score
                + 0.20 * fed_liq_score
                + 0.20 * hygtlt_score
                + 0.15 * breadth_score
                + 0.15 * real10_risk_score,
                -100.0,
                100.0,
            )
        )
    )

    # 5) CFTC Commitments of Traders (TFF & Disaggregated)
    tff_codes = [v["cot_code"] for v in WATCHLIST.values() if v["cot_dataset"] == "gpe5-46if" and v["cot_code"]]
    dis_codes = [v["cot_code"] for v in WATCHLIST.values() if v["cot_dataset"] == "72hh-3qpy" and v["cot_code"]]
    cot_map: Dict[str, pd.DataFrame] = {}
    cot_map.update(fetch_cftc_cot("gpe5-46if", tff_codes, limit_per_code=160))
    cot_map.update(fetch_cftc_cot("72hh-3qpy", dis_codes, limit_per_code=160))

    # 6) Crypto Shared & Asset Feeds
    stable_data = fetch_defillama_stablecoins()
    fng_data = fetch_fear_greed(limit=30)
    cm_cache: Dict[str, Optional[pd.DataFrame]] = {
        "btc": fetch_coinmetrics("btc", page_size=120, max_pages=1),
        "eth": fetch_coinmetrics("eth", page_size=120, max_pages=1),
    }
    deribit_cache: Dict[str, Optional[Dict[str, float]]] = {
        "BTC-PERPETUAL": fetch_deribit_perp("BTC-PERPETUAL"),
        "ETH-PERPETUAL": fetch_deribit_perp("ETH-PERPETUAL"),
    }

    # 7) Compute Symbol-Specific Composite Scores
    csv_rows: List[Tuple[str, int, str]] = []
    symbol_details: Dict[str, Any] = {}

    for sym, meta in WATCHLIST.items():
        ac = meta["asset_class"]
        cot_df = cot_map.get(meta["cot_code"]) if meta["cot_code"] else None
        cot_score, cot_meta = _cot_symbol_score(cot_df, invert=bool(meta["cot_invert"]))

        etf_scores: List[float] = []
        etf_meta: List[Dict[str, Any]] = []
        for etf_t in meta.get("etf_tickers", []):
            es, em = _etf_flow_score(etf_t)
            etf_scores.append(es)
            etf_meta.append(em)
        etf_avg = float(np.mean(etf_scores)) if etf_scores else 0.0

        tags: List[str] = []
        if ac == "crypto":
            cm_df = cm_cache.get(meta["cm_asset"])
            der = deribit_cache.get(meta["deribit_inst"])
            onchain_score, onchain_tags, onchain_meta = _compute_onchain_score(
                cm_df, der, stable_data, fng_data
            )
            tags.extend(onchain_tags)
            raw_sym = (
                0.55 * onchain_score
                + 0.65 * etf_avg
                + 0.45 * cot_score
                + 0.35 * global_bias
                + 0.25 * real10_risk_score
                - 0.25 * dxy_bull_score
            )
            extra = {"onchain": onchain_meta}
        elif ac == "metals":
            raw_sym = (
                0.95 * real10_risk_score
                + 0.85 * etf_avg
                + 0.70 * cot_score
                - 0.70 * dxy_bull_score
                + 0.25 * fed_liq_score
            )
            extra = {}
        elif ac == "energy":
            raw_sym = (
                0.80 * cot_score
                + 0.65 * etf_avg
                + 0.45 * hy_score
                + 0.35 * vix_score
                - 0.35 * dxy_bull_score
            )
            extra = {}
        elif ac == "fx":
            if sym == "USDJPY":
                # USDJPY benefits from stronger DXY, rising US real yields, and risk-on VIX contango
                raw_sym = (
                    0.85 * dxy_bull_score
                    + 0.75 * cot_score
                    - 0.75 * real10_risk_score
                    + 0.35 * vix_score
                )
            else:
                # EURUSD / GBPUSD benefit from weaker DXY, positive global risk, and falling US real yields
                raw_sym = (
                    -0.90 * dxy_bull_score
                    + 0.80 * cot_score
                    + 0.40 * real10_risk_score
                    + 0.35 * global_bias
                )
            extra = {}
        else:  # index (US500, US100)
            tech_tilt = qqqxlp_score if sym == "US100" else breadth_score
            raw_sym = (
                0.60 * global_bias
                + 0.70 * cot_score
                + 0.65 * etf_avg
                + 0.55 * tech_tilt
                + 0.35 * vix_score
            )
            extra = {}

        final_bias = int(round(_clamp(raw_sym, -100.0, 100.0)))
        if cot_meta.get("ok"):
            tags.append(f"COT{int(round(cot_meta['inst_pct']))}")
        if etf_meta and any(m.get("ok") for m in etf_meta):
            tags.append(f"ETF{int(round(etf_avg)):+d}")
        if vix_ratio is not None:
            tags.append("VIXCont" if vix_ratio < 1.0 else "VIXBack")
        note = "_".join(tags[:4]) if tags else "MacroComposite"
        # Ensure clean CSV field with zero commas
        note = note.replace(",", "_")

        csv_rows.append((sym, final_bias, note))
        symbol_details[sym] = {
            "asset_class": ac,
            "bias_score": final_bias,
            "note": note,
            "cot": cot_meta,
            "etfs": etf_meta,
            "cot_score": round(cot_score, 1),
            "etf_score": round(etf_avg, 1),
            **extra,
        }

    global_note = f"VIXr{vix_ratio:.2f}" if vix_ratio else "GlobalLiquidity"
    csv_rows.append(("GLOBAL", global_bias, global_note))

    # Write MacroFlowBias.csv
    csv_lines = [
        f"# LeadingConfluencePro Cloud Macro/OnChain/ETF/COT Feed | Updated UTC: {now_iso}",
        "SYMBOL,BIAS_SCORE,SOURCE_NOTE",
    ]
    for sym, b, n in csv_rows:
        csv_lines.append(f"{sym},{b},{n}")
    (DATA_DIR / "MacroFlowBias.csv").write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

    # Write macro_snapshot.json
    snapshot = {
        "updated_utc": now_iso,
        "global_bias": global_bias,
        "global_regime": {
            "vix": round(vix_val, 2) if vix_val is not None else None,
            "vix3m": round(vix3m_val, 2) if vix3m_val is not None else None,
            "vix_vix3m_ratio": round(vix_ratio, 3) if vix_ratio is not None else None,
            "vix_state": "CONTANGO_RISK_ON" if (vix_ratio and vix_ratio < 1.0) else ("BACKWARDATION_RISK_OFF" if vix_ratio else "UNKNOWN"),
            "hy_oas_pct": round(hy_latest, 2) if hy_latest is not None else None,
            "hy_oas_chg20": round(hy_chg20, 2),
            "real_10y_yield": round(real10_latest, 2) if real10_latest is not None else None,
            "real_10y_chg20": round(real10_chg20, 2),
            "yield_curve_10y2y": round(t10y2y_latest, 2) if t10y2y_latest is not None else None,
            "fed_net_liq_trillion": fed_liq_latest_t,
            "fed_net_liq_4w_pct": fed_liq_chg4w_pct,
            "dxy": round(dxy_latest, 2) if dxy_latest is not None else None,
            "dxy_ret20_pct": round(dxy_ret20, 2),
            "breadth_rsp_spy_20d_pct": breadth_chg,
            "credit_hyg_tlt_20d_pct": hygtlt_chg,
            "tech_qqq_xlp_20d_pct": qqqxlp_chg,
            "stablecoins": stable_data,
            "fear_greed": fng_data,
        },
        "symbols": symbol_details,
    }
    (DATA_DIR / "macro_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return snapshot


def _snapshot_is_fresh(max_age_minutes: int) -> bool:
    if max_age_minutes <= 0:
        return False
    snap_path = DATA_DIR / "macro_snapshot.json"
    csv_path = DATA_DIR / "MacroFlowBias.csv"
    if not snap_path.exists() or not csv_path.exists():
        return False
    try:
        meta = json.loads(snap_path.read_text(encoding="utf-8"))
        ts = datetime.strptime(meta["updated_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        if age_min < max_age_minutes:
            print(f"Macro snapshot is fresh ({age_min:.1f}m < {max_age_minutes}m); skipping external macro poll.")
            return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch live Macro, On-Chain, ETF & COT feeds.")
    parser.add_argument("--if-older-than-minutes", type=int, default=0, help="Skip refresh if existing snapshot is newer than N minutes.")
    parser.add_argument("--force", action="store_true", help="Force refresh regardless of age.")
    args = parser.parse_args()

    if not args.force and _snapshot_is_fresh(args.if_older_than_minutes):
        raise SystemExit(0)

    snap = run_macro_pipeline()
    print(f"[{snap['updated_utc']}] Global Bias = {snap['global_bias']:+d}")
    for s, info in snap["symbols"].items():
        print(f"  {s:7s} -> {info['bias_score']:+4d} ({info['note']})")
