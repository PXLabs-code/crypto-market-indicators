import unittest

import pandas as pd

from scripts.update_data import ensure_strict_continuity, merge_deduplicate


class UpdateDataTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
