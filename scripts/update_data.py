from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

COIN_METRICS_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
BINANCE_SPOT_URL = "https://api.binance.com/api/v3/klines"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FNG_URL = "https://api.alternative.me/fng/"

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
REQUEST_TIMEOUT_SECONDS = 30


def _request_json(url: str, params: dict) -> dict:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _load_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def _validate_values(df: pd.DataFrame, timestamp_col: str) -> None:
    value_df = df.drop(columns=[timestamp_col])
    if value_df.isnull().any().any():
        raise ValueError("Detected missing or invalid values in dataset")


def ensure_strict_continuity(df: pd.DataFrame, timestamp_col: str, frequency: str) -> None:
    if df.empty:
        return

    if df[timestamp_col].isnull().any():
        raise ValueError("Detected missing timestamps")

    df = df.sort_values(timestamp_col)
    _validate_values(df, timestamp_col)

    expected = pd.date_range(
        start=df[timestamp_col].iloc[0],
        end=df[timestamp_col].iloc[-1],
        freq=frequency,
        tz="UTC",
    )
    actual = pd.DatetimeIndex(df[timestamp_col])

    if len(expected) != len(actual) or not actual.equals(expected):
        raise ValueError("Detected data gaps; refusing to write corrupted data")


def merge_deduplicate(existing: pd.DataFrame, new_data: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        merged = new_data.copy()
    elif new_data.empty:
        merged = existing.copy()
    else:
        merged = pd.concat([existing, new_data], ignore_index=True)

    if merged.empty:
        return merged

    merged = merged.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    merged.reset_index(drop=True, inplace=True)
    return merged


def _write_if_changed(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_existing(path)
    existing_export = existing.copy()
    if not existing_export.empty:
        existing_export["timestamp"] = existing_export["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    export_df = df.copy()
    if not export_df.empty:
        export_df["timestamp"] = export_df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not existing_export.empty and existing_export.equals(export_df):
        return

    export_df.to_csv(path, index=False)


def fetch_coin_metrics_mvrv(asset: str, start_time: pd.Timestamp | None) -> pd.DataFrame:
    params = {"assets": asset, "metrics": "CapMVRVCur", "frequency": "1d"}
    if start_time is not None:
        params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload = _request_json(COIN_METRICS_URL, params)
    rows = []
    for point in payload.get("data", []):
        rows.append(
            {
                "timestamp": pd.to_datetime(point["time"], utc=True),
                "mvrv": float(point["CapMVRVCur"]),
            }
        )
    return pd.DataFrame(rows)


def fetch_binance_spot_ohlcv(symbol: str, start_time: pd.Timestamp | None) -> pd.DataFrame:
    params = {"symbol": symbol, "interval": "1d", "limit": 1000}
    if start_time is not None:
        params["startTime"] = int(start_time.timestamp() * 1000)

    payload = _request_json(BINANCE_SPOT_URL, params)
    rows = []
    for item in payload:
        rows.append(
            {
                "timestamp": pd.to_datetime(item[0], unit="ms", utc=True),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            }
        )
    return pd.DataFrame(rows)


def fetch_binance_funding_rates(symbol: str, start_time: pd.Timestamp | None) -> pd.DataFrame:
    params = {"symbol": symbol, "limit": 1000}
    if start_time is not None:
        params["startTime"] = int(start_time.timestamp() * 1000)

    payload = _request_json(BINANCE_FUNDING_URL, params)
    rows = []
    for item in payload:
        rows.append(
            {
                "timestamp": pd.to_datetime(item["fundingTime"], unit="ms", utc=True),
                "funding_rate": float(item["fundingRate"]),
            }
        )
    return pd.DataFrame(rows)


def fetch_fear_and_greed() -> pd.DataFrame:
    payload = _request_json(FNG_URL, {"limit": 0, "format": "json"})
    rows = []
    for item in payload.get("data", []):
        rows.append(
            {
                "timestamp": pd.to_datetime(int(item["timestamp"]), unit="s", utc=True),
                "fear_greed_value": int(item["value"]),
                "classification": item["value_classification"],
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("timestamp")
    return df


def update_series(
    path: Path,
    fetch_fn,
    continuity_frequency: str,
) -> None:
    existing = _load_existing(path)
    start_time = None
    if not existing.empty:
        start_time = existing["timestamp"].max() + pd.tseries.frequencies.to_offset(continuity_frequency)

    fetched = fetch_fn(start_time)
    if existing.empty and fetched.empty:
        raise ValueError(f"No data returned for {path}")

    merged = merge_deduplicate(existing, fetched)
    ensure_strict_continuity(merged, "timestamp", continuity_frequency)
    _write_if_changed(path, merged)


def update_asset(asset_code: str, symbol: str) -> None:
    asset_dir = DATA_ROOT / asset_code

    update_series(
        asset_dir / "mvrv.csv",
        lambda start: fetch_coin_metrics_mvrv(asset_code, start),
        continuity_frequency="1D",
    )
    update_series(
        asset_dir / "spot_ohlcv.csv",
        lambda start: fetch_binance_spot_ohlcv(symbol, start),
        continuity_frequency="1D",
    )
    update_series(
        asset_dir / "funding_rates.csv",
        lambda start: fetch_binance_funding_rates(symbol, start),
        continuity_frequency="8h",
    )


def update_fear_and_greed() -> None:
    path = DATA_ROOT / "market" / "fear_greed.csv"
    existing = _load_existing(path)
    fetched = fetch_fear_and_greed()
    if existing.empty and fetched.empty:
        raise ValueError("No data returned for fear and greed index")

    merged = merge_deduplicate(existing, fetched)
    ensure_strict_continuity(merged, "timestamp", "1D")
    _write_if_changed(path, merged)


def main() -> None:
    update_asset("btc", "BTCUSDT")
    update_asset("eth", "ETHUSDT")
    update_fear_and_greed()


if __name__ == "__main__":
    main()
