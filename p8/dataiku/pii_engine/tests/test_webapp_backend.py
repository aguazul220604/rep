# -*- coding: utf-8 -*-
"""Smoke tests del backend Flask de la webapp (sin Dataiku)."""
import io
import unittest

from pii_engine.core.schema import ENGINE_VERSION, SCHEMA_VERSION, ColumnResult
from webapp import backend


class _FakeAdapter:
    def __init__(self):
        self.dataset_calls = []
        self.file_calls = []

    def health(self):
        return {
            "status": "ok",
            "engine_ready": True,
            "engine_version": ENGINE_VERSION,
            "model_version": "bin:test,ent:test,content:test",
        }

    def analyze_dataset(self, dataset_name, columns=None, sample_size=None, seed=None):
        if dataset_name == "falla":
            raise ValueError("dataset inválido")
        self.dataset_calls.append((dataset_name, columns, sample_size, seed))
        return [self._result(dataset_name, columns[0] if columns else "col_a")]

    def analyze_file(self, file_obj, filename, columns=None, sample_size=None, seed=None):
        self.file_calls.append((filename, columns, sample_size, seed))
        return [self._result("archivo", columns[0] if columns else "col_a")]

    @staticmethod
    def _result(table, column):
        return ColumnResult(
            table=table,
            column=column,
            pii=True,
            prob_pii=0.9,
            entity="nombre",
            prob_entity=0.8,
            evidence="both",
            match_rate=0.7,
            sample_size=3,
            needs_review=False,
            classification=1,
            engine_version=ENGINE_VERSION,
            model_version="test",
            config_version="1.0",
            schema_version=SCHEMA_VERSION,
        )


class TestWebappBackend(unittest.TestCase):
    def setUp(self):
        self.adapter = _FakeAdapter()
        backend.set_adapter(self.adapter)
        self.client = backend.app.test_client()

    def tearDown(self):
        backend.set_adapter(None)

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["engine_ready"])

    def test_analyze_dataset_requires_name(self):
        response = self.client.post("/analyze", json={})
        self.assertEqual(response.status_code, 400)

    def test_analyze_dataset(self):
        response = self.client.post(
            "/analyze",
            json={"dataset": "clientes", "columns": ["nombre_cliente"], "sample_size": 100},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload[0]["column"], "nombre_cliente")
        self.assertNotIn("values", payload[0])
        self.assertEqual(self.adapter.dataset_calls[0][0], "clientes")

    def test_analyze_dataset_value_error(self):
        response = self.client.post("/analyze", json={"dataset": "falla"})
        self.assertEqual(response.status_code, 400)

    def test_analyze_file(self):
        data = {"file": (io.BytesIO(b"col_a\nvalor\n"), "muestra.csv"), "columns": "col_a"}
        response = self.client.post("/analyze-file", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.adapter.file_calls[0][0], "muestra.csv")


if __name__ == "__main__":
    unittest.main()
