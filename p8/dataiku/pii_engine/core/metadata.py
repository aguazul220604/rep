# -*- coding: utf-8 -*-
"""
Análisis PII basado únicamente en nombres de columnas o elementos de dato.

Este módulo está pensado para escenarios de metadata-only, donde no se dispone
del contenido de las columnas y solo se conocen sus nombres (por ejemplo, la
columna `DataElem` de una tabla de metadata).

Reutiliza `NameModelPredictor` (modelo binario + modelo de entidad + reglas de
`regex.txt`) y añade un refuerzo explícito: cualquier coincidencia de las reglas
de nombre cuenta como PII, aunque el modelo binario no la haya marcado.
"""
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Optional

from pii_engine.core.name_models import NameModelPredictor
from pii_engine.core.schema import NO_ENTITY, OTRO_PII, NamePrediction


@dataclass
class MetadataPrediction:
    """Resultado de evaluar un nombre de columna/elemento sin contenido."""

    element: str
    normalized_name: str
    is_pii: bool
    entity: str
    prob_pii: float
    prob_entity: float
    needs_review: bool
    source: str
    score_binary: float
    regex_entity: Optional[str]
    entity_top1: Optional[str]
    abstained: bool
    classification: int
    evaluable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["prob_pii"] = round(float(self.prob_pii), 4)
        data["prob_entity"] = round(float(self.prob_entity), 4)
        data["score_binary"] = round(float(self.score_binary), 4)
        return data


def _as_probability(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class MetadataNameAnalyzer:
    """
    Evalúa nombres de columna/elemento usando los modelos de nombre y regex.

    La decisión es:

      pii = modelo_binario_pii OR regla_de_nombre

    La entidad se toma, por prioridad, del modelo de entidad, de la regla de
    nombre o del top-1 del modelo. Si no hay señal suficiente se usa `otro_pii`
    y se marca `needs_review`.
    """

    def __init__(
        self,
        predictor: Optional[NameModelPredictor] = None,
        regex_confidence: float = 0.85,
        entity_min_confidence: float = 0.45,
    ):
        self.predictor = predictor or NameModelPredictor()
        self.regex_confidence = _as_probability(regex_confidence, 0.85)
        self.entity_min_confidence = _as_probability(entity_min_confidence, 0.45)

    @property
    def model_version_tag(self) -> str:
        return getattr(self.predictor, "model_version_tag", "rules")

    def clear_cache(self) -> None:
        clear = getattr(self.predictor, "clear_cache", None)
        if callable(clear):
            clear()

    def predict(self, elements: Iterable[str]) -> Dict[str, MetadataPrediction]:
        """
        Devuelve un `MetadataPrediction` por elemento. El diccionario conserva
        el texto original de cada elemento como clave.
        """
        unique: list[str] = []
        seen: set[str] = set()
        for raw in elements or []:
            text = "" if raw is None else str(raw).strip()
            if text in seen:
                continue
            seen.add(text)
            unique.append(text)

        non_empty = [text for text in unique if text]
        predictions = self.predictor.predict(non_empty) if non_empty else {}

        results: Dict[str, MetadataPrediction] = {}
        for text in unique:
            if not text:
                results[text] = self._empty_prediction(text)
                continue
            pred = predictions.get(text)
            if pred is None:
                results[text] = self._empty_prediction(text)
                continue
            results[text] = self._build_prediction(text, pred)
        return results

    @staticmethod
    def _empty_prediction(element: str) -> MetadataPrediction:
        return MetadataPrediction(
            element=element,
            normalized_name="",
            is_pii=False,
            entity=NO_ENTITY,
            prob_pii=0.0,
            prob_entity=0.0,
            needs_review=True,
            source="none",
            score_binary=0.0,
            regex_entity=None,
            entity_top1=None,
            abstained=True,
            classification=1,
            evaluable=False,
        )

    def _build_prediction(
        self,
        element: str,
        pred: NamePrediction,
    ) -> MetadataPrediction:
        regex_entity = pred.rule_entity
        model_is_pii = bool(pred.is_pii_name)
        is_pii = model_is_pii or regex_entity is not None

        if not is_pii:
            prob_pii = min(0.45, _as_probability(pred.score_name))
            return MetadataPrediction(
                element=element,
                normalized_name=pred.normalized_name,
                is_pii=False,
                entity=NO_ENTITY,
                prob_pii=prob_pii,
                prob_entity=0.0,
                needs_review=False,
                source="none",
                score_binary=_as_probability(pred.score_name),
                regex_entity=None,
                entity_top1=pred.entity_top1,
                abstained=bool(pred.abstained),
                classification=1,
            )

        # Entidad: el modelo manda si no se abstuvo; después la regla de nombre.
        entity: Optional[str] = None
        if pred.expected_entity and not pred.abstained:
            entity = pred.expected_entity
        if entity is None and regex_entity:
            entity = regex_entity
        if entity is None and pred.entity_top1 and not pred.abstained:
            entity = pred.entity_top1
        if entity is None:
            entity = OTRO_PII

        if model_is_pii:
            prob_pii = _as_probability(pred.score_name)
            if pred.source == "rules":
                source = "rules"
            elif regex_entity:
                source = "both"
            else:
                source = "model"
            prob_entity = _as_probability(pred.prob_entity_name)
            if prob_entity <= 0.0 and entity != OTRO_PII:
                prob_entity = self.regex_confidence if regex_entity else max(
                    self.entity_min_confidence, prob_pii
                )
        else:
            prob_pii = max(_as_probability(pred.score_name), self.regex_confidence)
            prob_entity = self.regex_confidence
            source = "rules"

        if entity == OTRO_PII:
            prob_entity = max(prob_entity, 0.5)

        # Revisión: abstención, `otro_pii`, rescate por regex o desacuerdo de
        # entidad entre nombre y modelo.
        disagreement = bool(
            regex_entity
            and pred.expected_entity
            and regex_entity != pred.expected_entity
        )
        needs_review = bool(
            entity == OTRO_PII
            or pred.abstained
            or (regex_entity is not None and not model_is_pii)
            or disagreement
        )

        classification = 2 if entity == OTRO_PII else 1
        return MetadataPrediction(
            element=element,
            normalized_name=pred.normalized_name,
            is_pii=True,
            entity=entity,
            prob_pii=prob_pii,
            prob_entity=prob_entity,
            needs_review=needs_review,
            source=source,
            score_binary=_as_probability(pred.score_name),
            regex_entity=regex_entity,
            entity_top1=pred.entity_top1,
            abstained=bool(pred.abstained),
            classification=classification,
        )
