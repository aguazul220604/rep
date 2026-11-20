from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

import numpy as np
import pandas as pd

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    pl = None
    HAS_POLARS = False

from pii_engine.core.sampling import detect_free_text
from pii_engine.core.schema import ENGINE_VERSION
from pii_engine.recognizers.context import (
    is_placeholder_column_name,
    normalize_text_value,
)
from pii_engine.recognizers.dictionary_recognizer import DictionaryRecognizer
from pii_engine.recognizers.model_recognizer import (
    MAX_VALUE_LEN,
    MIN_VALUE_LEN,
    NULL_TOKENS,
    ContentModelScorer,
    ModelRecognizer,
    _is_numeric_token,
)
from pii_engine.recognizers.regex_recognizer import RegexRecognizer

_INTEGRAL_FLOAT_SUFFIX = re.compile(r"^(-?\d+)\.0+$")
_GROUP_RANK = {"A": 5, "C": 4, "B": 3, "D": 2, "E": 1}


@dataclass(frozen=True)
class DeepScanOptions:
    max_rows: Optional[int] = 500_000
    chunk_size: int = 50_000
    include_model: bool = True
    include_name_only: bool = True


@dataclass
class EntityFinding:
    entity: str
    group: str
    classification: int
    cells: int
    distribution_rate: float
    coverage_rate: float
    used_validator: bool = False
    context_matched: bool = False

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "group": self.group,
            "classification": self.classification,
            "cells": int(self.cells),
            "distribution_rate": round(float(self.distribution_rate), 6),
            "coverage_rate": round(float(self.coverage_rate), 6),
            "used_validator": bool(self.used_validator),
            "context_matched": bool(self.context_matched),
        }


@dataclass
class ColumnScanResult:
    column: str
    total_rows: int = 0
    rows_scanned: int = 0
    truncated: bool = False
    non_empty_values: int = 0
    empty_values: int = 0
    unique_values: int = 0
    unique_pii_values: int = 0
    pii_values: int = 0
    pii_rate: float = 0.0
    non_pii_values: int = 0
    non_pii_rate: float = 0.0
    entities: List[EntityFinding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    engine_version: str = ENGINE_VERSION
    model_version: Optional[str] = None
    config_version: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "total_rows": int(self.total_rows),
            "rows_scanned": int(self.rows_scanned),
            "truncated": bool(self.truncated),
            "non_empty_values": int(self.non_empty_values),
            "empty_values": int(self.empty_values),
            "unique_values": int(self.unique_values),
            "unique_pii_values": int(self.unique_pii_values),
            "pii_values": int(self.pii_values),
            "pii_rate": round(float(self.pii_rate), 6),
            "non_pii_values": int(self.non_pii_values),
            "non_pii_rate": round(float(self.non_pii_rate), 6),
            "entities": [item.to_dict() for item in self.entities],
            "warnings": list(self.warnings),
            "engine_version": self.engine_version,
            "model_version": self.model_version,
            "config_version": self.config_version,
        }


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _normalize_values(values: Iterable[Any]) -> List[str]:
    normalized: List[str] = []
    for value in values:
        if _is_missing(value):
            continue
        text = str(value).strip()
        if not text:
            continue
        text = _INTEGRAL_FLOAT_SUFFIX.sub(r"\1", text)
        if text:
            normalized.append(text)
    return normalized


def _extract_chunk(chunk: Any) -> tuple[List[Any], Optional[bool]]:
    if isinstance(chunk, pd.DataFrame):
        if chunk.shape[1] != 1:
            raise ValueError("El escaneo profundo espera una sola columna por chunk")
        series = chunk.iloc[:, 0]
    elif isinstance(chunk, pd.Series):
        series = chunk
    else:
        return list(chunk), None

    numeric = bool(pd.api.types.is_numeric_dtype(series.dtype)) and not bool(
        pd.api.types.is_bool_dtype(series.dtype)
    )
    return series.tolist(), numeric


def _unique_counts(values: List[str]) -> tuple[List[str], np.ndarray]:
    counts = pd.Series(values, dtype="object").value_counts(sort=False)
    return [str(value) for value in counts.index.tolist()], counts.to_numpy(
        dtype=np.int64
    )


def _context_state(recognizer: Any, column: str) -> tuple[bool, bool]:
    matched, _ = recognizer.context_enhancer.evaluate(column, expected_by_name=False)
    allow_content = bool(
        getattr(recognizer, "allow_content_without_name", False)
        and not matched
        and is_placeholder_column_name(column)
    )
    return bool(matched), allow_content


def _regex_detection(
    recognizer: RegexRecognizer,
    values: List[str],
    column: str,
    is_free_text: bool,
) -> tuple[Optional[np.ndarray], bool]:
    context_matched, allow_content = _context_state(recognizer, column)
    if recognizer.requires_context and not (context_matched or allow_content):
        return None, context_matched
    if is_free_text and not recognizer.allow_in_free_text:
        return None, context_matched

    use_free_text = bool(is_free_text and recognizer.allow_in_free_text)
    pattern = (
        recognizer._unanchored_pattern
        if use_free_text
        else recognizer._anchored_pattern
    )
    if not pattern:
        return None, context_matched

    if HAS_POLARS and pl is not None:
        series = pl.Series("v", values, dtype=pl.Utf8)
        mask = series.str.contains(pattern).to_numpy()
    else:
        compiled = (
            recognizer._py_extract_re if use_free_text else recognizer._py_anchored_re
        )
        if compiled is None:
            return None, context_matched
        mask = np.asarray(
            [bool(compiled.search(value)) for value in values], dtype=bool
        )

    if not mask.any():
        return mask, context_matched

    if recognizer.validator is not None:
        valid = np.zeros(len(values), dtype=bool)
        for index in np.nonzero(mask)[0]:
            value = values[index]
            candidate = value
            if use_free_text and recognizer._py_extract_re is not None:
                match = recognizer._py_extract_re.search(value)
                if match:
                    candidate = match.group(0)
            if recognizer.validator(candidate):
                valid[index] = True
        mask = valid

    return mask, context_matched


def _dictionary_detection(
    recognizer: DictionaryRecognizer,
    values: List[str],
    column: str,
) -> tuple[Optional[np.ndarray], bool]:
    context_matched, _ = _context_state(recognizer, column)
    if recognizer.requires_context and not context_matched:
        return None, context_matched
    if not recognizer.catalog:
        return None, context_matched

    normalized = [normalize_text_value(value) for value in values]
    mask = np.zeros(len(values), dtype=bool)
    for index, value in enumerate(normalized):
        if value in recognizer.catalog:
            mask[index] = True
        elif (
            recognizer._py_substring_re is not None
            and recognizer._py_substring_re.search(value)
        ):
            mask[index] = True

    if recognizer.entity == "sexo" and not context_matched:
        mean_len = sum(len(value) for value in normalized) / max(1, len(normalized))
        if mean_len <= 2.0:
            mask[:] = False

    return mask, context_matched


def _model_clean_indices(values: List[str]) -> List[int]:
    indices: List[int] = []
    for index, value in enumerate(values):
        text = str(value).strip()
        if not text or text.lower() in NULL_TOKENS:
            continue
        if len(text) < MIN_VALUE_LEN or _is_numeric_token(text):
            continue
        if len(text) > MAX_VALUE_LEN:
            text = text[:MAX_VALUE_LEN]
        if text:
            indices.append(index)
    return indices


def _model_detection(
    recognizer: ModelRecognizer,
    values: List[str],
    column: str,
    is_numeric_dtype: bool,
) -> tuple[Optional[np.ndarray], Optional[np.ndarray], bool, bool]:
    context_matched, _ = _context_state(recognizer, column)
    if is_numeric_dtype or not recognizer.model_path:
        return None, None, context_matched, False
    if recognizer.requires_context and not context_matched:
        return None, None, context_matched, False

    clean_indices = _model_clean_indices(values)
    if not clean_indices:
        return (
            np.zeros(len(values), dtype=bool),
            np.zeros(len(values), dtype=float),
            context_matched,
            False,
        )

    aggregated = ContentModelScorer.score(recognizer.model_path, values)
    if aggregated is None:
        return None, None, context_matched, True

    probabilities = np.asarray(aggregated.get("proba"), dtype=float)
    labels = [str(label) for label in aggregated.get("labels", [])]
    if probabilities.ndim != 2 or probabilities.shape[0] != len(clean_indices):
        return None, None, context_matched, False
    if not labels or probabilities.shape[1] != len(labels):
        return None, None, context_matched, False

    threshold = (
        float(aggregated.get("tau", recognizer.value_prob_threshold))
        if aggregated.get("has_metadata")
        else float(recognizer.value_prob_threshold)
    )

    top_indices = probabilities.argmax(axis=1)
    confidence = probabilities.max(axis=1)

    mask = np.zeros(len(values), dtype=bool)
    scores = np.zeros(len(values), dtype=float)
    for row, index in enumerate(clean_indices):
        label = labels[int(top_indices[row])]
        score = float(confidence[row])
        if label == recognizer.entity and score >= threshold:
            mask[index] = True
            scores[index] = score

    return mask, scores, context_matched, False


def _priority_values(
    recognizer: Any,
    context_matched: bool,
    mask: np.ndarray,
    scores: np.ndarray,
    recognizer_index: int,
    recognizer_count: int,
) -> np.ndarray:
    validator = bool(
        isinstance(recognizer, RegexRecognizer)
        and recognizer.has_strong_validator
        and mask.any()
    )
    base = (
        (1_000_000 if validator else 0)
        + (100_000 if context_matched else 0)
        + (_GROUP_RANK.get(recognizer.group, 0) * 10_000)
    )
    tie_break = (recognizer_count - recognizer_index) * 1e-6
    return np.where(mask, base + (scores * 1_000.0) + tie_break, 0.0)


def scan_column_chunks(
    analyzer: Any,
    chunks: Iterable[Any],
    column: str,
    options: Optional[DeepScanOptions] = None,
) -> ColumnScanResult:
    opts = options or DeepScanOptions()
    registry = getattr(analyzer, "registry", None)
    if registry is None:
        raise ValueError("El analizador no expone un registry de recognizers")

    recognizers = list(
        registry.get_scannable_recognizers(
            include_name_only=opts.include_name_only
        )
    )
    if not opts.include_model:
        recognizers = [
            recognizer
            for recognizer in recognizers
            if not isinstance(recognizer, ModelRecognizer)
        ]

    result = ColumnScanResult(column=str(column))
    try:
        result.model_version = registry.content_model_version()
    except Exception:
        result.model_version = None
    result.config_version = getattr(registry, "config_version", None)

    if not recognizers:
        result.warnings.append("No hay entidades elegibles para escanear contenido")
        return result

    recognizer_count = len(recognizers)
    entity_cells = np.zeros(recognizer_count, dtype=np.int64)
    winner_cells = np.zeros(recognizer_count, dtype=np.int64)
    entity_context = np.zeros(recognizer_count, dtype=bool)
    entity_validator = np.zeros(recognizer_count, dtype=bool)

    seen_values: set[str] = set()
    seen_pii_values: set[str] = set()

    remaining = (
        int(opts.max_rows)
        if opts.max_rows is not None and int(opts.max_rows) > 0
        else None
    )
    free_text_ready = False
    is_free_text = False
    is_numeric_dtype = False
    model_unavailable = False

    chunk_iter = iter(chunks)
    for chunk in chunk_iter:
        if chunk is None:
            continue
        raw_values, numeric = _extract_chunk(chunk)
        if not raw_values:
            continue

        if remaining is not None and len(raw_values) > remaining:
            raw_values = raw_values[:remaining]
            result.truncated = True

        result.total_rows += len(raw_values)
        normalized = _normalize_values(raw_values)
        if not normalized:
            if remaining is not None:
                remaining -= len(raw_values)
                if remaining <= 0:
                    break
            continue

        if not free_text_ready:
            is_numeric_dtype = bool(numeric)
            is_free_text, _ = detect_free_text(normalized[:2000])
            free_text_ready = True

        unique_values, counts = _unique_counts(normalized)
        unique_count = len(unique_values)
        seen_values.update(unique_values)
        result.non_empty_values += int(counts.sum())

        detected = np.zeros((unique_count, recognizer_count), dtype=bool)
        priority = np.zeros((unique_count, recognizer_count), dtype=float)

        for index, recognizer in enumerate(recognizers):
            context_matched = False
            scores: Optional[np.ndarray] = None

            if isinstance(recognizer, RegexRecognizer):
                mask, context_matched = _regex_detection(
                    recognizer, unique_values, column, is_free_text
                )
                if mask is not None:
                    scores = mask.astype(float)
            elif isinstance(recognizer, DictionaryRecognizer):
                mask, context_matched = _dictionary_detection(
                    recognizer, unique_values, column
                )
                if mask is not None:
                    scores = mask.astype(float)
            elif isinstance(recognizer, ModelRecognizer):
                mask, scores, context_matched, unavailable = _model_detection(
                    recognizer, unique_values, column, is_numeric_dtype
                )
                model_unavailable = model_unavailable or unavailable
            else:
                mask = None

            if mask is None or scores is None:
                continue
            if len(mask) != unique_count:
                continue
            if not mask.any():
                continue

            detected[:, index] = mask
            priority[:, index] = _priority_values(
                recognizer,
                context_matched,
                mask,
                scores,
                index,
                recognizer_count,
            )
            entity_context[index] = entity_context[index] or context_matched
            if isinstance(recognizer, RegexRecognizer) and recognizer.has_strong_validator:
                entity_validator[index] = True

        if recognizer_count:
            has_any = detected.any(axis=1)
            if has_any.any():
                winner = np.where(has_any, priority.argmax(axis=1), -1)
                entity_cells += (detected * counts[:, None]).sum(axis=0).astype(
                    np.int64
                )
                winner_counts = np.bincount(
                    winner[has_any],
                    weights=counts[has_any],
                    minlength=recognizer_count,
                )
                winner_cells += winner_counts.astype(np.int64)
                result.pii_values += int(counts[has_any].sum())
                seen_pii_values.update(
                    value for value, flag in zip(unique_values, has_any) if flag
                )

        if remaining is not None:
            remaining -= len(raw_values)
            if remaining <= 0:
                try:
                    next_chunk = next(chunk_iter)
                except StopIteration:
                    next_chunk = None
                if next_chunk is not None:
                    next_values, _ = _extract_chunk(next_chunk)
                    if next_values:
                        result.truncated = True
                break

    result.rows_scanned = result.total_rows
    result.empty_values = max(0, result.total_rows - result.non_empty_values)
    result.unique_values = len(seen_values)
    result.unique_pii_values = len(seen_pii_values)
    result.non_pii_values = max(0, result.non_empty_values - result.pii_values)
    result.pii_rate = (
        result.pii_values / result.non_empty_values if result.non_empty_values else 0.0
    )
    result.non_pii_rate = (
        result.non_pii_values / result.non_empty_values
        if result.non_empty_values
        else 0.0
    )

    if result.total_rows == 0:
        result.warnings.append("La columna no contiene filas para analizar.")
    elif result.non_empty_values == 0:
        result.warnings.append(
            "La columna no contiene valores no vacíos para analizar."
        )

    findings: List[EntityFinding] = []
    for index, recognizer in enumerate(recognizers):
        cells = int(entity_cells[index])
        if cells <= 0:
            continue
        findings.append(
            EntityFinding(
                entity=str(recognizer.entity),
                group=str(recognizer.group),
                classification=int(recognizer.classification),
                cells=cells,
                distribution_rate=(
                    float(winner_cells[index]) / result.pii_values
                    if result.pii_values
                    else 0.0
                ),
                coverage_rate=(
                    cells / result.non_empty_values if result.non_empty_values else 0.0
                ),
                used_validator=bool(entity_validator[index]),
                context_matched=bool(entity_context[index]),
            )
        )

    findings.sort(key=lambda item: (-item.cells, item.entity))
    result.entities = findings

    if model_unavailable:
        result.warnings.append(
            "El modelo de contenido no está disponible; no se evaluaron nombre, "
            "apellido, dirección ni empresa."
        )
    if result.truncated:
        result.warnings.append(
            "Se alcanzó el tope de filas del escaneo profundo; los porcentajes "
            "corresponden a las filas escaneadas."
        )

    return result
