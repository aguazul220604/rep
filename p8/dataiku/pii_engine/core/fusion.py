# -*- coding: utf-8 -*-
"""
Fusión de evidencia (nombre + contenido), calibración y reglas de decisión
(Secciones 5.4 y 5.5 de la documentación).
"""
import math
from typing import Any, Dict, Optional

from pii_engine.core.schema import (
    ENGINE_VERSION,
    NO_ENTITY,
    OTRO_PII,
    SCHEMA_VERSION,
    ColumnResult,
    ColumnSample,
    EntityScore,
    NamePrediction,
)

# Familias de entidades para detectar si un conflicto nombre/contenido es dentro de la misma
# familia (ej. nombre vs apellido, clabe vs cuenta) o entre familias distintas.
_ENTITY_FAMILIES: Dict[str, str] = {
    "curp": "gov_id",
    "rfc": "gov_id",
    "clave_elector": "gov_id",
    "cic": "gov_id",
    "ocr": "gov_id",
    "pasaporte": "gov_id",
    "licencia_conducir": "gov_id",
    "ssn": "gov_id",
    "nss": "gov_id",
    "clabe": "financial_acct",
    "iban": "financial_acct",
    "tarjeta": "financial_acct",
    "cuenta": "financial_acct",
    "credito": "financial_acct",
    "contrato": "financial_acct",
    "cliente": "internal_id",
    "nomina": "internal_id",
    "soeid": "internal_id",
    "geid": "internal_id",
    "nombre": "person_name",
    "apellido": "person_name",
    "direccion": "location",
    "lugar_nacimiento": "location",
    "codigo_postal": "location",
    "coordenadas": "location",
    "fecha_nacimiento": "date",
    "fecha_vencimiento": "date",
    "correo": "contact",
    "telefono": "contact",
}


def _logit(p: float, eps: float = 1e-4) -> float:
    p_clipped = max(eps, min(1.0 - eps, float(p)))
    return math.log(p_clipped / (1.0 - p_clipped))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def same_entity_family(ent_a: Optional[str], ent_b: Optional[str]) -> bool:
    if not ent_a or not ent_b:
        return False
    if ent_a == ent_b:
        return True
    fam_a = _ENTITY_FAMILIES.get(ent_a)
    fam_b = _ENTITY_FAMILIES.get(ent_b)
    return fam_a is not None and fam_a == fam_b


class EvidenceFuser:
    """
    Aplica las reglas de decisión de la Tabla 5.4 y la fórmula de fusión calibrada
    de la Sección 5.5.
    """

    def __init__(self, fusion_cfg: Optional[Dict[str, Any]] = None, name_cfg: Optional[Dict[str, Any]] = None):
        f_cfg = fusion_cfg or {}
        n_cfg = name_cfg or {}
        self.w0 = float(f_cfg.get("w0", -0.25))
        self.w1 = float(f_cfg.get("w1", 0.55))
        self.w2 = float(f_cfg.get("w2", 0.65))
        self.w3 = float(f_cfg.get("w3", 0.90))
        self.eps = float(f_cfg.get("epsilon", 1e-4))
        self.min_sample_full = int(f_cfg.get("min_sample_for_full_weight", 50))
        self.pii_threshold = float(f_cfg.get("pii_decision_threshold", 0.50))
        self.empty_content_damping = float(f_cfg.get("empty_content_damping", 0.72))
        self.unconfirmed_name_damping = float(f_cfg.get("unconfirmed_name_damping", 0.35))
        self.high_conf_name_score = float(n_cfg.get("high_confidence_name_score", 0.80))

    def adjust_match_rate(self, match_rate: float, sample_size: int) -> float:
        """Penaliza el `match_rate` cuando la muestra tiene muy pocos valores únicos."""
        if sample_size <= 0:
            return 0.0
        coverage_factor = min(1.0, math.sqrt(sample_size / max(1, self.min_sample_full)))
        return float(match_rate) * coverage_factor

    def compute_calibrated_prob(
        self,
        score_name: float,
        match_rate_adj: float,
        has_strong_validator: bool,
    ) -> float:
        """
        prob_pii = calibrar( w0 + w1 * logit(score_nombre) + w2 * logit(match_rate_ajustado) + w3 * bonus_validador )
        """
        bonus = 1.0 if has_strong_validator and match_rate_adj > 0.5 else 0.0
        z = (
            self.w0
            + self.w1 * _logit(score_name, self.eps)
            + self.w2 * _logit(match_rate_adj, self.eps)
            + self.w3 * bonus
        )
        return _sigmoid(z)

    def fuse(
        self,
        table_name: str,
        sample: ColumnSample,
        name_pred: NamePrediction,
        expected_content_score: Optional[EntityScore],
        scanned_content_score: Optional[EntityScore],
        engine_version: str = ENGINE_VERSION,
        model_version: str = "rules_fallback",
        config_version: str = "1.0",
    ) -> ColumnResult:
        col = sample.column
        n = sample.sample_size

        # -------------------------------------------------------------------
        # Caso 0: Clasificación 2 (`otro_pii`) detectada por nombre
        # -------------------------------------------------------------------
        if name_pred.is_pii_name and name_pred.expected_entity == OTRO_PII:
            # Si hay un match fuerte del Grupo A en contenido (ej. una CURP o RFC dentro de columna ambigua),
            # el contenido con dígito verificador tiene prioridad; si no, sale como otro_pii con needs_review=True.
            if scanned_content_score is not None and scanned_content_score.passed_threshold and scanned_content_score.group == "A":
                adj_mr = self.adjust_match_rate(scanned_content_score.match_rate, n)
                prob_pii = max(
                    0.80,
                    self.compute_calibrated_prob(
                        name_pred.score_name, adj_mr, scanned_content_score.used_validator
                    ),
                )
                return ColumnResult(
                    table=table_name,
                    column=col,
                    pii=True,
                    prob_pii=prob_pii,
                    entity=scanned_content_score.entity,
                    prob_entity=scanned_content_score.score,
                    evidence="content",
                    match_rate=scanned_content_score.match_rate,
                    sample_size=n,
                    needs_review=True,
                    classification=1,
                    engine_version=engine_version,
                    model_version=model_version,
                    config_version=config_version,
                    schema_version=SCHEMA_VERSION,
                )

            prob_pii = max(0.55, name_pred.score_name * 0.85)
            return ColumnResult(
                table=table_name,
                column=col,
                pii=True,
                prob_pii=prob_pii,
                entity=OTRO_PII,
                prob_entity=max(0.50, name_pred.prob_entity_name),
                evidence="name",
                match_rate=0.0,
                sample_size=n,
                needs_review=True,
                classification=2,
                engine_version=engine_version,
                model_version=model_version,
                config_version=config_version,
                schema_version=SCHEMA_VERSION,
            )

        # -------------------------------------------------------------------
        # Caso 1: Contenido vacío o todo nulo (`sample_size == 0`)
        # -------------------------------------------------------------------
        if n == 0:
            if name_pred.is_pii_name:
                ent = name_pred.expected_entity or OTRO_PII
                cls_id = 2 if ent == OTRO_PII else 1
                prob_pii = max(0.51, name_pred.score_name * self.empty_content_damping)
                prob_ent = max(0.40, name_pred.prob_entity_name * self.empty_content_damping)
                return ColumnResult(
                    table=table_name,
                    column=col,
                    pii=True,
                    prob_pii=prob_pii,
                    entity=ent,
                    prob_entity=prob_ent,
                    evidence="name",
                    match_rate=0.0,
                    sample_size=0,
                    needs_review=True,
                    classification=cls_id,
                    engine_version=engine_version,
                    model_version=model_version,
                    config_version=config_version,
                    schema_version=SCHEMA_VERSION,
                )
            else:
                prob_pii = min(0.45, name_pred.score_name)
                return ColumnResult(
                    table=table_name,
                    column=col,
                    pii=False,
                    prob_pii=prob_pii,
                    entity=NO_ENTITY,
                    prob_entity=1.0 - prob_pii,
                    evidence="name",
                    match_rate=0.0,
                    sample_size=0,
                    needs_review=False,
                    classification=1,
                    engine_version=engine_version,
                    model_version=model_version,
                    config_version=config_version,
                    schema_version=SCHEMA_VERSION,
                )

        # -------------------------------------------------------------------
        # Caso 2: Nombre sugiere PII y el contenido valida la entidad esperada
        # -------------------------------------------------------------------
        if (
            name_pred.is_pii_name
            and expected_content_score is not None
            and expected_content_score.passed_threshold
        ):
            adj_mr = self.adjust_match_rate(expected_content_score.match_rate, n)
            raw_prob = self.compute_calibrated_prob(
                name_pred.score_name, adj_mr, expected_content_score.used_validator
            )
            prob_pii = max(0.75, raw_prob, expected_content_score.score)
            prob_ent = max(
                expected_content_score.score,
                0.5 * name_pred.prob_entity_name + 0.5 * expected_content_score.match_rate,
            )
            return ColumnResult(
                table=table_name,
                column=col,
                pii=True,
                prob_pii=min(0.999, prob_pii),
                entity=expected_content_score.entity,
                prob_entity=min(0.999, prob_ent),
                evidence="both",
                match_rate=expected_content_score.match_rate,
                sample_size=n,
                needs_review=False,
                classification=expected_content_score.classification,
                engine_version=engine_version,
                model_version=model_version,
                config_version=config_version,
                schema_version=SCHEMA_VERSION,
            )

        # -------------------------------------------------------------------
        # Caso 3: Nombre sugiere PII, NO valida la esperada, pero valida otra entidad en el escaneo
        # -------------------------------------------------------------------
        if (
            name_pred.is_pii_name
            and scanned_content_score is not None
            and scanned_content_score.passed_threshold
        ):
            adj_mr = self.adjust_match_rate(scanned_content_score.match_rate, n)
            raw_prob = self.compute_calibrated_prob(
                name_pred.score_name, adj_mr, scanned_content_score.used_validator
            )
            prob_pii = max(0.65, raw_prob, scanned_content_score.score)
            # Conflicto nombre vs contenido: si pertenecen a distinta familia y no tiene dígito verificador fuerte,
            # o si el nombre tenía alta confianza en otra familia, marcamos needs_review
            in_same_family = same_entity_family(
                name_pred.expected_entity, scanned_content_score.entity
            )
            needs_rev = not in_same_family
            return ColumnResult(
                table=table_name,
                column=col,
                pii=True,
                prob_pii=min(0.999, prob_pii),
                entity=scanned_content_score.entity,
                prob_entity=min(0.999, scanned_content_score.score),
                evidence="content",
                match_rate=scanned_content_score.match_rate,
                sample_size=n,
                needs_review=needs_rev,
                classification=scanned_content_score.classification,
                engine_version=engine_version,
                model_version=model_version,
                config_version=config_version,
                schema_version=SCHEMA_VERSION,
            )

        # -------------------------------------------------------------------
        # Caso 4: Nombre sugiere PII, hay contenido, pero ninguna entidad valida
        # -------------------------------------------------------------------
        if name_pred.is_pii_name:
            # Si la entidad esperada es del Grupo D (modelo de contenido) y el modelo aún no está cargado
            # (match_rate == 0 y score == 0 por abstención), mantenemos la predicción por nombre con needs_review=True
            if (
                expected_content_score is not None
                and expected_content_score.group == "D"
                and expected_content_score.raw_match_rate == 0.0
                and not sample.is_numeric_dtype
            ):
                prob_pii = max(0.55, name_pred.score_name * self.empty_content_damping)
                return ColumnResult(
                    table=table_name,
                    column=col,
                    pii=True,
                    prob_pii=prob_pii,
                    entity=expected_content_score.entity,
                    prob_entity=max(0.50, name_pred.prob_entity_name * self.empty_content_damping),
                    evidence="name",
                    match_rate=0.0,
                    sample_size=n,
                    needs_review=True,
                    classification=1,
                    engine_version=engine_version,
                    model_version=model_version,
                    config_version=config_version,
                    schema_version=SCHEMA_VERSION,
                )

            best_mr = (
                expected_content_score.match_rate
                if expected_content_score is not None
                else 0.0
            )
            reduced_prob = min(0.45, name_pred.score_name * self.unconfirmed_name_damping)
            needs_rev = name_pred.score_name >= self.high_conf_name_score
            return ColumnResult(
                table=table_name,
                column=col,
                pii=False,
                prob_pii=reduced_prob,
                entity=NO_ENTITY,
                prob_entity=1.0 - reduced_prob,
                evidence="content",
                match_rate=best_mr,
                sample_size=n,
                needs_review=needs_rev,
                classification=1,
                engine_version=engine_version,
                model_version=model_version,
                config_version=config_version,
                schema_version=SCHEMA_VERSION,
            )

        # -------------------------------------------------------------------
        # Caso 5: Nombre NO sugiere PII, pero el contenido valida una entidad elegible
        # -------------------------------------------------------------------
        if scanned_content_score is not None and scanned_content_score.passed_threshold:
            adj_mr = self.adjust_match_rate(scanned_content_score.match_rate, n)
            cal_prob = self.compute_calibrated_prob(
                max(0.25, name_pred.score_name),
                adj_mr,
                scanned_content_score.used_validator,
            )
            prob_pii = max(0.60, cal_prob, scanned_content_score.score)
            needs_rev = scanned_content_score.group != "A" and not scanned_content_score.context_matched
            return ColumnResult(
                table=table_name,
                column=col,
                pii=True,
                prob_pii=min(0.999, prob_pii),
                entity=scanned_content_score.entity,
                prob_entity=min(0.999, scanned_content_score.score),
                evidence="content",
                match_rate=scanned_content_score.match_rate,
                sample_size=n,
                needs_review=needs_rev,
                classification=scanned_content_score.classification,
                engine_version=engine_version,
                model_version=model_version,
                config_version=config_version,
                schema_version=SCHEMA_VERSION,
            )

        # -------------------------------------------------------------------
        # Caso 6: Nombre NO sugiere PII y ninguna entidad valida en el contenido
        # -------------------------------------------------------------------
        best_mr = scanned_content_score.match_rate if scanned_content_score is not None else 0.0
        low_prob = min(0.20, name_pred.score_name * 0.5)
        return ColumnResult(
            table=table_name,
            column=col,
            pii=False,
            prob_pii=low_prob,
            entity=NO_ENTITY,
            prob_entity=1.0 - low_prob,
            evidence="both",
            match_rate=best_mr,
            sample_size=n,
            needs_review=False,
            classification=1,
            engine_version=engine_version,
            model_version=model_version,
            config_version=config_version,
            schema_version=SCHEMA_VERSION,
        )
