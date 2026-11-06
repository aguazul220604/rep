# -*- coding: utf-8 -*-
"""Pruebas del muestreo aleatorio de filas (reservoir / sample_dataframe)."""
import unittest

import pandas as pd

from pii_engine.core.row_sampling import (
    DEFAULT_ROW_SEED,
    count_chunks,
    resolve_row_sample_size,
    reservoir_sample,
    sample_dataframe,
)


def _chunks(total=50, size=100):
    return [
        pd.DataFrame({"v": range(i * size, (i + 1) * size)})
        for i in range(total)
    ]


class TestSampleDataframe(unittest.TestCase):
    def test_random_is_deterministic_and_bounded(self):
        df = pd.DataFrame({"v": range(1000)})
        first = sample_dataframe(df, 100, mode="random", seed=DEFAULT_ROW_SEED)
        second = sample_dataframe(df, 100, mode="random", seed=DEFAULT_ROW_SEED)
        self.assertEqual(len(first), 100)
        self.assertEqual(first["v"].tolist(), second["v"].tolist())

    def test_head_returns_first_rows(self):
        df = pd.DataFrame({"v": range(1000)})
        out = sample_dataframe(df, 10, mode="head")
        self.assertEqual(out["v"].tolist(), list(range(10)))

    def test_small_table_is_returned_whole(self):
        df = pd.DataFrame({"v": range(5)})
        self.assertEqual(len(sample_dataframe(df, 100)), 5)

    def test_invalid_mode_raises(self):
        df = pd.DataFrame({"v": range(5)})
        with self.assertRaises(ValueError):
            sample_dataframe(df, 5, mode="ventana")

    def test_random_does_not_take_head(self):
        df = pd.DataFrame({"v": range(1000)})
        out = sample_dataframe(df, 10, mode="random", seed=DEFAULT_ROW_SEED)
        self.assertNotEqual(out["v"].tolist(), list(range(10)))


class TestResolveRowSampleSize(unittest.TestCase):
    def test_two_percent_uses_ceiling(self):
        self.assertEqual(resolve_row_sample_size(100_000, 0.02), 2_000)
        self.assertEqual(resolve_row_sample_size(1_001, 0.02), 21)
        self.assertEqual(resolve_row_sample_size(1, 0.02), 1)

    def test_zero_rows_returns_zero(self):
        self.assertEqual(resolve_row_sample_size(0, 0.02), 0)

    def test_optional_min_and_max(self):
        self.assertEqual(resolve_row_sample_size(100, 0.02, min_rows=10), 10)
        self.assertEqual(resolve_row_sample_size(100_000, 0.02, max_rows=500), 500)

    def test_min_greater_than_max_raises(self):
        with self.assertRaises(ValueError):
            resolve_row_sample_size(100, 0.02, min_rows=10, max_rows=5)

    def test_invalid_ratio_raises(self):
        with self.assertRaises(ValueError):
            resolve_row_sample_size(100, 0)
        with self.assertRaises(ValueError):
            resolve_row_sample_size(100, 1.5)


class TestRatioSampling(unittest.TestCase):
    def test_sample_dataframe_ratio_is_exact_and_bounded(self):
        df = pd.DataFrame({"v": range(1_000)})
        out = sample_dataframe(df, ratio=0.02, seed=DEFAULT_ROW_SEED)
        self.assertEqual(len(out), 20)

    def test_sample_dataframe_ratio_respects_max(self):
        df = pd.DataFrame({"v": range(1_000)})
        out = sample_dataframe(df, max_rows=5, ratio=0.02, seed=DEFAULT_ROW_SEED)
        self.assertEqual(len(out), 5)

    def test_count_chunks(self):
        self.assertEqual(count_chunks(_chunks(total=3, size=10)), 30)


class TestReservoirSample(unittest.TestCase):
    def test_returns_all_when_smaller_than_limit(self):
        out = reservoir_sample(_chunks(total=2, size=5), 100, seed=1)
        self.assertEqual(len(out), 10)

    def test_is_deterministic(self):
        first = reservoir_sample(_chunks(), 200, seed=7)
        second = reservoir_sample(_chunks(), 200, seed=7)
        self.assertEqual(first["v"].tolist(), second["v"].tolist())

    def test_respects_max_rows(self):
        out = reservoir_sample(_chunks(), 200, seed=7)
        self.assertEqual(len(out), 200)

    def test_is_uniform_across_chunks(self):
        left = pd.DataFrame({"grp": ["L"] * 1000})
        right = pd.DataFrame({"grp": ["R"] * 1000})
        out = reservoir_sample([left, right], 200, seed=DEFAULT_ROW_SEED)
        counts = out["grp"].value_counts()
        self.assertGreater(counts.get("L", 0), 60)
        self.assertGreater(counts.get("R", 0), 60)

    def test_includes_tail_rows(self):
        normal = pd.DataFrame({"v": ["inicio"] * 900})
        tail = pd.DataFrame({"v": ["final"] * 100})
        out = reservoir_sample([normal, tail], 200, seed=DEFAULT_ROW_SEED)
        self.assertIn("final", set(out["v"].tolist()))

    def test_empty_chunks_return_empty_frame(self):
        out = reservoir_sample([pd.DataFrame(), None], 10, seed=1)
        self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()
