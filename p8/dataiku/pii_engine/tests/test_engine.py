# -*- coding: utf-8 -*-
"""
Pruebas unitarias y de integración del motor de detección PII (`pii_engine`).
"""
import unittest

import numpy as np
import pandas as pd

from pii_engine import NO_ENTITY, OTRO_PII, PIIAnalyzer, RecognizerRegistry, TableMeta, analyze
from pii_engine.core.name_models import NameModelPredictor
from pii_engine.core.sampling import sample_column
from pii_engine.recognizers.model_recognizer import ContentModelLoader, ContentModelScorer
from pii_engine.recognizers.validators import (
    validate_clabe,
    validate_clave_elector,
    validate_codigo_postal,
    validate_coordenadas,
    validate_curp,
    validate_email,
    validate_fecha_nacimiento,
    validate_fecha_vencimiento,
    validate_iban,
    validate_ip,
    validate_luhn_card,
    validate_nss,
    validate_rfc,
    validate_ssn,
    validate_telefono,
)


def _build_valid_curp(base17: str) -> str:
    chars = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
    d = {c: i for i, c in enumerate(chars)}
    total = sum(d[base17[i]] * (18 - i) for i in range(17))
    check = (10 - (total % 10)) % 10
    return f"{base17}{check}"


def _build_valid_clabe(base17: str) -> str:
    weights = (3, 7, 1)
    total = sum((int(base17[i]) * weights[i % 3]) % 10 for i in range(17))
    check = (10 - (total % 10)) % 10
    return f"{base17}{check}"


class TestValidators(unittest.TestCase):
    def test_curp_validator(self):
        valid_curp = _build_valid_curp("GODE880515HDFRNS0")
        self.assertTrue(validate_curp(valid_curp))
        # Dígito verificador incorrecto
        wrong_check = valid_curp[:-1] + str((int(valid_curp[-1]) + 1) % 10)
        self.assertFalse(validate_curp(wrong_check))
        # Fecha inválida (mes 15)
        self.assertFalse(validate_curp("GODE881515HDFRNS01"))

    def test_rfc_validator(self):
        self.assertTrue(validate_rfc("GODE561231GR8"))
        self.assertTrue(validate_rfc("XAXX010101000"))
        self.assertFalse(validate_rfc("GODE561331GR8"))  # mes 13

    def test_clabe_validator(self):
        valid_clabe = _build_valid_clabe("00218001567891234")
        self.assertTrue(validate_clabe(valid_clabe))
        self.assertFalse(validate_clabe("00218001567891234" + str((int(valid_clabe[-1]) + 1) % 10)))
        self.assertFalse(validate_clabe("000000000000000000"))

    def test_iban_validator(self):
        self.assertTrue(validate_iban("ES9121000418450200051332"))
        self.assertTrue(validate_iban("GB82WEST12345698765432"))
        self.assertFalse(validate_iban("ES0021000418450200051332"))

    def test_luhn_card_validator(self):
        self.assertTrue(validate_luhn_card("4532015112830366"))
        self.assertTrue(validate_luhn_card("4532-0151-1283-0366"))
        self.assertFalse(validate_luhn_card("4532015112830367"))
        self.assertFalse(validate_luhn_card("0000000000000000"))

    def test_clave_elector_validator(self):
        self.assertTrue(validate_clave_elector("GMRDLS85031209H200"))
        self.assertFalse(validate_clave_elector("GMRDLS85191209H200"))  # mes 19

    def test_nss_validator(self):
        # Construir NSS válido de 11 dígitos
        base10 = "1290881234"
        total = 0
        for i, ch in enumerate(base10):
            p = int(ch) * (2 if i % 2 == 1 else 1)
            if p >= 10:
                p = (p // 10) + (p % 10)
            total += p
        chk = (10 - (total % 10)) % 10
        self.assertTrue(validate_nss(base10 + str(chk)))
        self.assertFalse(validate_nss(base10 + str((chk + 1) % 10)))

    def test_other_validators(self):
        self.assertTrue(validate_email("usuario.prueba@citibanamex.com.mx"))
        self.assertFalse(validate_email("correo_invalido@"))
        self.assertTrue(validate_ip("192.168.1.254"))
        self.assertTrue(validate_ip("2001:0db8:85a3:0000:0000:8a2e:0370:7334"))
        self.assertFalse(validate_ip("999.999.1.1"))
        self.assertTrue(validate_coordenadas("19.432608, -99.133209"))
        self.assertFalse(validate_coordenadas("195.4326, -99.1332"))
        self.assertTrue(validate_ssn("123-45-6789"))
        self.assertFalse(validate_ssn("666-45-6789"))
        self.assertTrue(validate_telefono("+52 55 4321 8765"))
        self.assertFalse(validate_telefono("1234567890"))
        self.assertFalse(validate_telefono("5555555555"))
        self.assertTrue(validate_codigo_postal("06600"))
        self.assertFalse(validate_codigo_postal("00100"))
        self.assertTrue(validate_fecha_nacimiento("1989-07-24"))
        self.assertTrue(validate_fecha_vencimiento("08/28"))


class TestRegistryAndSampling(unittest.TestCase):
    def test_40_entities_registered(self):
        reg = RecognizerRegistry()
        entities = reg.all_entities()
        self.assertEqual(len(entities), 40)
        # Verificar conteo por grupo (A=11, B=20, C=4, D=4, E=1)
        counts = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
        for name in entities:
            rec = reg.get(name)
            counts[rec.group] += 1
        self.assertEqual(counts, {"A": 11, "B": 20, "C": 4, "D": 4, "E": 1})

    def test_sampling_and_free_text(self):
        s_struct = pd.Series(["GODE561231GR8", "GODE561231GR8", None, "XAXX010101000"], name="rfc_cte")
        samp = sample_column(s_struct, sample_size=10)
        self.assertEqual(samp.sample_size, 2)
        self.assertFalse(samp.is_free_text)

        s_free = pd.Series(
            [
                "El cliente solicita aclaración de su correo juan.perez@gmail.com por cargo no reconocido en su cuenta.",
                "Se contactó al titular en su correo maria.lopez@hotmail.com para envío de estado de cuenta mensual.",
                "Favor de enviar comprobante fiscal al buzón soporte.clientes@empresa.com.mx antes del viernes.",
            ],
            name="comentarios",
        )
        samp_free = sample_column(s_free, sample_size=10)
        self.assertTrue(samp_free.is_free_text)


class TestEndToEndAnalyze(unittest.TestCase):
    def test_analyze_mixed_columns(self):
        c1 = _build_valid_curp("GODE880515HDFRNS0")
        c2 = _build_valid_curp("MOPA921120MDFLPR0")
        clabe1 = _build_valid_clabe("00218001567891234")
        clabe2 = _build_valid_clabe("01218009876543210")

        df = pd.DataFrame({
            "tel casa1": ["5543218765", "5587654321", "3312349876", "8187651234"],
            "campo_opaco_x1": [c1, c2, c1, c2],  # Nombre opaco pero contenido Grupo A (CURP)
            "edo_civil_cte": ["Soltero", "Casado", "Unión libre", "Divorciado"],
            "saldo_actual": ["15420.50", "890.00", "120500.75", "0.00"],  # name_only
            "folio_operacion": ["15420.50", "890.00", "120500.75", "0.00"],  # mismos números pero nombre No PII
            "credencial_ine": ["ABC123", "DEF456", "GHI789", "JKL012"],  # Clasificación 2 -> otro_pii
            "correo_vacio": [None, None, None, None],  # Nombre PII pero sin contenido
            "clabe_erronea": ["GODE561231GR8", "XAXX010101000", "GODE561231GR8", "XAXX010101000"],
            "clabe_interbancaria": [clabe1, clabe2, clabe1, clabe2],
        })

        results = {r.column: r for r in analyze(df, table_meta=TableMeta(table="clientes_nuevos"))}

        # 1. Teléfono: nombre + contenido coinciden -> evidence = both
        r_tel = results["tel casa1"]
        self.assertTrue(r_tel.pii)
        self.assertEqual(r_tel.entity, "telefono")
        self.assertEqual(r_tel.evidence, "both")
        self.assertFalse(r_tel.needs_review)

        # 2. Columna con nombre opaco pero CURP válida -> PII por contenido (Grupo A)
        r_curp = results["campo_opaco_x1"]
        self.assertTrue(r_curp.pii)
        self.assertEqual(r_curp.entity, "curp")
        self.assertEqual(r_curp.evidence, "content")

        # 3. Diccionario Grupo C (estado_civil)
        r_ec = results["edo_civil_cte"]
        self.assertTrue(r_ec.pii)
        self.assertEqual(r_ec.entity, "estado_civil")
        self.assertEqual(r_ec.evidence, "both")

        # 4. Entidad name_only: `saldo_actual` es PII, pero `folio_operacion` con los mismos números NO es PII
        r_saldo = results["saldo_actual"]
        self.assertTrue(r_saldo.pii)
        self.assertEqual(r_saldo.entity, "saldo")

        r_folio = results["folio_operacion"]
        self.assertFalse(r_folio.pii)
        self.assertEqual(r_folio.entity, NO_ENTITY)

        # 5. Clasificación 2 (`otro_pii`) -> needs_review = True
        r_ine = results["credencial_ine"]
        self.assertTrue(r_ine.pii)
        self.assertEqual(r_ine.entity, OTRO_PII)
        self.assertEqual(r_ine.classification, 2)
        self.assertTrue(r_ine.needs_review)

        # 6. Columna con nombre PII pero vacía -> conserva PII por nombre con evidence = name y needs_review = True
        r_vacio = results["correo_vacio"]
        self.assertTrue(r_vacio.pii)
        self.assertEqual(r_vacio.entity, "correo")
        self.assertEqual(r_vacio.evidence, "name")
        self.assertEqual(r_vacio.sample_size, 0)
        self.assertTrue(r_vacio.needs_review)

        # 7. Conflicto nombre vs contenido (nombre dice clabe, contenido tiene RFC) -> gana RFC con needs_review = True
        r_conf = results["clabe_erronea"]
        self.assertTrue(r_conf.pii)
        self.assertEqual(r_conf.entity, "rfc")
        self.assertEqual(r_conf.evidence, "content")
        self.assertTrue(r_conf.needs_review)

    def test_model_recognizer_interface(self):
        """Verifica que ModelRecognizer funciona cuando se registra un modelo mock en ContentModelLoader."""
        class DummyContentModel:
            classes_ = np.array(["no_pii", "nombre", "empresa"])

            def predict_proba(self, X):
                out = []
                for val in X:
                    if "S.A." in str(val):
                        out.append([0.05, 0.05, 0.90])
                    else:
                        out.append([0.05, 0.90, 0.05])
                return np.asarray(out)

        ContentModelLoader.register_in_memory("models/content/model.joblib", DummyContentModel())
        try:
            df = pd.DataFrame({
                "nom_cte": ["JUAN PEREZ LOPEZ", "MARIA GARCIA HERNANDEZ", "CARLOS SANCHEZ"],
                "razon_social": [
                    "COMERCIALIZADORA DEL NORTE S.A. DE C.V.",
                    "SERVICIOS INTEGRALES S.A.",
                    "GRUPO INDUSTRIAL S.A. DE C.V.",
                ],
            })
            engine = PIIAnalyzer()
            res = {r.column: r for r in engine.analyze(df, table_meta="tabla_grupo_d")}
            self.assertTrue(res["nom_cte"].pii)
            self.assertEqual(res["nom_cte"].entity, "nombre")
            self.assertEqual(res["nom_cte"].evidence, "both")

            self.assertTrue(res["razon_social"].pii)
            self.assertEqual(res["razon_social"].entity, "empresa")
            self.assertEqual(res["razon_social"].evidence, "both")
        finally:
            ContentModelLoader.clear_cache()


    def test_model_recognizer_dict_bundle_grupo_d(self):
        """Bundle {'model','classes',...} con las 5 clases del Grupo D (contrato de model_content_v1)."""
        classes = ["apellido", "direccion", "empresa", "nombre", "otro"]
        surnames = {"PEREZ", "LOPEZ", "GARCIA", "HERNANDEZ", "MARTINEZ", "DIAZ", "SANCHEZ"}

        class DummyContentModel:
            classes_ = np.array(classes)

            def predict_proba(self, X):
                rows = []
                for v in X:
                    s = str(v)
                    up = s.upper().strip()
                    p = {c: 0.02 for c in classes}
                    if up in surnames:
                        p["apellido"] = 0.93
                    elif any(k in up for k in ("S.A.", "S A", "C.V", "GRUPO", "COMERCIALIZADORA")):
                        p["empresa"] = 0.93
                    elif any(k in up for k in ("CALLE", "AVENIDA", "AV ", "BLVD", "COL ")):
                        p["direccion"] = 0.90
                    elif len(s.split()) >= 2:
                        p["nombre"] = 0.88
                    else:
                        p["otro"] = 0.80
                    total = sum(p.values())
                    rows.append([p[c] / total for c in classes])
                return np.asarray(rows)

        bundle = {
            "model": DummyContentModel(),
            "classes": classes,
            "temperature": 1.0,
            "tau": 0.50,
            "theta": 0.30,
            "other_label": "otro",
            "model_version": "test-content-v1",
        }
        ContentModelLoader.register_in_memory("models/content/model.joblib", bundle)
        try:
            df = pd.DataFrame({
                "nom_cte": ["JUAN PEREZ LOPEZ", "MARIA LOPEZ GARCIA", "CARLOS SANCHEZ DIAZ"],
                "razon_social": [
                    "COMERCIALIZADORA DEL NORTE S.A. DE C.V.",
                    "SERVICIOS INTEGRALES S.A.",
                    "GRUPO INDUSTRIAL S.A. DE C.V.",
                ],
                "lugar_nacimiento": ["GUADALAJARA", "MONTERREY", "PUEBLA"],
                "domicilio": [
                    "CALLE HIDALGO 123 COL CENTRO",
                    "AVENIDA JUAREZ 456 COL NORTE",
                    "BLVD BENITO JUAREZ 789",
                ],
                "estatus": ["N/A", "-", "nan"],  # solo null tokens: sin PII
            })
            engine = PIIAnalyzer()
            res = {r.column: r for r in engine.analyze(df, table_meta="tabla_grupo_d")}

            self.assertTrue(res["nom_cte"].pii)
            self.assertEqual(res["nom_cte"].entity, "nombre")
            self.assertEqual(res["nom_cte"].evidence, "both")

            self.assertTrue(res["razon_social"].pii)
            self.assertEqual(res["razon_social"].entity, "empresa")

            # lugar_nacimiento ya no es clase del modelo: se resuelve por catálogo (Grupo B).
            self.assertTrue(res["lugar_nacimiento"].pii)
            self.assertEqual(res["lugar_nacimiento"].entity, "lugar_nacimiento")

            self.assertTrue(res["domicilio"].pii)
            self.assertEqual(res["domicilio"].entity, "direccion")

            self.assertFalse(res["estatus"].pii)
        finally:
            ContentModelLoader.clear_cache()


class TestNameResolutionOptimizations(unittest.TestCase):
    class CountingBinary:
        def __init__(self):
            self.calls = 0
            self.seen = []

        def predict_proba(self, X):
            self.calls += 1
            self.seen = [str(x) for x in X]
            rows = []
            for name in self.seen:
                if "saldo" in name.lower():
                    rows.append([0.90, 0.10])
                else:
                    rows.append([0.10, 0.90])
            return np.asarray(rows)

    class CountingEntity:
        classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

        def __init__(self):
            self.calls = 0
            self.seen = []

        def predict_proba(self, X):
            self.calls += 1
            self.seen = [str(x) for x in X]
            rows = []
            for _ in self.seen:
                p = {"apellido": 0.01, "direccion": 0.01, "empresa": 0.01, "nombre": 0.90, "otro": 0.07}
                rows.append([p[c] for c in self.classes_])
            return np.asarray(rows)

    def test_binary_gate_fast_path_and_cache(self):
        binary = self.CountingBinary()
        entity = self.CountingEntity()
        predictor = NameModelPredictor(
            binary_bundle={"model": binary, "threshold": 0.50},
            entity_bundle={
                "model": entity,
                "classes": list(self.CountingEntity.classes_),
                "temperature": 1.0,
                "min_confidence": 0.45,
            },
            name_resolution_cfg={
                "binary_gate_enabled": True,
                "fast_path_enabled": True,
                "fast_path_min_confidence": 0.85,
                "cache_size": 8,
                "fast_path_entities": {"rfc": 0.97},
            },
        )

        columns = ["saldo_actual", "nombre_cliente", "rfc_cte"]
        res = predictor.predict(columns)

        self.assertFalse(res["saldo_actual"].is_pii_name)
        self.assertIsNone(res["saldo_actual"].expected_entity)

        self.assertEqual(entity.calls, 1)
        self.assertEqual(entity.seen, ["nombre_cliente"])
        self.assertEqual(res["nombre_cliente"].expected_entity, "nombre")
        self.assertEqual(res["nombre_cliente"].source, "model")

        self.assertTrue(res["rfc_cte"].is_pii_name)
        self.assertEqual(res["rfc_cte"].expected_entity, "rfc")
        self.assertEqual(res["rfc_cte"].source, "rules")
        self.assertGreaterEqual(res["rfc_cte"].prob_entity_name, 0.85)

        # Segunda ejecución: todo debe venir de caché por nombre normalizado.
        predictor.predict(columns)
        self.assertEqual(binary.calls, 1)
        self.assertEqual(entity.calls, 1)

    def test_content_model_single_pass_and_model_version(self):
        class CountingContent:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def __init__(self):
                self.calls = 0

            def predict_proba(self, X):
                self.calls += 1
                rows = []
                for _ in X:
                    p = {"apellido": 0.01, "direccion": 0.01, "empresa": 0.01, "nombre": 0.95, "otro": 0.02}
                    total = sum(p.values())
                    rows.append([p[c] / total for c in self.classes_])
                return np.asarray(rows)

        model = CountingContent()
        ContentModelLoader.register_in_memory(
            "models/content/model.joblib",
            {
                "model": model,
                "classes": list(CountingContent.classes_),
                "temperature": 1.0,
                "tau": 0.50,
                "theta": 0.30,
                "other_label": "otro",
                "model_version": "content-vtest",
            },
        )
        try:
            df = pd.DataFrame({
                "campo_x": ["JUAN PEREZ LOPEZ", "MARIA GARCIA HERNANDEZ", "CARLOS SANCHEZ DIAZ"],
            })
            engine = PIIAnalyzer()
            res = engine.analyze(df, table_meta="tabla_single_pass")[0]

            self.assertTrue(res.pii)
            self.assertEqual(res.entity, "nombre")
            self.assertEqual(res.evidence, "content")
            # La columna se evalúa con 4 entidades D, pero `predict_proba` corre una sola vez.
            self.assertEqual(model.calls, 1)
            self.assertIn("content:content-vtest", res.model_version)
        finally:
            ContentModelLoader.clear_cache()

    def test_content_tau_theta_abstention(self):
        class LowConfidenceContent:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def __init__(self):
                self.calls = 0

            def predict_proba(self, X):
                self.calls += 1
                rows = []
                for _ in X:
                    p = {"apellido": 0.10, "direccion": 0.10, "empresa": 0.10, "nombre": 0.40, "otro": 0.30}
                    rows.append([p[c] for c in self.classes_])
                return np.asarray(rows)

        model = LowConfidenceContent()
        ContentModelLoader.register_in_memory(
            "models/content/model.joblib",
            {
                "model": model,
                "classes": list(LowConfidenceContent.classes_),
                "temperature": 1.0,
                "tau": 0.90,
                "theta": 0.80,
                "other_label": "otro",
                "model_version": "content-lowconf",
            },
        )
        try:
            df = pd.DataFrame({
                "campo_y": ["JUAN PEREZ LOPEZ", "MARIA GARCIA HERNANDEZ", "CARLOS SANCHEZ DIAZ"],
            })
            res = PIIAnalyzer().analyze(df, table_meta="tabla_abstencion")[0]

            self.assertFalse(res.pii)
            self.assertEqual(res.entity, NO_ENTITY)
            self.assertEqual(model.calls, 1)
        finally:
            ContentModelLoader.clear_cache()

    def test_analyzer_wires_name_bundles_and_content_path(self):
        class Binary:
            def predict_proba(self, X):
                return np.asarray([[0.10, 0.90] if "nombre" in str(x).lower() else [0.90, 0.10] for x in X])

        class Entity:
            classes_ = np.array(["nombre", "otro"])

            def predict_proba(self, X):
                return np.asarray([[0.95, 0.05] for _ in X])

        class Content:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def predict_proba(self, X):
                rows = []
                for _ in X:
                    p = {"apellido": 0.01, "direccion": 0.01, "empresa": 0.01, "nombre": 0.95, "otro": 0.02}
                    total = sum(p.values())
                    rows.append([p[c] / total for c in self.classes_])
                return np.asarray(rows)

        content_key = "custom/content/model.joblib"
        ContentModelLoader.register_in_memory(
            content_key,
            {
                "model": Content(),
                "classes": list(Content.classes_),
                "temperature": 1.0,
                "tau": 0.50,
                "theta": 0.30,
                "other_label": "otro",
                "model_version": "wired-v1",
            },
        )
        try:
            engine = PIIAnalyzer(
                binary_model_bundle={"model": Binary(), "threshold": 0.50},
                entity_model_bundle={
                    "model": Entity(),
                    "classes": list(Entity.classes_),
                    "temperature": 1.0,
                    "min_confidence": 0.45,
                },
                content_model_path=content_key,
            )
            df = pd.DataFrame({
                "nombre_cliente": ["JUAN PEREZ", "MARIA LOPEZ", "CARLOS DIAZ"],
                "campo_q": ["ANA TORRES", "LUIS RAMOS", "EVA SOTO"],
            })
            res = {r.column: r for r in engine.analyze(df, table_meta="tabla_wiring")}

            self.assertTrue(res["nombre_cliente"].pii)
            self.assertEqual(res["nombre_cliente"].entity, "nombre")
            self.assertTrue(res["campo_q"].pii)
            self.assertEqual(res["campo_q"].entity, "nombre")
            self.assertIn("content:wired-v1", res["campo_q"].model_version)
        finally:
            ContentModelLoader.clear_cache()

    def tearDown(self):
        ContentModelLoader.clear_cache()
        ContentModelScorer.clear_cache()


if __name__ == "__main__":
    unittest.main()
