# -*- coding: utf-8 -*-
"""Pruebas del adaptador local (sin Dataiku) y sus lectores con reservoir."""
import io
import os
import tempfile
import unittest

import pandas as pd

from pii_engine.adapters.local_adapter import (
    LocalAdapter,
    read_csv_sample,
    read_parquet_sample,
)


def _csv_with_tail(total=1000, tail=100):
    values = ["inicio"] * (total - tail) + ["final"] * tail
    return ("valor\n" + "\n".join(values) + "\n").encode("utf-8")


class _RecordingAnalyzer:
    def __init__(self):
        self.data = None

    def analyze(self, data=None, **kwargs):
        self.data = data
        return []


class TestCsvReader(unittest.TestCase):
    def test_random_includes_tail_rows(self):
        source = io.BytesIO(_csv_with_tail())
        df = read_csv_sample(source, 200, seed=42, mode="random")
        self.assertEqual(len(df), 200)
        self.assertIn("final", set(df["valor"].tolist()))

    def test_head_keeps_first_rows(self):
        source = io.BytesIO(_csv_with_tail())
        df = read_csv_sample(source, 200, seed=42, mode="head")
        self.assertEqual(set(df["valor"].tolist()), {"inicio"})

    def test_preserves_leading_zeros(self):
        source = io.BytesIO(b"clabe\n012180098765432100\n")
        df = read_csv_sample(source, 10, mode="random")
        self.assertEqual(df.iloc[0]["clabe"], "012180098765432100")


class TestParquetReader(unittest.TestCase):
    def test_random_includes_tail_rows(self):
        try:
            import pyarrow.parquet  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow no disponible")

        values = ["inicio"] * 900 + ["final"] * 100
        frame = pd.DataFrame({"valor": values})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "datos.parquet")
            frame.to_parquet(path, index=False)
            sampled = read_parquet_sample(path, 200, seed=42, mode="random")
        self.assertEqual(len(sampled), 200)
        self.assertIn("final", set(sampled["valor"].tolist()))

    def test_ratio_uses_parquet_metadata(self):
        try:
            import pyarrow.parquet  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow no disponible")

        frame = pd.DataFrame({"valor": range(1000)})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "datos.parquet")
            frame.to_parquet(path, index=False)
            sampled = read_parquet_sample(path, ratio=0.02, seed=42)
        self.assertEqual(len(sampled), 20)


class TestLocalAdapter(unittest.TestCase):
    def test_analyze_file_uses_sampled_rows(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            max_read_rows=200,
            sampling_mode="random",
            sampling_seed=42,
            row_ratio=None,
            analyzer=analyzer,
        )
        results = adapter.analyze_file(
            io.BytesIO(_csv_with_tail()), "datos.csv"
        )
        self.assertEqual(results, [])
        self.assertIsNotNone(analyzer.data)
        self.assertIn("final", set(analyzer.data["valor"].tolist()))

    def test_head_mode_uses_first_rows(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            max_read_rows=200,
            sampling_mode="head",
            row_ratio=None,
            analyzer=analyzer,
        )
        adapter.analyze_file(io.BytesIO(_csv_with_tail()), "datos.csv")
        self.assertEqual(set(analyzer.data["valor"].tolist()), {"inicio"})

    def test_analyze_file_applies_dynamic_ratio(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            row_ratio=0.02,
            sampling_seed=42,
            analyzer=analyzer,
        )
        adapter.analyze_file(io.BytesIO(_csv_with_tail()), "datos.csv")
        self.assertEqual(len(analyzer.data), 20)

    def test_analyze_dataframe_applies_dynamic_ratio(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            row_ratio=0.02,
            sampling_seed=42,
            analyzer=analyzer,
        )
        adapter.analyze_dataframe(pd.DataFrame({"valor": range(1000)}))
        self.assertEqual(len(analyzer.data), 20)

    def test_analyze_dataframe_can_skip_row_sampling(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            row_ratio=0.02,
            sampling_seed=42,
            analyzer=analyzer,
        )
        adapter.analyze_dataframe(
            pd.DataFrame({"valor": range(1000)}),
            apply_row_sampling=False,
        )
        self.assertEqual(len(analyzer.data), 1000)

    def test_unsupported_format_raises(self):
        adapter = LocalAdapter(analyzer=_RecordingAnalyzer())
        with self.assertRaises(ValueError):
            adapter.analyze_file(io.BytesIO(b"x"), "datos.txt")


if __name__ == "__main__":
    unittest.main()
