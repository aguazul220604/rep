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


def _build_valid_nss(base10: str) -> str:
    total = 0
    for i, ch in enumerate(base10):
        p = int(ch) * (2 if i % 2 == 1 else 1)
        if p >= 10:
            p = (p // 10) + (p % 10)
        total += p
    check = (10 - (total % 10)) % 10
    return f"{base10}{check}"


class _CoverageContentModel:
    """Stub de contenido que separa las 5 clases del bundle para las pruebas de cobertura."""

    classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

    @classmethod
    def _label(cls, value: str) -> str:
        s = str(value).strip().upper()
        if " SA DE CV" in f" {s}" or "S.A." in s:
            return "empresa"
        if any(ch.isdigit() for ch in s) or any(ch in "+,;@/_-=:" for ch in s):
            if "CALLE" in s or "," in s:
                return "direccion"
            return "otro"
        tokens = s.split()
        if len(tokens) == 1 and s.replace(" ", "").isalpha():
            return "apellido"
        if len(tokens) >= 2 and all(token.isalpha() for token in tokens):
            return "nombre"
        return "otro"

    def predict_proba(self, X):
        rows = []
        for value in X:
            label = self._label(value)
            probs = {str(cls): 0.02 for cls in self.classes_}
            probs[label] = 0.92
            total = sum(probs.values())
            rows.append([probs[str(cls)] / total for cls in self.classes_])
        return np.asarray(rows)


def _coverage_fixture():
    """40 columnas sintéticas, una por entidad, con valores válidos y sin PII real."""
    return [
        ("curp", "curp", _build_valid_curp("GODE880515HDFRNS0")),
        ("rfc", "rfc", "GODE561231GR8"),
        ("clave_elector", "clave_elector", "GMRDLS85031209H200"),
        ("clabe", "clabe", _build_valid_clabe("00218001567891234")),
        ("iban", "iban", "ES9121000418450200051332"),
        ("tarjeta", "tarjeta", "4532015112830366"),
        ("correo", "correo", "usuario.prueba@citibanamex.com.mx"),
        ("ip", "ip", "192.168.1.254"),
        ("coordenadas", "coordenadas", "19.432608, -99.133209"),
        ("ssn", "ssn", "123-45-6789"),
        ("nss", "nss", _build_valid_nss("1290881234")),
        ("telefono", "telefono", "+52 55 4321 8765"),
        ("codigo_postal", "codigo_postal", "06600"),
        ("fecha_nacimiento", "fecha_nacimiento", "1980-01-15"),
        ("fecha_vencimiento", "fecha_vencimiento", "08/28"),
        ("pasaporte", "pasaporte", "A12345678"),
        ("licencia_conducir", "licencia_conducir", "ABC123456"),
        ("soeid", "soeid", "AB12345"),
        ("geid", "geid", "1234567890"),
        ("cic", "cic", "123456789"),
        ("ocr", "ocr", "123456789012"),
        ("cuenta", "cuenta", "123456"),
        ("cliente", "cliente", "CLI12345"),
        ("contrato", "contrato", "CTO-2024-001"),
        ("nomina", "nomina", "NOM12345"),
        ("cvv", "cvv", "123"),
        ("pin_nip", "pin_nip", "1234"),
        ("saldo", "saldo", "1500.00"),
        ("credito", "credito", "CRED1234"),
        ("huella", "biometrico", "QWxhZGRpbjpvcGVuIHNlc2FtZQ==abcdefgh"),
        ("lugar_nacimiento", "lugar_nacimiento", "Hidalgo"),
        ("nacionalidad", "nacionalidad", "mexicana"),
        ("sexo", "sexo", "masculino"),
        ("estado_civil", "estado_civil", "casado"),
        ("puesto", "puesto", "director"),
        ("nombre", "nombre", "JUAN PEREZ LOPEZ"),
        ("apellido", "apellido", "GONZALEZ"),
        ("direccion", "direccion", "CALLE REFORMA 123, CDMX"),
        ("empresa", "empresa", "BANCO NACIONAL SA DE CV"),
        ("edad", OTRO_PII, "34"),
    ]


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

    def test_allow_content_without_name_requires_validator(self):
        base_entity = {
            "classification": 1,
            "group": "B",
            "strategy": "regex",
            "patterns": [r"^\d{6,16}$"],
            "context_mode": "required",
            "min_match_rate": 0.65,
            "allow_content_without_name": True,
        }
        with self.assertRaises(ValueError):
            RecognizerRegistry({"version": "1.0", "entities": {"cuenta": base_entity}})

        with self.assertRaises(ValueError):
            RecognizerRegistry({
                "version": "1.0",
                "entities": {
                    "cuenta": {
                        **base_entity,
                        "validator": "curp_checksum",
                        "context_mode": "recommended",
                    }
                },
            })

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

    def test_sampling_without_size_returns_all_unique_values(self):
        values = [f"valor_{i}" for i in range(2500)] + ["valor_0"]
        s = pd.Series(values, name="alta_cardinalidad")
        samp = sample_column(s)
        self.assertEqual(samp.sample_size, 2500)

    def test_sampling_float_integer_values_have_no_decimal_suffix(self):
        s = pd.Series([7721630414.0, 42310.0, None], name="mezcla")
        samp = sample_column(s, sample_size=10)
        self.assertIn("7721630414", samp.values)
        self.assertIn("42310", samp.values)
        self.assertNotIn("7721630414.0", samp.values)
        self.assertNotIn("42310.0", samp.values)

        s_obj = pd.Series(["42310.0", "15420.50", None], name="mixto")
        samp_obj = sample_column(s_obj, sample_size=10)
        self.assertIn("42310", samp_obj.values)
        self.assertIn("15420.50", samp_obj.values)


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

    def test_float_phone_and_postal_code_with_nulls_are_pii(self):
        df = pd.DataFrame({
            "telefono": [7721630414.0, 7721630414.0, None],
            "codigo postal": [42310.0, 42310.0, None],
        })
        results = {r.column: r for r in analyze(df, table_meta="tabla_numerica")}

        self.assertTrue(results["telefono"].pii)
        self.assertEqual(results["telefono"].entity, "telefono")
        self.assertTrue(results["codigo postal"].pii)
        self.assertEqual(results["codigo postal"].entity, "codigo_postal")

    def test_iban_with_and_without_spaces(self):
        df = pd.DataFrame({
            "iban_con_espacios": ["ES91 2100 0418 45 0200051332"] * 3,
            "iban_compacto": ["ES9121000418450200051332"] * 3,
            "iban_invalido": ["ES0021000418450200051332"] * 3,
        })
        results = {r.column: r for r in analyze(df, table_meta="tabla_iban")}

        self.assertTrue(results["iban_con_espacios"].pii)
        self.assertEqual(results["iban_con_espacios"].entity, "iban")
        self.assertTrue(results["iban_compacto"].pii)
        self.assertEqual(results["iban_compacto"].entity, "iban")
        self.assertFalse(results["iban_invalido"].pii)

    def test_coordinates_content_detection(self):
        df = pd.DataFrame({"c": ["19.432608, -99.133209"] * 3})
        result = analyze(df, table_meta="tabla_coordenadas")[0]

        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "coordenadas")

    def test_valid_clabe_with_known_value(self):
        df = pd.DataFrame({"clabe": ["218001234567890103"] * 3})
        result = analyze(df, table_meta="tabla_clabe")[0]

        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "clabe")

    def test_clabe_without_leading_zero_is_not_validated(self):
        # 17 dígitos: se perdió el cero inicial, no debe enmascararse como CLABE válida.
        df = pd.DataFrame({"clabe": ["62226541894810392"] * 3})
        result = analyze(df, table_meta="tabla_clabe_truncada")[0]

        self.assertFalse(result.pii)
        self.assertEqual(result.entity, NO_ENTITY)

    def test_cvv_name_rule_rescues_low_binary_score(self):
        class LowBinary:
            def predict_proba(self, X):
                return np.asarray([[0.96, 0.04] for _ in X])

        engine = PIIAnalyzer(
            binary_model_bundle={"model": LowBinary(), "threshold": 0.409}
        )
        df = pd.DataFrame({"cvv": ["1674", "087", "5438", "150", "334", "1224"]})
        result = engine.analyze(df, table_meta="tabla_cvv")[0]

        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "cvv")
        self.assertGreaterEqual(result.prob_pii, 0.75)

    def test_codigo_postal_content_only_without_name(self):
        df = pd.DataFrame({"Unnamed: 5": ["72000", "42300", "72000"]})
        result = analyze(df, table_meta="tabla_cp")[0]

        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "codigo_postal")
        self.assertTrue(result.needs_review)

    def test_geid_content_only_without_name(self):
        df = pd.DataFrame({"Unnamed: 24": ["1011678043", "1011678043"]})
        result = analyze(df, table_meta="tabla_geid")[0]

        self.assertTrue(result.pii)
        self.assertEqual(result.entity, "geid")
        self.assertTrue(result.needs_review)

    def test_codigo_postal_and_geid_with_random_names(self):
        df = pd.DataFrame({
            "f": ["72000", "42300"],
            "y": ["1011678043", "1011678043"],
        })
        results = {r.column: r for r in analyze(df, table_meta="tabla_aleatoria")}

        self.assertTrue(results["f"].pii)
        self.assertEqual(results["f"].entity, "codigo_postal")
        self.assertTrue(results["f"].needs_review)
        self.assertTrue(results["y"].pii)
        self.assertEqual(results["y"].entity, "geid")
        self.assertTrue(results["y"].needs_review)

    def test_content_only_cp_geid_flags_named_columns_for_review(self):
        df = pd.DataFrame({
            "folio_5": ["72000", "42300"],
            "folio_10": ["1011678043", "1011678043"],
        })
        results = {r.column: r for r in analyze(df, table_meta="tabla_folios")}

        self.assertTrue(results["folio_5"].pii)
        self.assertEqual(results["folio_5"].entity, "codigo_postal")
        self.assertTrue(results["folio_5"].needs_review)
        self.assertTrue(results["folio_10"].pii)
        self.assertEqual(results["folio_10"].entity, "geid")
        self.assertTrue(results["folio_10"].needs_review)

    def test_unnamed_placeholder_blocks_required_and_name_only(self):
        engine = PIIAnalyzer()
        required_cases = {
            "fecha_nacimiento": "1980-01-15",
            "fecha_vencimiento": "08/28",
            "licencia_conducir": "ABC123456",
            "cic": "123456789",
            "ocr": "123456789012",
            "cuenta": "123456",
            "cliente": "CLI12345",
            "contrato": "CTO-2024-001",
            "nomina": "NOM12345",
        }
        name_only_cases = {
            "cvv": "123",
            "pin_nip": "1234",
            "saldo": "1500.00",
            "credito": "CRED1234",
            "biometrico": "QWxhZGRpbjpvcGVuIHNlc2FtZQ==abcdefgh",
        }

        for entity, value in {**required_cases, **name_only_cases}.items():
            sample = sample_column(
                pd.Series([value, value], name="Unnamed: 0"), sample_size=10
            )
            score = engine.registry.get(entity).score(sample, expected_by_name=False)
            self.assertFalse(score.passed_threshold, msg=entity)

    def test_unnamed_required_content_does_not_fire_end_to_end(self):
        df = pd.DataFrame({
            "Unnamed: 0": ["123456"] * 3,        # cuenta (required)
            "Unnamed: 1": ["1980-01-15"] * 3,    # fecha_nacimiento (required)
            "Unnamed: 2": ["ABC123456"] * 3,     # licencia_conducir (required)
            "Unnamed: 3": ["123"] * 3,           # cvv (name_only)
        })
        results = PIIAnalyzer().analyze(df, table_meta="tabla_required_sin_nombre")

        for result in results:
            self.assertFalse(result.pii, msg=result.column)

    def _analyze_coverage_fixture(self, named: bool):
        fixture = _coverage_fixture()
        content_key = "test/coverage/model.pkl"
        ContentModelLoader.register_in_memory(
            content_key,
            {
                "model": _CoverageContentModel(),
                "classes": list(_CoverageContentModel.classes_),
                "temperature": 1.0,
                "tau": 0.50,
                "theta": 0.30,
                "other_label": "otro",
                "model_version": "coverage-test",
            },
        )
        try:
            engine = PIIAnalyzer(content_model_path=content_key)
            data = {}
            for i, (name, _, value) in enumerate(fixture):
                column = name if named else f"Unnamed: {i}"
                data[column] = [value] * 3
            results = engine.analyze(pd.DataFrame(data), table_meta="cobertura")
            return {r.column: r for r in results}, fixture
        finally:
            ContentModelLoader.clear_cache()
            ContentModelScorer.clear_cache()

    def test_named_coverage_is_40_of_40_with_rules(self):
        results, fixture = self._analyze_coverage_fixture(named=True)

        for name, expected_entity, _ in fixture:
            self.assertTrue(results[name].pii, msg=name)
            self.assertEqual(results[name].entity, expected_entity, msg=name)

    def test_unnamed_coverage_is_25_of_40(self):
        excluded = {
            "fecha_nacimiento",
            "fecha_vencimiento",
            "licencia_conducir",
            "cic",
            "ocr",
            "cuenta",
            "cliente",
            "contrato",
            "nomina",
            "cvv",
            "pin_nip",
            "saldo",
            "credito",
            "huella",
            "edad",
        }
        results, fixture = self._analyze_coverage_fixture(named=False)
        detected_entities = set()

        for i, (name, expected_entity, _) in enumerate(fixture):
            result = results[f"Unnamed: {i}"]
            if name in excluded:
                self.assertFalse(result.pii, msg=name)
            else:
                self.assertTrue(result.pii, msg=name)
                self.assertEqual(result.entity, expected_entity, msg=name)
                detected_entities.add(result.entity)

        self.assertEqual(len(detected_entities), 25)
        self.assertEqual(sum(1 for r in results.values() if r.pii), 25)

    def test_otro_pii_rule_marks_review_when_binary_gate_skips_name(self):
        class LowBinary:
            def predict_proba(self, X):
                return np.asarray([[0.90, 0.10] for _ in X])

        engine = PIIAnalyzer(
            binary_model_bundle={"model": LowBinary(), "threshold": 0.50}
        )
        df = pd.DataFrame({"edad": [34, 35, 36]})
        result = engine.analyze(df, table_meta="tabla_edad")[0]

        self.assertFalse(result.pii)
        self.assertEqual(result.entity, NO_ENTITY)
        self.assertTrue(result.needs_review)

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

        ContentModelLoader.register_in_memory("model_content_v1/model.pkl", DummyContentModel())
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
        ContentModelLoader.register_in_memory("model_content_v1/model.pkl", bundle)
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
        self.assertEqual(res["saldo_actual"].rule_entity, "saldo")

        self.assertEqual(entity.calls, 1)
        self.assertEqual(entity.seen, ["nombre_cliente"])
        self.assertEqual(res["nombre_cliente"].expected_entity, "nombre")
        self.assertEqual(res["nombre_cliente"].source, "model")

        self.assertTrue(res["rfc_cte"].is_pii_name)
        self.assertEqual(res["rfc_cte"].expected_entity, "rfc")
        self.assertEqual(res["rfc_cte"].source, "rules")
        self.assertGreaterEqual(res["rfc_cte"].prob_entity_name, 0.85)

        # Segunda ejecución: todo debe venir de caché por nombre normalizado.
        cached = predictor.predict(columns)
        self.assertEqual(binary.calls, 1)
        self.assertEqual(entity.calls, 1)
        self.assertEqual(cached["saldo_actual"].rule_entity, "saldo")

    def test_fast_path_rule_rescues_binary_false_negative(self):
        class LowBinary:
            def predict_proba(self, X):
                return np.asarray([[0.96, 0.04] for _ in X])

        predictor = NameModelPredictor(
            binary_bundle={"model": LowBinary(), "threshold": 0.409},
            name_resolution_cfg={
                "binary_gate_enabled": True,
                "fast_path_enabled": True,
                "fast_path_min_confidence": 0.85,
                "fast_path_entities": {"cvv": 0.95},
            },
        )
        res = predictor.predict(["cvv"])["cvv"]

        self.assertTrue(res.is_pii_name)
        self.assertEqual(res.expected_entity, "cvv")
        self.assertGreaterEqual(res.score_name, 0.95)
        self.assertEqual(res.source, "rules")

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
            "model_content_v1/model.pkl",
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
            "model_content_v1/model.pkl",
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

        content_key = "custom/content/model.pkl"
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

    def test_scan_prefers_dictionary_over_content_model(self):
        class Binary:
            def predict_proba(self, X):
                return np.asarray([[0.90, 0.10] for _ in X])

        class Content:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def predict_proba(self, X):
                rows = []
                for _ in X:
                    p = {"apellido": 0.95, "direccion": 0.01, "empresa": 0.01, "nombre": 0.01, "otro": 0.02}
                    total = sum(p.values())
                    rows.append([p[c] / total for c in self.classes_])
                return np.asarray(rows)

        content_key = "test/sexo/model.pkl"
        ContentModelLoader.register_in_memory(
            content_key,
            {
                "model": Content(),
                "classes": list(Content.classes_),
                "temperature": 1.0,
                "tau": 0.50,
                "theta": 0.30,
                "other_label": "otro",
                "model_version": "sexo-test",
            },
        )
        try:
            engine = PIIAnalyzer(
                binary_model_bundle={"model": Binary(), "threshold": 0.50},
                content_model_path=content_key,
            )
            df = pd.DataFrame({"s": ["masculino", "femenino", "masculino"]})
            result = engine.analyze(df, table_meta="tabla_sexo")[0]

            self.assertTrue(result.pii)
            self.assertEqual(result.entity, "sexo")
        finally:
            ContentModelLoader.clear_cache()

    def test_unnamed_empty_column_is_not_pii(self):
        class Binary:
            def predict_proba(self, X):
                return np.asarray([[0.05, 0.95] for _ in X])

        class Entity:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def predict_proba(self, X):
                rows = []
                for _ in X:
                    p = {"apellido": 0.02, "direccion": 0.02, "empresa": 0.02, "nombre": 0.90, "otro": 0.04}
                    rows.append([p[c] for c in self.classes_])
                return np.asarray(rows)

        engine = PIIAnalyzer(
            binary_model_bundle={"model": Binary(), "threshold": 0.50},
            entity_model_bundle={
                "model": Entity(),
                "classes": list(Entity.classes_),
                "temperature": 1.0,
                "min_confidence": 0.45,
            },
        )
        df = pd.DataFrame({"Unnamed: 13": [None, None, None]})
        result = engine.analyze(df, table_meta="tabla_unnamed")[0]

        self.assertFalse(result.pii)
        self.assertEqual(result.entity, NO_ENTITY)

    def test_lugar_nacimiento_content_only_without_name(self):
        class Content:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def predict_proba(self, X):
                rows = []
                for _ in X:
                    p = {"apellido": 0.95, "direccion": 0.01, "empresa": 0.01, "nombre": 0.01, "otro": 0.02}
                    total = sum(p.values())
                    rows.append([p[c] / total for c in self.classes_])
                return np.asarray(rows)

        content_key = "test/lugar/model.pkl"
        ContentModelLoader.register_in_memory(
            content_key,
            {
                "model": Content(),
                "classes": list(Content.classes_),
                "temperature": 1.0,
                "tau": 0.50,
                "theta": 0.30,
                "other_label": "otro",
                "model_version": "lugar-test",
            },
        )
        try:
            engine = PIIAnalyzer(content_model_path=content_key)
            df = pd.DataFrame({
                "Unnamed: 7": ["Hidalgo", "Guadalajara", "Querétaro", "Ciudad de México", "Michoacán"],
            })
            result = engine.analyze(df, table_meta="tabla_lugar")[0]

            self.assertTrue(result.pii)
            self.assertEqual(result.entity, "lugar_nacimiento")
            self.assertTrue(result.needs_review)
        finally:
            ContentModelLoader.clear_cache()

    def test_address_context_wins_over_lugar_nacimiento(self):
        class Binary:
            def predict_proba(self, X):
                return np.asarray([[0.90, 0.10] for _ in X])

        class Content:
            classes_ = np.array(["apellido", "direccion", "empresa", "nombre", "otro"])

            def predict_proba(self, X):
                rows = []
                for _ in X:
                    p = {"apellido": 0.01, "direccion": 0.95, "empresa": 0.01, "nombre": 0.01, "otro": 0.02}
                    total = sum(p.values())
                    rows.append([p[c] / total for c in self.classes_])
                return np.asarray(rows)

        content_key = "test/direccion/model.pkl"
        ContentModelLoader.register_in_memory(
            content_key,
            {
                "model": Content(),
                "classes": list(Content.classes_),
                "temperature": 1.0,
                "tau": 0.50,
                "theta": 0.30,
                "other_label": "otro",
                "model_version": "direccion-test",
            },
        )
        try:
            engine = PIIAnalyzer(
                binary_model_bundle={"model": Binary(), "threshold": 0.50},
                content_model_path=content_key,
            )
            df = pd.DataFrame({
                "estado": ["Hidalgo", "Guadalajara", "Querétaro", "Ciudad de México", "Michoacán"],
            })
            result = engine.analyze(df, table_meta="tabla_direccion")[0]

            self.assertTrue(result.pii)
            self.assertEqual(result.entity, "direccion")
        finally:
            ContentModelLoader.clear_cache()

    def tearDown(self):
        ContentModelLoader.clear_cache()
        ContentModelScorer.clear_cache()


if __name__ == "__main__":
    unittest.main()
