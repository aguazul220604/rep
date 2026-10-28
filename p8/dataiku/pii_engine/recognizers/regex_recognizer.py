# -*- coding: utf-8 -*-
"""
`RegexRecognizer`: evaluación vectorizada con Polars + validador en Python + contexto.

Cumple la Sección 7.4 de la documentación:
  1. Evalúa la expresión regular una sola vez sobre la muestra con Polars (`str.contains`).
     - En columnas estructuradas usa el patrón tal cual (anclado `^...$`).
     - En columnas de texto libre remueve anclas `^` y `$` para búsqueda embebida (`contains`).
  2. Ejecuta el validador (`Validator`) únicamente sobre los valores que hicieron match.
  3. Aplica `ContextEnhancer` según el nombre de la columna.
"""
import re
from typing import Callable, List, Optional

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    pl = None  # type: ignore
    HAS_POLARS = False

from pii_engine.core.schema import ColumnSample, EntityScore
from pii_engine.recognizers.base import Recognizer
from pii_engine.recognizers.context import ContextEnhancer
from pii_engine.recognizers.validators import STRONG_CHECKSUM_VALIDATORS, get_validator


def _strip_anchors(pattern: str) -> str:
    """Convierte un patrón anclado `^...$` en uno apto para búsqueda `contains` en texto libre."""
    p = pattern.strip()
    if p.startswith("^"):
        p = p[1:]
    if p.endswith("$") and not p.endswith(r"\$"):
        p = p[:-1]
    return p


class RegexRecognizer(Recognizer):
    """
    Recognizer basado en expresiones regulares (Grupos A y B) y validador opcional.
    """

    def __init__(
        self,
        entity: str,
        patterns: List[str],
        group: str = "A",
        classification: int = 1,
        validator_name: Optional[str] = None,
        min_match_rate: float = 0.50,
        context_words: Optional[List[str]] = None,
        negative_context_words: Optional[List[str]] = None,
        context_mode: str = "optional",
        allow_in_free_text: bool = True,
    ):
        super().__init__(
            entity=entity,
            group=group,
            classification=classification,
            min_match_rate=min_match_rate,
            context_words=context_words,
            negative_context_words=negative_context_words,
            context_mode=context_mode,
        )
        self.patterns = list(patterns or [])
        self.validator_name = validator_name
        self.validator: Optional[Callable[[str], bool]] = get_validator(validator_name)
        self.has_strong_validator: bool = bool(
            validator_name and validator_name in STRONG_CHECKSUM_VALIDATORS
        )
        self.allow_in_free_text = bool(allow_in_free_text)

        # Compilar patrón combinado una sola vez al cargar el registro (Sección 7.4)
        if self.patterns:
            combined_anchored = "|".join(f"(?:{p})" for p in self.patterns)
            combined_unanchored = "|".join(f"(?:{_strip_anchors(p)})" for p in self.patterns)
            self._anchored_pattern = f"(?i)(?:{combined_anchored})"
            self._unanchored_pattern = f"(?i)(?:{combined_unanchored})"
            self._py_anchored_re = re.compile(self._anchored_pattern)
            self._py_extract_re = re.compile(self._unanchored_pattern)
        else:
            self._anchored_pattern = ""
            self._unanchored_pattern = ""
            self._py_anchored_re = None
            self._py_extract_re = None

        self.context_enhancer = ContextEnhancer(
            positive_words=self.context_words,
            negative_words=self.negative_context_words,
        )

    def score(
        self,
        sample: ColumnSample,
        expected_by_name: bool = False,
    ) -> EntityScore:
        n = sample.sample_size
        ctx_matched, _ = self.context_enhancer.evaluate(
            sample.column, expected_by_name=expected_by_name
        )

        # Si la entidad exige contexto obligatorio (o es de solo nombre) y no hay contexto, no aplica
        if self.requires_context and not (expected_by_name or ctx_matched):
            return EntityScore(
                entity=self.entity,
                group=self.group,
                classification=self.classification,
                match_rate=0.0,
                raw_match_rate=0.0,
                score=0.0,
                passed_threshold=False,
                used_validator=self.validator is not None,
                validator_passed_rate=0.0,
                context_matched=ctx_matched,
                context_boost=0.0,
                sample_size=n,
            )

        if n == 0 or not self._anchored_pattern:
            return EntityScore(
                entity=self.entity,
                group=self.group,
                classification=self.classification,
                match_rate=0.0,
                raw_match_rate=0.0,
                score=0.0,
                passed_threshold=False,
                used_validator=self.validator is not None,
                validator_passed_rate=0.0,
                context_matched=ctx_matched,
                context_boost=0.0,
                sample_size=0,
            )

        use_free_text = sample.is_free_text and self.allow_in_free_text
        if sample.is_free_text and not self.allow_in_free_text and not expected_by_name:
            return EntityScore(
                entity=self.entity,
                group=self.group,
                classification=self.classification,
                match_rate=0.0,
                raw_match_rate=0.0,
                score=0.0,
                passed_threshold=False,
                used_validator=self.validator is not None,
                validator_passed_rate=0.0,
                context_matched=ctx_matched,
                context_boost=0.0,
                sample_size=n,
            )

        pattern = self._unanchored_pattern if use_free_text else self._anchored_pattern
        if HAS_POLARS:
            s = pl.Series("v", sample.values, dtype=pl.Utf8)
            mask = s.str.contains(pattern)
            matched_values = s.filter(mask).to_list()
        else:
            comp_re = self._py_extract_re if use_free_text else self._py_anchored_re
            matched_values = [v for v in sample.values if comp_re is not None and comp_re.search(v)]

        n_regex_matched = len(matched_values)
        raw_match_rate = n_regex_matched / n

        if n_regex_matched == 0:
            return EntityScore(
                entity=self.entity,
                group=self.group,
                classification=self.classification,
                match_rate=0.0,
                raw_match_rate=0.0,
                score=0.0,
                passed_threshold=False,
                used_validator=self.validator is not None,
                validator_passed_rate=0.0,
                context_matched=ctx_matched,
                context_boost=0.0,
                sample_size=n,
            )

        # Ejecutar validador SOLO sobre los valores que hicieron match en la regex
        if self.validator is not None:
            valid_count = 0
            for val in matched_values:
                candidate = val
                if use_free_text and self._py_extract_re is not None:
                    m = self._py_extract_re.search(val)
                    if m:
                        candidate = m.group(0)
                if self.validator(candidate):
                    valid_count += 1
            match_rate = valid_count / n
            validator_passed_rate = valid_count / n_regex_matched
        else:
            match_rate = raw_match_rate
            validator_passed_rate = 1.0

        # En texto libre el umbral efectivo es menor porque el PII puede aparecer en una fracción de filas
        effective_threshold = (
            min(self.min_match_rate, 0.15) if use_free_text else self.min_match_rate
        )

        # Penalización si el contexto es recomendado ("recommended") pero no está en el nombre
        base_score = match_rate
        if self.context_mode == "recommended" and not (expected_by_name or ctx_matched):
            base_score *= 0.65

        adj_score, ctx_matched, ctx_delta = self.context_enhancer.boost(
            sample.column, base_score, expected_by_name=expected_by_name
        )

        passed = match_rate >= effective_threshold and (
            not self.requires_context or expected_by_name or ctx_matched
        )

        return EntityScore(
            entity=self.entity,
            group=self.group,
            classification=self.classification,
            match_rate=match_rate,
            raw_match_rate=raw_match_rate,
            score=adj_score,
            passed_threshold=passed,
            used_validator=self.has_strong_validator,
            validator_passed_rate=validator_passed_rate,
            context_matched=ctx_matched,
            context_boost=ctx_delta,
            sample_size=n,
        )
