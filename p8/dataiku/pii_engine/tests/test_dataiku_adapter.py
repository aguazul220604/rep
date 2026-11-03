# -*- coding: utf-8 -*-
"""Pruebas del adaptador de Dataiku con un fake del paquete `dataiku`."""
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd

from pii_engine.adapters.dataiku_adapter import DataikuAdapter, ModelArtifactError
from pii_engine.core.name_models import NameModelPredictor
from pii_engine.recognizers.model_recognizer import ContentModelLoader


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

    def test_analyze_file_csv(self):
        csv = b"nombre_cliente,campo_x\nJUAN PEREZ,ANA TORRES\nMARIA LOPEZ,LUIS RAMOS\n"
        results = self.adapter.analyze_file(io.BytesIO(csv), "muestra.csv")
        self.assertEqual({result.column for result in results}, {"nombre_cliente", "campo_x"})

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
