# -*- coding: utf-8 -*-
"""Tests del export XLSX de webapp_v2_1/backend.py (9 columnas)."""
import importlib.util
import io
import unittest
from pathlib import Path

_BACKEND_PATH = (
    Path(__file__).resolve().parents[2] / "webapp_v2_1" / "backend.py"
)

EXPECTED_HEADER = [
    "Archivo",
    "Nombre de Columna",
    "Prediccion",
    "Probabilidad PII (%)",
    "Evidencia",
    "Requiere revision",
    "Entidad",
    "Probabilidad Entidad (%)",
    "Muestra",
    "Clasificación",
    "Grupo",
    "Revisión clasificación",
]


def _load_backend():
    spec = importlib.util.spec_from_file_location(
        "webapp_v2_1_backend", _BACKEND_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    backend = _load_backend()
except Exception as _exc:  # pragma: no cover - dependencias del entorno
    raise unittest.SkipTest(f"backend webapp_v2_1 no cargable: {_exc}")


@unittest.skipIf(backend.Workbook is None, "openpyxl no disponible")
class TestExportXlsx(unittest.TestCase):
    def setUp(self):
        self.client = backend.app.test_client()

    def _post(self, rows, filename="analisis.csv.xlsx"):
        return self.client.post(
            "/export_xlsx",
            json={"filename": filename, "rows": rows},
        )

    @staticmethod
    def _sheet(response):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(response.data))
        return wb.active

    def test_header_has_twelve_columns(self):
        self.assertEqual(backend.EXPORT_XLSX_HEADER, EXPECTED_HEADER)
        self.assertEqual(backend._EXPORT_COL_COUNT, 12)

    def test_xlsx_filename_avoids_double_extension(self):
        self.assertEqual(
            backend._safe_xlsx_filename("analisis_pii_data_test_final.csv"),
            "analisis_pii_data_test_final.xlsx",
        )
        self.assertEqual(
            backend._safe_xlsx_filename("analisis_pii_data_test_final.xlsx"),
            "analisis_pii_data_test_final.xlsx",
        )
        self.assertEqual(
            backend._safe_xlsx_filename("analisis_pii_data_test_final.xls"),
            "analisis_pii_data_test_final.xlsx",
        )

    def test_full_row_keeps_twelve_values(self):
        row = [
            "clientes.csv", "nombre_cliente", "PII", "98.50",
            "Nombre + contenido", "Si", "nombre", "88.10", 42,
            "PII Interno", "internal", "No",
        ]
        response = self._post([row])
        self.assertEqual(response.status_code, 200)
        ws = self._sheet(response)
        self.assertEqual(
            [c.value for c in ws[1]], EXPECTED_HEADER
        )
        values = [c.value for c in ws[2]]
        self.assertEqual(len(values), 12)
        self.assertEqual(values[0:3], row[0:3])
        self.assertEqual(values[3], 98.5)
        self.assertEqual(values[4:7], row[4:7])
        self.assertEqual(values[7], 88.1)
        self.assertEqual(values[8], 42)
        self.assertEqual(values[9:12], row[9:12])

    def test_short_row_is_padded(self):
        row = ["a.csv", "col", "NO PII", "12.00", "Nombre", "No"]
        response = self._post([row])
        self.assertEqual(response.status_code, 200)
        ws = self._sheet(response)
        values = [c.value for c in ws[2]]
        self.assertEqual(len(values), 12)
        self.assertEqual(values[:3], row[:3])
        self.assertEqual(values[3], 12.0)
        self.assertEqual(values[4:6], row[4:6])
        self.assertTrue(all(v in (None, "") for v in values[6:]))

    def test_long_row_is_truncated(self):
        row = ["a.csv", "col", "PII", "90.00", "Contenido", "No",
               "email", "70.00", 10, "PII Confidencial",
               "confidential_sensitive", "No", "extra1", "extra2", "extra3"]
        response = self._post([row])
        self.assertEqual(response.status_code, 200)
        ws = self._sheet(response)
        values = [c.value for c in ws[2]]
        self.assertEqual(len(values), 12)
        self.assertEqual(values[0:3], row[0:3])
        self.assertEqual(values[3], 90.0)
        self.assertEqual(values[4:7], row[4:7])
        self.assertEqual(values[7], 70.0)
        self.assertEqual(values[8], 10)
        self.assertEqual(values[9:12], row[9:12])

    def test_empty_payload_is_rejected(self):
        response = self._post([])
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
