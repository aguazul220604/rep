# -*- coding: utf-8 -*-
"""Pruebas del lector de archivos de la webapp v2.1."""
import io
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dependencia opcional del entorno
    yaml = None

from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.schema import ColumnResult
from webapp_v2_1 import backend


def _build_valid_clabe(base17: str) -> str:
    weights = (3, 7, 1)
    total = sum((int(base17[i]) * weights[i % 3]) % 10 for i in range(17))
    return f"{base17}{(10 - (total % 10)) % 10}"


class _Upload:
    def __init__(self, data: bytes, filename: str):
        self._buffer = io.BytesIO(data)
        self.filename = filename

    def read(self, *args, **kwargs):
        return self._buffer.read(*args, **kwargs)

    def seek(self, *args, **kwargs):
        return self._buffer.seek(*args, **kwargs)


class TestWebappV2Reader(unittest.TestCase):
    def test_csv_preserves_clabe_leading_zeros(self):
        clabes = [
            _build_valid_clabe("00218001567891234"),  # banco 002
            _build_valid_clabe("01218009876543210"),  # banco 012
            _build_valid_clabe("06222654189481000"),  # banco 062
        ]
        csv = ("clabe\n" + "\n".join(clabes) + "\n").encode("utf-8")

        df = backend._read_uploaded_file(_Upload(csv, "clabes.csv"), max_rows=100)

        self.assertEqual(len(df), len(clabes))
        for value in df["clabe"]:
            self.assertIsInstance(value, str)
            self.assertEqual(len(value), 18)

        result = PIIAnalyzer().analyze(df, table_meta="clabes.csv")[0]
        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "clabe")

    def test_csv_reader_samples_tail_rows(self):
        values = ["inicio"] * 900 + ["final"] * 100
        csv = ("valor\n" + "\n".join(values) + "\n").encode("utf-8")

        df = backend._read_uploaded_file(
            _Upload(csv, "ordenado.csv"), max_rows=200
        )

        self.assertEqual(len(df), 200)
        self.assertIn("final", set(df["valor"].tolist()))

    def test_csv_reader_applies_dynamic_ratio(self):
        values = list(range(1000))
        csv = ("valor\n" + "\n".join(str(v) for v in values) + "\n").encode("utf-8")

        df = backend._read_uploaded_file(
            _Upload(csv, "ratio.csv"),
            max_rows=None,
            ratio=0.02,
        )

        self.assertEqual(len(df), 20)


class TestUploadLimit(unittest.TestCase):
    def test_parse_max_upload_mb(self):
        for value in (
            None,
            "",
            "0",
            "none",
            "unlimited",
            "sin_limite",
            " sin límite ",
        ):
            self.assertIsNone(backend._parse_max_upload_mb(value))
        self.assertEqual(backend._parse_max_upload_mb("200"), 200)
        self.assertEqual(backend._parse_max_upload_mb(" 50 "), 50)
        with self.assertRaises(ValueError):
            backend._parse_max_upload_mb("abc")

    def test_413_returns_json_with_message(self):
        original = backend.app.config.get("MAX_CONTENT_LENGTH")
        backend.app.config["MAX_CONTENT_LENGTH"] = 16
        try:
            res = backend.app.test_client().post(
                "/process_table", data=b"x" * 128
            )
        finally:
            backend.app.config["MAX_CONTENT_LENGTH"] = original

        self.assertEqual(res.status_code, 413)
        payload = res.get_json()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "error")
        self.assertTrue(payload["message"])


class TestInformationClassification(unittest.TestCase):
    def _classify(self, entity, pii=True, needs_review=False):
        return backend._classify_information(
            {"pii": pii, "entity": entity, "needs_review": needs_review}
        )

    def test_mapping_covers_all_catalog_entities(self):
        if yaml is None:
            self.skipTest("PyYAML no disponible")
        path = Path(__file__).resolve().parents[1] / "config" / "entities.yaml"
        with path.open(encoding="utf-8") as handle:
            catalog = yaml.safe_load(handle) or {}
        entities = set((catalog.get("entities") or {}).keys())
        missing = entities - set(backend.ENTITY_TO_INFORMATION_CLASSIFICATION)
        self.assertFalse(missing, f"Entidades sin mapeo de clasificación: {sorted(missing)}")

    def test_entity_mapping(self):
        cases = {
            "nombre": "internal_pii",
            "correo": "internal_pii",
            "tarjeta": "confidential_pii",
            "clabe": "confidential_pii",
            "curp": "confidential_pii",
            "biometrico": "sensitive_pii",
            "nacionalidad": "sensitive_pii",
            "contrato": "confidential_information",
            "empresa": "internal_information",
        }
        for entity, expected in cases.items():
            with self.subTest(entity=entity):
                result = self._classify(entity)
                self.assertEqual(
                    result["information_classification"], expected
                )
                self.assertEqual(
                    result["information_classification_label"],
                    backend.INFORMATION_CLASSIFICATION_LABELS[expected],
                )
                self.assertEqual(
                    result["information_classification_group"],
                    backend.INFORMATION_CLASSIFICATION_GROUPS[expected],
                )

    def test_needs_review_overrides_to_confidential_pii(self):
        result = self._classify("nombre", needs_review=True)
        self.assertEqual(
            result["information_classification"], "confidential_pii"
        )
        self.assertTrue(result["classification_review"])
        self.assertEqual(result["classification_reason"], "needs_review")

    def test_otro_pii_is_confidential_pii_with_review(self):
        result = self._classify("otro_pii")
        self.assertEqual(
            result["information_classification"], "confidential_pii"
        )
        self.assertTrue(result["classification_review"])
        self.assertEqual(result["classification_reason"], "needs_review")

    def test_unknown_entity_falls_back_to_confidential_pii(self):
        result = self._classify("entidad_desconocida")
        self.assertEqual(
            result["information_classification"], "confidential_pii"
        )
        self.assertTrue(result["classification_review"])
        self.assertEqual(result["classification_reason"], "unknown_entity")

    def test_non_pii_has_no_classification(self):
        result = self._classify("nombre", pii=False)
        self.assertIsNone(result["information_classification"])
        self.assertIsNone(result["information_classification_group"])
        self.assertEqual(
            result["information_classification_label"], "Sin clasificación"
        )
        self.assertFalse(result["classification_review"])
        self.assertEqual(result["classification_reason"], "not_pii")

    def test_business_entities_are_marked_for_review(self):
        for entity in ("empresa", "contrato"):
            with self.subTest(entity=entity):
                result = self._classify(entity)
                self.assertTrue(result["classification_review"])

    def test_result_to_row_includes_classification(self):
        class _Result:
            @staticmethod
            def to_dict():
                return {
                    "column": "num_tarjeta",
                    "pii": True,
                    "prob_pii": 0.99,
                    "entity": "tarjeta",
                    "prob_entity": 0.95,
                    "evidence": "both",
                    "match_rate": 0.9,
                    "sample_size": 100,
                    "needs_review": False,
                    "model_version": "test",
                }

        row = backend._result_to_row(_Result(), "datos.csv")
        self.assertEqual(
            row["information_classification"], "confidential_pii"
        )
        self.assertEqual(row["information_classification_label"], "PII Confidencial")
        self.assertFalse(row["classification_review"])
        self.assertEqual(row["classification_reason"], "entity")
        self.assertEqual(
            row["classification_version"], backend.INFORMATION_CLASSIFICATION_VERSION
        )


class _FakeAdapter:
    def analyze_dataframe(
        self,
        df,
        table_name,
        sample_size=None,
        apply_row_sampling=True,
    ):
        return [
            ColumnResult(
                table=table_name,
                column="num_tarjeta",
                pii=True,
                prob_pii=0.99,
                entity="tarjeta",
                prob_entity=0.95,
                evidence="both",
                match_rate=0.9,
                sample_size=10,
                needs_review=False,
                classification=1,
            )
        ]


class TestProcessTableClassification(unittest.TestCase):
    def test_process_table_returns_information_classification(self):
        original = backend._adapter
        backend.set_adapter(_FakeAdapter())
        try:
            csv = b"num_tarjeta\n4111111111111111\n"
            res = backend.app.test_client().post(
                "/process_table",
                data={"file": (io.BytesIO(csv), "datos.csv")},
                content_type="multipart/form-data",
            )
        finally:
            backend.set_adapter(original)

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(
            payload["classification_version"],
            backend.INFORMATION_CLASSIFICATION_VERSION,
        )
        row = payload["columns_analysis"][0]
        self.assertEqual(row["information_classification"], "confidential_pii")
        self.assertEqual(row["information_classification_label"], "PII Confidencial")
        self.assertEqual(row["classification_reason"], "entity")
        self.assertFalse(row["classification_review"])


if __name__ == "__main__":
    unittest.main()
