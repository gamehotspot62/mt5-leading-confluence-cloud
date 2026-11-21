"""
Resilient keyless HTTP clients for Yahoo Finance, FRED, CFTC COT (Socrata),
CoinMetrics Community API, Deribit Public Derivatives, DefiLlama Stablecoins,
and Alternative.me Fear & Greed Index.
"""

from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

WATCHLIST: Dict[str, Dict[str, Any]] = {
    "XAUUSD": {
        "asset_class": "metals",
        "yahoo": "GC=F",
        "cot_dataset": "72hh-3qpy",
        "cot_code": "088691",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["GLD"],
    },
    "BTCUSD": {
        "asset_class": "crypto",
        "yahoo": "BTC-USD",
        "cot_dataset": "gpe5-46if",
        "cot_code": "133741",
        "cot_invert": False,
        "cm_asset": "btc",
        "deribit_inst": "BTC-PERPETUAL",
        "etf_tickers": ["IBIT", "FBTC"],
    },
    "ETHUSD": {
        "asset_class": "crypto",
        "yahoo": "ETH-USD",
        "cot_dataset": None,
        "cot_code": None,
        "cot_invert": False,
        "cm_asset": "eth",
        "deribit_inst": "ETH-PERPETUAL",
        "etf_tickers": ["ETHA"],
    },
    "XAGUSD": {
        "asset_class": "metals",
        "yahoo": "SI=F",
        "cot_dataset": "72hh-3qpy",
        "cot_code": "084691",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["SLV"],
    },
    "USOIL": {
        "asset_class": "energy",
        "yahoo": "CL=F",
        "cot_dataset": "72hh-3qpy",
        "cot_code": "067651",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["USO"],
    },
    "EURUSD": {
        "asset_class": "fx",
        "yahoo": "EURUSD=X",
        "cot_dataset": "gpe5-46if",
        "cot_code": "099741",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["FXE"],
    },
    "GBPUSD": {
        "asset_class": "fx",
        "yahoo": "GBPUSD=X",
        "cot_dataset": "gpe5-46if",
        "cot_code": "096742",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["FXB"],
    },
    "USDJPY": {
        "asset_class": "fx",
        "yahoo": "JPY=X",
        "cot_dataset": "gpe5-46if",
        "cot_code": "097741",
        "cot_invert": True,  # CFTC JPY futures are quoted JPY/USD; MT5 USDJPY is USD/JPY
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["FXY"],
    },
    "US500": {
        "asset_class": "index",
        "yahoo": "SPY",
        "cot_dataset": "gpe5-46if",
        "cot_code": "13874A",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["SPY", "RSP"],
    },
    "US100": {
        "asset_class": "index",
        "yahoo": "QQQ",
        "cot_dataset": "gpe5-46if",
        "cot_code": "209742",
        "cot_invert": False,
        "cm_asset": None,
        "deribit_inst": None,
        "etf_tickers": ["QQQ"],
    },
}


def _http_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
    retries: int = 2,
) -> Optional[requests.Response]:
    """Perform resilient HTTP GET with browser User-Agent and exponential backoff."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/csv,*/*"}
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None


def fetch_yahoo_ohlcv(
    ticker: str,
    interval: str = "1d",
    range_period: str = "5y",
) -> Optional[pd.DataFrame]:
    """
    Fetch historical OHLCV from Yahoo Finance v8 chart API.
    Returns DataFrame with columns ['time', 'open', 'high', 'low', 'close', 'volume']
    indexed chronologically with UTC pd.Timestamp in 'time'.
    If >80% of bars have volume <= 0 (typical on Yahoo FX feeds), synthesizes a
    range-activity proxy so BVC/VWAP/RVOL stay informative.
    """
    encoded = quote(ticker, safe="=.-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"

    ranges_to_try = [range_period]
    if interval == "1h" and range_period == "730d":
        ranges_to_try.append("1y")
    elif interval == "15m" and range_period == "60d":
        ranges_to_try.append("30d")

    resp = None
    for rp in ranges_to_try:
        resp = _http_get(
            url,
            params={"interval": interval, "range": rp, "includePrePost": "false"},
            timeout=20,
        )
        if resp is not None:
            break

    if resp is None:
        return None

    try:
        payload = resp.json()
        res = payload["chart"]["result"][0]
        ts = res.get("timestamp", [])
        q = res["indicators"]["quote"][0]
        if not ts:
            return None

        vol_raw = q.get("volume")
        if vol_raw is None:
            vol_series = np.zeros(len(ts), dtype=float)
        else:
            vol_series = pd.to_numeric(pd.Series(vol_raw), errors="coerce").fillna(0.0).values

        df = pd.DataFrame(
            {
                "time": pd.to_datetime(ts, unit="s", utc=True),
                "open": pd.to_numeric(pd.Series(q.get("open")), errors="coerce").values,
                "high": pd.to_numeric(pd.Series(q.get("high")), errors="coerce").values,
                "low": pd.to_numeric(pd.Series(q.get("low")), errors="coerce").values,
                "close": pd.to_numeric(pd.Series(q.get("close")), errors="coerce").values,
                "volume": vol_series,
            }
        )
        df = df.dropna(subset=["open", "high", "low", "close"]).copy()
        df = df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
        if df.empty:
            return None

        # Fix missing/zero volume feeds (e.g., EURUSD=X) via normalized intrabar range activity
        zero_frac = float((df["volume"] <= 0.0).mean())
        if zero_frac > 0.80:
            rel_range = (df["high"] - df["low"]).clip(lower=0.0) / df["close"].abs().clip(lower=1e-9)
            df["volume"] = np.maximum(rel_range * 1e6, 1.0)
        else:
            df["volume"] = np.where(df["volume"] > 0.0, df["volume"], 1.0)

        return df
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def fetch_fred_series(series_id: str, timeout: int = 8, retries: int = 0) -> Optional[pd.Series]:
    """Fetch a FRED series via keyless fredgraph.csv endpoint (bounded timeout)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series_id)}&cosd=2021-01-01"
    resp = _http_get(url, timeout=timeout, retries=retries)
    if resp is None:
        return None
    try:
        df = pd.read_csv(io.StringIO(resp.text))
        if df.shape[1] < 2:
            return None
        date_col = df.columns[0]
        val_col = df.columns[1]
        df[date_col] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
        df = df.dropna(subset=[date_col, val_col]).sort_values(date_col)
        s = pd.Series(df[val_col].values, index=df[date_col], name=series_id)
        return s[~s.index.duplicated(keep="last")]
    except Exception:
        return None


def fetch_fred_batch(series_ids: List[str], timeout: int = 8) -> Dict[str, Optional[pd.Series]]:
    """Fetch multiple FRED series concurrently so timeouts never compound sequentially."""
    if not series_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(series_ids))) as ex:
        results = list(ex.map(lambda sid: fetch_fred_series(sid, timeout=timeout, retries=0), series_ids))
    return dict(zip(series_ids, results))


def fetch_cftc_cot(
    dataset_id: str,
    contract_codes: List[str],
    limit_per_code: int = 160,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch CFTC Commitments of Traders data via Socrata JSON API.
    Supports TFF ('gpe5-46if') and Disaggregated ('72hh-3qpy').
    Returns dict mapping contract_code -> chronologically sorted DataFrame with
    ['date', 'inst_net', 'spec_net', 'inst_pct', 'spec_pct', 'inst_chg4w'].
    """
    if not contract_codes:
        return {}

    codes_sql = ",".join(f"'{c}'" for c in contract_codes)
    url = f"https://publicreporting.cftc.gov/resource/{dataset_id}.json"
    params = {
        "$where": f"cftc_contract_market_code in({codes_sql})",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": int(limit_per_code * len(contract_codes)),
    }
    resp = _http_get(url, params=params, timeout=25)
    if resp is None:
        return {}

    try:
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return {}
        raw = pd.DataFrame(rows)
        if "cftc_contract_market_code" not in raw.columns:
            return {}

        out: Dict[str, pd.DataFrame] = {}
        for code in contract_codes:
            sub = raw[raw["cftc_contract_market_code"] == code].copy()
            if sub.empty:
                continue
            sub["date"] = pd.to_datetime(
                sub["report_date_as_yyyy_mm_dd"], utc=True, errors="coerce"
            )
            sub = sub.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")

            if dataset_id == "gpe5-46if":
                am_l = pd.to_numeric(sub.get("asset_mgr_positions_long"), errors="coerce").fillna(0.0)
                am_s = pd.to_numeric(sub.get("asset_mgr_positions_short"), errors="coerce").fillna(0.0)
                lm_l = pd.to_numeric(sub.get("lev_money_positions_long"), errors="coerce").fillna(0.0)
                lm_s = pd.to_numeric(sub.get("lev_money_positions_short"), errors="coerce").fillna(0.0)
                sub["inst_net"] = am_l - am_s
                sub["spec_net"] = lm_l - lm_s
            else:
                mm_l = pd.to_numeric(sub.get("m_money_positions_long_all"), errors="coerce").fillna(0.0)
                mm_s = pd.to_numeric(sub.get("m_money_positions_short_all"), errors="coerce").fillna(0.0)
                pr_l = pd.to_numeric(sub.get("prod_merc_positions_long"), errors="coerce").fillna(0.0)
                pr_s = pd.to_numeric(sub.get("prod_merc_positions_short"), errors="coerce").fillna(0.0)
                sub["inst_net"] = pr_l - pr_s
                sub["spec_net"] = mm_l - mm_s

            # Rolling 156-week (3-year) percentile rank [0..100]
            def _roll_pct(series: pd.Series) -> pd.Series:
                vals = series.values
                res = np.full(len(vals), 50.0)
                for idx in range(len(vals)):
                    w = vals[max(0, idx - 155) : idx + 1]
                    if len(w) >= 4:
                        res[idx] = 100.0 * float(np.mean(w <= vals[idx]))
                return pd.Series(res, index=series.index)

            sub["inst_pct"] = _roll_pct(sub["inst_net"])
            sub["spec_pct"] = _roll_pct(sub["spec_net"])
            sub["inst_chg4w"] = sub["inst_pct"] - sub["inst_pct"].shift(4).fillna(sub["inst_pct"])
            sub["spec_chg4w"] = sub["spec_pct"] - sub["spec_pct"].shift(4).fillna(sub["spec_pct"])

            out[code] = sub[
                ["date", "inst_net", "spec_net", "inst_pct", "spec_pct", "inst_chg4w", "spec_chg4w"]
            ].reset_index(drop=True)
        return out
    except Exception:
        return {}


def fetch_coinmetrics(
    asset: str,
    metrics: str = "CapMVRVCur,FlowInExUSD,FlowOutExUSD,SplyExNtv,AdrActCnt,PriceUSD",
    page_size: int = 120,
    max_pages: int = 1,
) -> Optional[pd.DataFrame]:
    """
    Fetch keyless CoinMetrics Community API daily metrics.
    Uses ONLY verified free metrics (never NVTAdj).
    """
    url = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    all_rows: List[Dict[str, Any]] = []
    next_url: Optional[str] = url
    params: Optional[Dict[str, Any]] = {
        "assets": asset,
        "metrics": metrics,
        "frequency": "1d",
        "page_size": page_size,
    }

    for _ in range(max_pages):
        resp = _http_get(next_url, params=params, timeout=20)
        if resp is None:
            break
        try:
            js = resp.json()
            data = js.get("data", [])
            if data:
                all_rows.extend(data)
            next_url = js.get("next_page_url")
            params = None
            if not next_url or len(data) == 0:
                break
        except Exception:
            break

    if not all_rows:
        return None

    try:
        df = pd.DataFrame(all_rows)
        df["date"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        for col in metrics.split(","):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").drop_duplicates("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def fetch_deribit_perp(instrument: str = "BTC-PERPETUAL") -> Optional[Dict[str, float]]:
    """Fetch live Deribit perpetual funding rate and open interest (keyless)."""
    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_instrument"
    resp = _http_get(url, params={"instrument_name": instrument}, timeout=12)
    if resp is None:
        return None
    try:
        res = resp.json().get("result", [])
        if not res:
            return None
        row = res[0]
        return {
            "funding_8h": float(row.get("funding_8h") or 0.0),
            "current_funding": float(row.get("current_funding") or 0.0),
            "open_interest": float(row.get("open_interest") or 0.0),
            "mark_price": float(row.get("mark_price") or 0.0),
            "volume_usd": float(row.get("volume_usd") or 0.0),
        }
    except Exception:
        return None


def fetch_defillama_stablecoins() -> Optional[Dict[str, float]]:
    """Fetch global stablecoin supply 7d and 30d % change from DefiLlama."""
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    resp = _http_get(url, timeout=20)
    if resp is None:
        return None
    try:
        rows = resp.json()
        if not isinstance(rows, list) or len(rows) < 35:
            return None
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_numeric(df["date"], errors="coerce")
        df["usd"] = df["totalCirculatingUSD"].apply(
            lambda d: float(d.get("peggedUSD", 0.0)) if isinstance(d, dict) else 0.0
        )
        df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
        cur = float(df["usd"].iloc[-1])
        prev7 = float(df["usd"].iloc[-8]) if len(df) >= 8 else cur
        prev30 = float(df["usd"].iloc[-31]) if len(df) >= 31 else cur
        chg7d = ((cur - prev7) / prev7 * 100.0) if prev7 > 0 else 0.0
        chg30d = ((cur - prev30) / prev30 * 100.0) if prev30 > 0 else 0.0
        return {
            "total_usd_b": round(cur / 1e9, 2),
            "chg_7d_pct": round(chg7d, 2),
            "chg_30d_pct": round(chg30d, 2),
        }
    except Exception:
        return None


def fetch_fear_greed(limit: int = 30) -> Optional[Dict[str, float]]:
    """Fetch Alternative.me Crypto Fear & Greed Index."""
    url = f"https://api.alternative.me/fng/?limit={int(limit)}"
    resp = _http_get(url, timeout=12)
    if resp is None:
        return None
    try:
        data = resp.json().get("data", [])
        if not data:
            return None
        vals = [float(d["value"]) for d in data if "value" in d]
        if not vals:
            return None
        cur = vals[0]
        avg7 = float(np.mean(vals[:7]))
        return {"value": cur, "avg7d": round(avg7, 1)}
    except Exception:
        return None
