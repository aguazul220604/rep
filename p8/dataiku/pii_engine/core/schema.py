# -*- coding: utf-8 -*-
"""
Contratos de datos y esquemas del motor de detección PII.
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

ENGINE_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
NO_ENTITY = "no aplica"
OTRO_PII = "otro_pii"


@dataclass(frozen=True)
class TableMeta:
    """Metadatos básicos de la tabla a analizar."""
    table: str
    source: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ColumnSample:
    """
    Muestra procesada de una columna (vive solo en memoria durante el análisis).
    """
    column: str
    dtype: str
    values: List[str]
    sample_size: int
    total_non_null: int
    is_free_text: bool = False
    is_numeric_dtype: bool = False
    mean_length: float = 0.0


@dataclass
class NamePrediction:
    """Resultado de los modelos de nombre (binario + multiclase de entidad)."""
    column: str
    normalized_name: str
    is_pii_name: bool
    score_name: float
    expected_entity: Optional[str] = None
    prob_entity_name: float = 0.0
    entity_top1: Optional[str] = None
    abstained: bool = False
    source: str = "rules"


@dataclass
class EntityScore:
    """Resultado de evaluar un Recognizer sobre la muestra de una columna."""
    entity: str
    group: str
    classification: int
    match_rate: float
    raw_match_rate: float
    score: float
    passed_threshold: bool
    used_validator: bool = False
    validator_passed_rate: float = 0.0
    context_matched: bool = False
    context_boost: float = 0.0
    sample_size: int = 0


@dataclass
class ColumnResult:
    """
    Contrato de salida por columna analizada (Sección 3 de la documentación).
    Nunca incluye valores ni ejemplos de la muestra.
    """
    table: str
    column: str
    pii: bool
    prob_pii: float
    entity: str
    prob_entity: float
    evidence: str
    match_rate: float
    sample_size: int
    needs_review: bool
    classification: int
    engine_version: str = ENGINE_VERSION
    model_version: str = "rules_fallback"
    config_version: str = "1.0"
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["prob_pii"] = round(float(self.prob_pii), 4)
        d["prob_entity"] = round(float(self.prob_entity), 4)
        d["match_rate"] = round(float(self.match_rate), 4)
        return d
