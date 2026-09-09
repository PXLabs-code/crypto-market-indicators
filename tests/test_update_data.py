import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import pandas as pd
import requests

from scripts.update_data import (
    BINANCE_FUNDING_ENDPOINTS,
    BINANCE_SPOT_ENDPOINTS,
    BinanceRequestError,
    _request_binance_json,
    _truncate_for_log,
    ensure_strict_continuity,
    fetch_binance_funding_rates,
    fetch_binance_spot_ohlcv,
    merge_deduplicate,
    update_series,
)


class UpdateDataTests(unittest.TestCase):
    @staticmethod
    def _mock_response(status_code=200, json_data=None, text=""):
        response = Mock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = json_data
        if status_code >= 400:
            response.raise_for_status.side_effect = requests.exceptions.HTTPError(
                f"{status_code} error",
                response=response,
            )
        else:
            response.raise_for_status.return_value = None
        return response

    def test_continuity_passes_for_daily_series(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2026-01-01T00:00:00Z",
                        "2026-01-02T00:00:00Z",
                        "2026-01-03T00:00:00Z",
                    ],
                    utc=True,
                ),
                "value": [1.0, 1.1, 1.2],
            }
        )

        ensure_strict_continuity(df, "timestamp", "1D")

    def test_continuity_fails_for_gap(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2026-01-01T00:00:00Z",
                        "2026-01-03T00:00:00Z",
                    ],
                    utc=True,
                ),
                "value": [1.0, 1.2],
            }
        )

        with self.assertRaises(ValueError):
            ensure_strict_continuity(df, "timestamp", "1D")

    def test_merge_deduplicate_keeps_single_timestamp(self):
        existing = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-01-01T00:00:00Z"], utc=True),
                "value": [1.0],
            }
        )
        new_data = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], utc=True
                ),
                "value": [1.0, 2.0],
            }
        )

        merged = merge_deduplicate(existing, new_data)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged.iloc[-1]["value"], 2.0)

    def test_continuity_fails_for_invalid_values(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], utc=True
                ),
                "value": [1.0, None],
            }
        )

        with self.assertRaises(ValueError):
            ensure_strict_continuity(df, "timestamp", "1D")

    @patch("scripts.update_data.time.sleep")
    @patch("scripts.update_data.requests.get")
    def test_fetch_binance_spot_ohlcv_switches_endpoint_on_451_and_truncates_log(
        self, mock_get, mock_sleep
    ):
        mock_get.side_effect = [
            self._mock_response(status_code=451, text="x" * 400),
            self._mock_response(
                status_code=200,
                json_data=[[1757376000000, "1", "2", "0.5", "1.5", "42"]],
            ),
        ]

        with self.assertLogs("scripts.update_data", level="WARNING") as logs:
            df = fetch_binance_spot_ohlcv("BTCUSDT", None)

        self.assertEqual(df.iloc[0]["close"], 1.5)
        self.assertEqual(
            [call.args[0] for call in mock_get.call_args_list[:2]],
            [BINANCE_SPOT_ENDPOINTS[0], BINANCE_SPOT_ENDPOINTS[1]],
        )
        mock_sleep.assert_not_called()
        joined_logs = "\n".join(logs.output)
        self.assertIn("HTTP 451", joined_logs)
        self.assertIn("switching to next endpoint", joined_logs)
        self.assertIn("... [truncated]", joined_logs)

    @patch("scripts.update_data.time.sleep")
    @patch("scripts.update_data.requests.get")
    def test_request_binance_json_retries_429_with_backoff(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            self._mock_response(status_code=429, text="rate limited"),
            self._mock_response(status_code=200, json_data={"ok": True}),
        ]

        payload = _request_binance_json(
            BINANCE_SPOT_ENDPOINTS,
            {"symbol": "BTCUSDT"},
            service="spot",
            request_type="klines",
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mock_get.call_args_list[0].args[0], BINANCE_SPOT_ENDPOINTS[0])
        self.assertEqual(mock_get.call_args_list[1].args[0], BINANCE_SPOT_ENDPOINTS[0])
        mock_sleep.assert_called_once_with(1)

    @patch("scripts.update_data.time.sleep")
    @patch("scripts.update_data.requests.get")
    def test_request_binance_json_retries_5xx_then_switches_endpoint(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            self._mock_response(status_code=503, text="temporary outage"),
            self._mock_response(status_code=503, text="temporary outage"),
            self._mock_response(status_code=503, text="temporary outage"),
            self._mock_response(status_code=200, json_data={"ok": True}),
        ]

        with self.assertLogs("scripts.update_data", level="WARNING") as logs:
            payload = _request_binance_json(
                BINANCE_SPOT_ENDPOINTS,
                {"symbol": "BTCUSDT"},
                service="spot",
                request_type="klines",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(
            [call.args[0] for call in mock_get.call_args_list],
            [
                BINANCE_SPOT_ENDPOINTS[0],
                BINANCE_SPOT_ENDPOINTS[0],
                BINANCE_SPOT_ENDPOINTS[0],
                BINANCE_SPOT_ENDPOINTS[1],
            ],
        )
        self.assertEqual(mock_sleep.call_args_list, [call(1), call(2)])
        self.assertIn("HTTP 503", "\n".join(logs.output))

    @patch("scripts.update_data.time.sleep")
    @patch("scripts.update_data.requests.get")
    def test_request_binance_json_retries_network_exception(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.exceptions.Timeout("timed out"),
            self._mock_response(status_code=200, json_data={"ok": True}),
        ]

        payload = _request_binance_json(
            BINANCE_SPOT_ENDPOINTS,
            {"symbol": "BTCUSDT"},
            service="spot",
            request_type="klines",
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mock_get.call_args_list[0].args[0], BINANCE_SPOT_ENDPOINTS[0])
        self.assertEqual(mock_get.call_args_list[1].args[0], BINANCE_SPOT_ENDPOINTS[0])
        mock_sleep.assert_called_once_with(1)

    @patch("scripts.update_data.requests.get")
    def test_fetch_binance_funding_rates_switches_endpoint_on_403(self, mock_get):
        mock_get.side_effect = [
            self._mock_response(status_code=403, text="blocked"),
            self._mock_response(
                status_code=200,
                json_data=[{"fundingTime": 1757376000000, "fundingRate": "0.0001"}],
            ),
        ]

        df = fetch_binance_funding_rates("BTCUSDT", None)

        self.assertEqual(df.iloc[0]["funding_rate"], 0.0001)
        self.assertEqual(
            [call.args[0] for call in mock_get.call_args_list[:2]],
            [BINANCE_FUNDING_ENDPOINTS[0], BINANCE_FUNDING_ENDPOINTS[1]],
        )

    @patch("scripts.update_data.time.sleep")
    @patch("scripts.update_data.requests.get")
    def test_fetch_binance_funding_rates_retries_invalid_json_then_switches_endpoint(
        self, mock_get, mock_sleep
    ):
        invalid_json_response = self._mock_response(status_code=200, text="")
        invalid_json_response.json.side_effect = ValueError("Expecting value")

        mock_get.side_effect = [
            self._mock_response(status_code=451, text="blocked"),
            invalid_json_response,
            invalid_json_response,
            invalid_json_response,
            self._mock_response(
                status_code=200,
                json_data=[{"fundingTime": 1757376000000, "fundingRate": "0.0002"}],
            ),
        ]

        with self.assertLogs("scripts.update_data", level="WARNING") as logs:
            df = fetch_binance_funding_rates("BTCUSDT", None)

        self.assertEqual(df.iloc[0]["funding_rate"], 0.0002)
        self.assertEqual(
            [call.args[0] for call in mock_get.call_args_list],
            [
                BINANCE_FUNDING_ENDPOINTS[0],
                BINANCE_FUNDING_ENDPOINTS[1],
                BINANCE_FUNDING_ENDPOINTS[1],
                BINANCE_FUNDING_ENDPOINTS[1],
                BINANCE_FUNDING_ENDPOINTS[2],
            ],
        )
        self.assertEqual(mock_sleep.call_args_list, [call(1), call(2)])
        self.assertIn("invalid JSON", "\n".join(logs.output))

    @patch("scripts.update_data.requests.get")
    def test_request_binance_json_raises_summary_when_all_endpoints_fail(self, mock_get):
        mock_get.side_effect = [
            self._mock_response(status_code=451, text="blocked primary"),
            self._mock_response(status_code=451, text="blocked secondary"),
            self._mock_response(status_code=451, text="blocked tertiary"),
        ]

        with self.assertRaises(BinanceRequestError) as context:
            _request_binance_json(
                BINANCE_SPOT_ENDPOINTS,
                {"symbol": "BTCUSDT"},
                service="spot",
                request_type="klines",
            )

        message = str(context.exception)
        self.assertIn(BINANCE_SPOT_ENDPOINTS[0], message)
        self.assertIn(BINANCE_SPOT_ENDPOINTS[1], message)
        self.assertIn(BINANCE_SPOT_ENDPOINTS[2], message)
        self.assertIn("HTTP 451", message)

    @patch("scripts.update_data._write_if_changed")
    @patch("scripts.update_data._load_existing")
    def test_update_series_keeps_existing_data_on_allowed_binance_fetch_error(
        self, mock_load_existing, mock_write_if_changed
    ):
        existing = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"],
                    utc=True,
                ),
                "funding_rate": [0.0001, 0.0002],
            }
        )
        mock_load_existing.return_value = existing
        fetch_fn = Mock(side_effect=BinanceRequestError("blocked"))

        with self.assertLogs("scripts.update_data", level="WARNING") as logs:
            update_series(
                Path("/tmp/funding_rates.csv"),
                fetch_fn,
                "8h",
                allow_stale_on_fetch_error=True,
            )

        fetch_fn.assert_called_once()
        mock_write_if_changed.assert_not_called()
        self.assertIn("keeping existing data unchanged", "\n".join(logs.output))

    @patch("scripts.update_data._load_existing")
    @patch("scripts.update_data._write_if_changed")
    def test_update_series_skips_allowed_binance_fetch_error_when_no_existing_data(
        self, mock_write_if_changed, mock_load_existing
    ):
        mock_load_existing.return_value = pd.DataFrame()

        with self.assertLogs("scripts.update_data", level="WARNING") as logs:
            update_series(
                Path("/tmp/funding_rates.csv"),
                Mock(side_effect=BinanceRequestError("blocked")),
                "8h",
                allow_stale_on_fetch_error=True,
            )

        mock_write_if_changed.assert_not_called()
        self.assertIn("no existing data available yet", "\n".join(logs.output))

    def test_truncate_for_log_limits_body_length(self):
        truncated = _truncate_for_log("x" * 400, max_chars=20)
        self.assertEqual(truncated, ("x" * 20) + "... [truncated]")


if __name__ == "__main__":
    unittest.main()
