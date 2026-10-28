# -*- coding: utf-8 -*-
"""
Motor agnóstico de detección e identificación de datos PII (`pii_engine`).
"""
from pii_engine.core.analyzer import PIIAnalyzer, analyze
from pii_engine.core.schema import (
    ENGINE_VERSION,
    NO_ENTITY,
    OTRO_PII,
    SCHEMA_VERSION,
    ColumnResult,
    ColumnSample,
    EntityScore,
    NamePrediction,
    TableMeta,
)
from pii_engine.recognizers.registry import RecognizerRegistry

__all__ = [
    "ENGINE_VERSION",
    "SCHEMA_VERSION",
    "NO_ENTITY",
    "OTRO_PII",
    "PIIAnalyzer",
    "analyze",
    "ColumnResult",
    "ColumnSample",
    "EntityScore",
    "NamePrediction",
    "TableMeta",
    "RecognizerRegistry",
]
