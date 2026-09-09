from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import requests

COIN_METRICS_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
BINANCE_SPOT_ENDPOINTS = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://api-gcp.binance.com/api/v3/klines",
)
BINANCE_FUNDING_ENDPOINTS = (
    "https://fapi.binance.com/fapi/v1/fundingRate",
    "https://fapi1.binance.com/fapi/v1/fundingRate",
    "https://fapi2.binance.com/fapi/v1/fundingRate",
)
FNG_URL = "https://api.alternative.me/fng/"

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
REQUEST_TIMEOUT_SECONDS = 30
BINANCE_MAX_RETRIES_PER_ENDPOINT = 3
BINANCE_RETRY_BACKOFF_SECONDS = 1
LOG_BODY_MAX_CHARS = 300
BINANCE_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
BINANCE_FAILOVER_STATUS_CODES = {403, 451}

LOGGER = logging.getLogger(__name__)


class BinanceRequestError(RuntimeError):
    pass


def _request_json(url: str, params: dict) -> Any:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _truncate_for_log(text: str, max_chars: int = LOG_BODY_MAX_CHARS) -> str:
    body = (text or "").strip()
    if not body:
        return "<empty body>"
    if len(body) <= max_chars:
        return body
    return f"{body[:max_chars]}... [truncated]"


def _request_binance_json(
    endpoints: Sequence[str],
    params: dict,
    *,
    service: str,
    request_type: str,
) -> Any:
    failures = []
    endpoint_count = len(endpoints)

    for endpoint_index, endpoint in enumerate(endpoints, start=1):
        has_next_endpoint = endpoint_index < endpoint_count
        for attempt in range(1, BINANCE_MAX_RETRIES_PER_ENDPOINT + 1):
            try:
                response = requests.get(endpoint, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.exceptions.RequestException as exc:
                if attempt < BINANCE_MAX_RETRIES_PER_ENDPOINT:
                    delay = BINANCE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    LOGGER.warning(
                        "Binance %s %s request error on endpoint %s (%d/%d), attempt %d/%d: %s; retrying in %ss",
                        service,
                        request_type,
                        endpoint,
                        endpoint_index,
                        endpoint_count,
                        attempt,
                        BINANCE_MAX_RETRIES_PER_ENDPOINT,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                LOGGER.warning(
                    "Binance %s %s request error on endpoint %s (%d/%d), attempt %d/%d: %s; %s",
                    service,
                    request_type,
                    endpoint,
                    endpoint_index,
                    endpoint_count,
                    attempt,
                    BINANCE_MAX_RETRIES_PER_ENDPOINT,
                    exc,
                    "switching to next endpoint" if has_next_endpoint else "no endpoints remaining",
                )
                failures.append(f"{endpoint} -> {type(exc).__name__}: {exc}")
                break

            status_code = response.status_code
            if status_code in BINANCE_FAILOVER_STATUS_CODES:
                body = _truncate_for_log(response.text)
                LOGGER.warning(
                    "Binance %s %s received HTTP %s from endpoint %s (%d/%d), attempt %d/%d, body=%r; %s",
                    service,
                    request_type,
                    status_code,
                    endpoint,
                    endpoint_index,
                    endpoint_count,
                    attempt,
                    BINANCE_MAX_RETRIES_PER_ENDPOINT,
                    body,
                    "switching to next endpoint" if has_next_endpoint else "no endpoints remaining",
                )
                failures.append(f"{endpoint} -> HTTP {status_code}: {body}")
                break

            if status_code in BINANCE_RETRYABLE_STATUS_CODES:
                body = _truncate_for_log(response.text)
                if attempt < BINANCE_MAX_RETRIES_PER_ENDPOINT:
                    delay = BINANCE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    LOGGER.warning(
                        "Binance %s %s received HTTP %s from endpoint %s (%d/%d), attempt %d/%d, body=%r; retrying in %ss",
                        service,
                        request_type,
                        status_code,
                        endpoint,
                        endpoint_index,
                        endpoint_count,
                        attempt,
                        BINANCE_MAX_RETRIES_PER_ENDPOINT,
                        body,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                LOGGER.warning(
                    "Binance %s %s received HTTP %s from endpoint %s (%d/%d), attempt %d/%d, body=%r; %s",
                    service,
                    request_type,
                    status_code,
                    endpoint,
                    endpoint_index,
                    endpoint_count,
                    attempt,
                    BINANCE_MAX_RETRIES_PER_ENDPOINT,
                    body,
                    "switching to next endpoint" if has_next_endpoint else "no endpoints remaining",
                )
                failures.append(f"{endpoint} -> HTTP {status_code}: {body}")
                break

            response.raise_for_status()
            try:
                return response.json()
            except (requests.exceptions.JSONDecodeError, ValueError) as exc:
                body = _truncate_for_log(response.text)
                if attempt < BINANCE_MAX_RETRIES_PER_ENDPOINT:
                    delay = BINANCE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    LOGGER.warning(
                        "Binance %s %s received invalid JSON from endpoint %s (%d/%d), attempt %d/%d, body=%r: %s; retrying in %ss",
                        service,
                        request_type,
                        endpoint,
                        endpoint_index,
                        endpoint_count,
                        attempt,
                        BINANCE_MAX_RETRIES_PER_ENDPOINT,
                        body,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                LOGGER.warning(
                    "Binance %s %s received invalid JSON from endpoint %s (%d/%d), attempt %d/%d, body=%r: %s; %s",
                    service,
                    request_type,
                    endpoint,
                    endpoint_index,
                    endpoint_count,
                    attempt,
                    BINANCE_MAX_RETRIES_PER_ENDPOINT,
                    body,
                    exc,
                    "switching to next endpoint" if has_next_endpoint else "no endpoints remaining",
                )
                failures.append(f"{endpoint} -> invalid JSON: {body}")
                break

    attempted = ", ".join(endpoints)
    failure_summary = "; ".join(failures) if failures else "no endpoint attempts were recorded"
    raise BinanceRequestError(
        f"All Binance {service} {request_type} endpoints failed. "
        f"Attempted endpoints: {attempted}. Final failures: {failure_summary}"
    )


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

    payload = _request_binance_json(
        BINANCE_SPOT_ENDPOINTS,
        params,
        service="spot",
        request_type="klines",
    )
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

    payload = _request_binance_json(
        BINANCE_FUNDING_ENDPOINTS,
        params,
        service="futures",
        request_type="fundingRate",
    )
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
    *,
    allow_stale_on_fetch_error: bool = False,
) -> None:
    existing = _load_existing(path)
    start_time = None
    if not existing.empty:
        start_time = existing["timestamp"].max() + pd.tseries.frequencies.to_offset(continuity_frequency)

    try:
        fetched = fetch_fn(start_time)
    except BinanceRequestError:
        if allow_stale_on_fetch_error and not existing.empty:
            LOGGER.warning(
                "Skipping update for %s after Binance fetch failure; keeping existing data unchanged",
                path,
            )
            return
        raise
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
        allow_stale_on_fetch_error=True,
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
