# -*- coding: utf-8 -*-
"""Pruebas de adaptadores (local, Dataiku) y muestreo de filas."""
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd

from pii_engine.adapters.dataiku_adapter import (
    DataikuAdapter,
    LocalAdapter,
    ModelArtifactError,
    read_csv_sample,
    read_parquet_sample,
)
from pii_engine.core.name_models import NameModelPredictor
from pii_engine.core.row_sampling import (
    DEFAULT_ROW_SEED,
    count_chunks,
    resolve_row_sample_size,
    reservoir_sample,
    sample_dataframe,
)
from pii_engine.recognizers.model_recognizer import ContentModelLoader


def _csv_with_tail(total=1000, tail=100):
    values = ["inicio"] * (total - tail) + ["final"] * tail
    return ("valor\n" + "\n".join(values) + "\n").encode("utf-8")


def _chunks(total=50, size=100):
    return [
        pd.DataFrame({"v": range(i * size, (i + 1) * size)})
        for i in range(total)
    ]


class _RecordingAnalyzer:
    def __init__(self):
        self.data = None

    def analyze(self, data=None, **kwargs):
        self.data = data
        return []


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
            row_min_rows=None,
            sampling_seed=42,
            analyzer=analyzer,
        )
        adapter.analyze_file(io.BytesIO(_csv_with_tail()), "datos.csv")
        self.assertEqual(len(analyzer.data), 20)

    def test_analyze_dataframe_applies_dynamic_ratio(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            row_ratio=0.02,
            row_min_rows=None,
            sampling_seed=42,
            analyzer=analyzer,
        )
        adapter.analyze_dataframe(pd.DataFrame({"valor": range(1000)}))
        self.assertEqual(len(analyzer.data), 20)

    def test_analyze_dataframe_applies_minimum_rows(self):
        analyzer = _RecordingAnalyzer()
        adapter = LocalAdapter(
            row_ratio=0.02,
            row_min_rows=2000,
            row_max_rows=50000,
            sampling_seed=42,
            analyzer=analyzer,
        )
        adapter.analyze_dataframe(pd.DataFrame({"valor": range(1000)}))
        self.assertEqual(len(analyzer.data), 1000)

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


class _BinaryModel:
    def predict_proba(self, X):
        rows = []
        for value in X:
            name = str(value).lower()
            rows.append([0.10, 0.90] if "nombre" in name else [0.90, 0.10])
        return np.asarray(rows)


class _EntityModel:
    classes_ = np.array(["nombre", "otro"])

    def predict_proba(self, X):
        return np.asarray([[0.95, 0.05] for _ in X])


class _ContentModel:
    classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

    def predict_proba(self, X):
        rows = []
        for _ in X:
            p = {"apellido": 0.01, "direccion": 0.01, "empresa": 0.01, "nombre": 0.95, "otro": 0.02}
            total = sum(p.values())
            rows.append([p[c] / total for c in self.classes_])
        return np.asarray(rows)


class _FakeFolder:
    def __init__(self, model_path=None, blob=None):
        self.model_path = model_path
        self.blob = blob

    def get_path(self):
        if self.model_path is None:
            raise AttributeError("folder sin ruta local")
        return str(self.model_path)

    def get_download_stream(self, remote_path):
        if self.blob is None:
            raise FileNotFoundError(remote_path)
        return io.BytesIO(self.blob)


def _build_valid_clabe(base17: str) -> str:
    weights = (3, 7, 1)
    total = sum((int(base17[i]) * weights[i % 3]) % 10 for i in range(17))
    return f"{base17}{(10 - (total % 10)) % 10}"


class _FakeDataset:
    def __init__(self, df):
        self.df = df

    def get_dataframe(self, columns=None, limit=None, **kwargs):
        df = self.df
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        if limit:
            df = df.head(limit)
        return df


class _FakeDatasetWithIterator(_FakeDataset):
    def iter_dataframes(self, columns=None, chunksize=None, **kwargs):
        df = self.df
        if columns:
            df = df[[c for c in columns if c in df.columns]]
        size = int(chunksize or 100)
        for start in range(0, len(df), size):
            yield df.iloc[start:start + size]


class _FakeDatasetWithInfo(_FakeDatasetWithIterator):
    def __init__(self, df, record_count=None):
        super().__init__(df)
        self.record_count = len(df) if record_count is None else int(record_count)

    def get_info(self):
        return {"recordCount": self.record_count}


def _write_bundles(root: Path, content_version: str = "adapter-v1"):
    binary_dir = root / "model_binary_v1"
    entity_dir = root / "model_entity_v1"
    content_dir = root / "model_content_v1"
    for directory in (binary_dir, entity_dir, content_dir):
        directory.mkdir(parents=True, exist_ok=True)

    joblib.dump({"model": _BinaryModel(), "threshold": 0.50}, binary_dir / "model.pkl")
    joblib.dump(
        {
            "model": _EntityModel(),
            "classes": list(_EntityModel.classes_),
            "temperature": 1.0,
            "min_confidence": 0.45,
        },
        entity_dir / "model.pkl",
    )
    joblib.dump(
        {
            "model": _ContentModel(),
            "classes": list(_ContentModel.classes_),
            "temperature": 1.0,
            "tau": 0.50,
            "theta": 0.30,
            "other_label": "otro",
            "model_version": content_version,
        },
        content_dir / "model.pkl",
    )
    return binary_dir, entity_dir, content_dir


class TestDataikuAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.binary_dir, self.entity_dir, self.content_dir = _write_bundles(self.root)

        self.folders = {
            "model_binary_v1": _FakeFolder(self.binary_dir),
            "model_entity_v1": _FakeFolder(self.entity_dir),
            "model_content_v1": _FakeFolder(self.content_dir),
        }
        self.datasets = {
            "clientes": _FakeDataset(
                pd.DataFrame({
                    "nombre_cliente": ["JUAN PEREZ", "MARIA LOPEZ", "CARLOS DIAZ"],
                    "campo_x": ["ANA TORRES", "LUIS RAMOS", "EVA SOTO"],
                })
            )
        }

        dataiku = types.ModuleType("dataiku")
        dataiku.Folder = lambda name: self.folders[name]
        dataiku.Dataset = lambda name: self.datasets[name]
        self._dataiku_patch = patch.dict(sys.modules, {"dataiku": dataiku})
        self._dataiku_patch.start()

        self.adapter = DataikuAdapter(cache_dir=self.root / "cache")

    def tearDown(self):
        ContentModelLoader.clear_cache()
        self._dataiku_patch.stop()
        self.tmp.cleanup()

    def test_build_analyzer_and_health(self):
        analyzer = self.adapter.build_analyzer()
        self.assertIsNotNone(analyzer)
        health = self.adapter.health()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["engine_ready"])
        self.assertEqual(health["folders"]["binary"], "model_binary_v1")
        self.assertEqual(health["folders"]["content"], "model_content_v1")
        self.assertIn("content:adapter-v1", health["model_version"])

    def test_resolves_managed_folder_paths(self):
        self.assertEqual(
            self.adapter.resolve_model_path("model_binary_v1"),
            self.binary_dir / "model.pkl",
        )
        self.assertEqual(
            self.adapter.resolve_model_path("model_entity_v1"),
            self.entity_dir / "model.pkl",
        )
        self.assertEqual(
            self.adapter.resolve_model_path("model_content_v1"),
            self.content_dir / "model.pkl",
        )

    def test_loaders_accept_directory_with_model_pkl(self):
        predictor = NameModelPredictor(
            binary_bundle=self.binary_dir,
            entity_bundle=self.entity_dir,
        )
        self.assertIsNotNone(predictor.binary_bundle)
        self.assertIsNotNone(predictor.entity_bundle)
        artifact = ContentModelLoader.load(self.content_dir)
        self.assertIsInstance(artifact, dict)
        self.assertIn("model", artifact)

    def test_analyze_dataset_is_metadata_only(self):
        results = self.adapter.analyze_dataset("clientes")
        by_col = {result.column: result for result in results}
        self.assertTrue(by_col["nombre_cliente"].pii)
        self.assertEqual(by_col["nombre_cliente"].entity, "nombre")
        self.assertTrue(by_col["campo_x"].pii)
        self.assertEqual(by_col["campo_x"].entity, "nombre")
        serialized = by_col["campo_x"].to_dict()
        self.assertNotIn("values", serialized)
        self.assertNotIn("sample", serialized)

    def _ordered_dataset(self):
        return _FakeDatasetWithIterator(
            pd.DataFrame({
                "valor": ["inicio"] * 900 + ["final"] * 100,
            })
        )

    def test_analyze_dataset_random_samples_tail_rows(self):
        self.datasets["ordenado"] = self._ordered_dataset()
        analyzer = _RecordingAnalyzer()
        adapter = DataikuAdapter(
            cache_dir=self.root / "cache",
            max_read_rows=200,
            sampling_mode="random",
            sampling_seed=42,
            row_ratio=None,
            analyzer_factory=lambda **kwargs: analyzer,
        )
        adapter.analyze_dataset("ordenado")
        self.assertIsNotNone(analyzer.data)
        self.assertEqual(len(analyzer.data), 200)
        self.assertIn("final", set(analyzer.data["valor"].tolist()))

    def test_analyze_dataset_head_mode_keeps_first_rows(self):
        self.datasets["ordenado_head"] = self._ordered_dataset()
        analyzer = _RecordingAnalyzer()
        adapter = DataikuAdapter(
            cache_dir=self.root / "cache",
            max_read_rows=200,
            sampling_mode="head",
            analyzer_factory=lambda **kwargs: analyzer,
        )
        adapter.analyze_dataset("ordenado_head")
        self.assertEqual(set(analyzer.data["valor"].tolist()), {"inicio"})

    def test_analyze_dataset_random_fallback_without_iterator(self):
        self.datasets["sin_iterador"] = _FakeDataset(
            pd.DataFrame({"valor": ["inicio"] * 900 + ["final"] * 100})
        )
        analyzer = _RecordingAnalyzer()
        adapter = DataikuAdapter(
            cache_dir=self.root / "cache",
            max_read_rows=200,
            sampling_mode="random",
            sampling_seed=42,
            row_ratio=None,
            analyzer_factory=lambda **kwargs: analyzer,
        )
        adapter.analyze_dataset("sin_iterador")
        self.assertEqual(len(analyzer.data), 200)
        self.assertIn("final", set(analyzer.data["valor"].tolist()))

    def test_analyze_dataset_uses_two_percent_from_metadata(self):
        self.datasets["ratio_meta"] = _FakeDatasetWithInfo(
            pd.DataFrame({"valor": range(1000)}),
            record_count=500,
        )
        analyzer = _RecordingAnalyzer()
        adapter = DataikuAdapter(
            cache_dir=self.root / "cache",
            row_ratio=0.02,
            row_min_rows=None,
            row_max_rows=None,
            sampling_seed=42,
            analyzer_factory=lambda **kwargs: analyzer,
        )
        adapter.analyze_dataset("ratio_meta")
        self.assertEqual(len(analyzer.data), 10)

    def test_analyze_dataset_counts_rows_without_metadata(self):
        self.datasets["ratio_count"] = _FakeDatasetWithIterator(
            pd.DataFrame({"valor": range(1000)})
        )
        analyzer = _RecordingAnalyzer()
        adapter = DataikuAdapter(
            cache_dir=self.root / "cache",
            row_ratio=0.02,
            row_min_rows=None,
            row_max_rows=None,
            sampling_seed=42,
            analyzer_factory=lambda **kwargs: analyzer,
        )
        adapter.analyze_dataset("ratio_count")
        self.assertEqual(len(analyzer.data), 20)

    def test_analyze_file_csv(self):
        csv = b"nombre_cliente,campo_x\nJUAN PEREZ,ANA TORRES\nMARIA LOPEZ,LUIS RAMOS\n"
        results = self.adapter.analyze_file(io.BytesIO(csv), "muestra.csv")
        self.assertEqual({result.column for result in results}, {"nombre_cliente", "campo_x"})

    def test_analyze_file_preserves_clabe_leading_zeros(self):
        clabes = [
            _build_valid_clabe("01218009876543210"),  # banco 012
            _build_valid_clabe("06222654189481000"),  # banco 062
            _build_valid_clabe("00218001567891234"),  # banco 002
        ]
        csv = ("clabe\n" + "\n".join(clabes) + "\n").encode("utf-8")
        results = self.adapter.analyze_file(io.BytesIO(csv), "clabes.csv")
        result = results[0]

        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "clabe")

    def test_downloads_model_when_folder_has_no_local_path(self):
        blob = (self.content_dir / "model.pkl").read_bytes()
        self.folders["model_content_v1"] = _FakeFolder(None, blob)
        path = self.adapter.resolve_model_path("model_content_v1")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), blob)

    def test_missing_model_is_reported(self):
        del self.folders["model_content_v1"]
        with self.assertRaises(ModelArtifactError):
            self.adapter.build_analyzer()
        health = self.adapter.health()
        self.assertEqual(health["status"], "degraded")
        self.assertFalse(health["engine_ready"])

    def test_health_reports_exception_detail(self):
        def broken_factory(**kwargs):
            raise RuntimeError("modelo roto")

        adapter = DataikuAdapter(
            binary_folder=self.binary_dir,
            entity_folder=self.entity_dir,
            content_folder=self.content_dir,
            analyzer_factory=broken_factory,
        )
        health = adapter.health()
        self.assertEqual(health["status"], "degraded")
        self.assertIn("RuntimeError: modelo roto", health["error"])


if __name__ == "__main__":
    unittest.main()
