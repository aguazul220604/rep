# -*- coding: utf-8 -*-
"""
`DictionaryRecognizer`: evaluación vectorizada contra catálogos cerrados (Grupo C).

Entidades cubiertas: `nacionalidad`, `sexo`, `estado_civil`, `puesto`.
"""
from pathlib import Path
import re
from typing import Iterable, List, Optional, Set

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    pl = None  # type: ignore
    HAS_POLARS = False

from pii_engine.core.schema import ColumnSample, EntityScore
from pii_engine.recognizers.base import Recognizer
from pii_engine.recognizers.context import ContextEnhancer, normalize_text_value


class DictionaryRecognizer(Recognizer):
    """
    Recognizer que evalúa pertenencia a un diccionario o catálogo cerrado de valores.
    """

    def __init__(
        self,
        entity: str,
        values: Optional[Iterable[str]] = None,
        catalog_path: Optional[Path] = None,
        group: str = "C",
        classification: int = 1,
        min_match_rate: float = 0.65,
        context_words: Optional[List[str]] = None,
        negative_context_words: Optional[List[str]] = None,
        context_mode: str = "recommended",
        allow_substring_match: bool = False,
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
        self.allow_substring_match = bool(allow_substring_match)
        raw_items: List[str] = list(values or [])
        if catalog_path is not None and catalog_path.exists():
            lines = catalog_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                clean = line.strip()
                if clean and not clean.startswith("#"):
                    raw_items.append(clean)

        self.catalog: Set[str] = {
            normalize_text_value(item)
            for item in raw_items
            if normalize_text_value(item)
        }
        self._catalog_list: List[str] = sorted(self.catalog)
        # Tokens de al menos 4 caracteres para match parcial en descripciones de puesto
        self._long_tokens: List[str] = [t for t in self._catalog_list if len(t) >= 4]
        if self.allow_substring_match and self._long_tokens:
            escaped = "|".join(re.escape(t) for t in self._long_tokens)
            self._substring_regex = rf"(?i)\b(?:{escaped})\b"
            self._py_substring_re = re.compile(self._substring_regex)
        else:
            self._substring_regex = ""
            self._py_substring_re = None

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

        if self.requires_context and not (expected_by_name or ctx_matched):
            return EntityScore(
                entity=self.entity,
                group=self.group,
                classification=self.classification,
                match_rate=0.0,
                raw_match_rate=0.0,
                score=0.0,
                passed_threshold=False,
                used_validator=False,
                validator_passed_rate=0.0,
                context_matched=ctx_matched,
                context_boost=0.0,
                sample_size=n,
            )

        if n == 0 or not self.catalog:
            return EntityScore(
                entity=self.entity,
                group=self.group,
                classification=self.classification,
                match_rate=0.0,
                raw_match_rate=0.0,
                score=0.0,
                passed_threshold=False,
                used_validator=False,
                validator_passed_rate=0.0,
                context_matched=ctx_matched,
                context_boost=0.0,
                sample_size=0,
            )

        # Normalizar muestra y evaluar pertenencia al conjunto
        norm_values = [normalize_text_value(v) for v in sample.values]
        if HAS_POLARS:
            s_norm = pl.Series("v", norm_values, dtype=pl.Utf8)
            exact_mask = s_norm.is_in(self._catalog_list)
            if self.allow_substring_match and self._substring_regex:
                sub_mask = s_norm.str.contains(self._substring_regex)
                mask = exact_mask | sub_mask
            else:
                mask = exact_mask
            matched_count = int(mask.sum())
        else:
            matched_count = 0
            for nv in norm_values:
                if nv in self.catalog:
                    matched_count += 1
                elif self._py_substring_re is not None and self._py_substring_re.search(nv):
                    matched_count += 1

        match_rate = matched_count / n

        base_score = match_rate
        # Diccionarios como 'H'/'M' (sexo) o gentilicios requieren contexto si no fueron predichos por nombre
        if self.context_mode == "recommended" and not (expected_by_name or ctx_matched):
            base_score *= 0.55

        adj_score, ctx_matched, ctx_delta = self.context_enhancer.boost(
            sample.column, base_score, expected_by_name=expected_by_name
        )

        passed = match_rate >= self.min_match_rate and (
            not self.requires_context or expected_by_name or ctx_matched
        )
        # Para diccionarios con valores de 1 sola letra (como sexo H/M/F), exigir contexto de nombre siempre
        if self.entity == "sexo" and not (expected_by_name or ctx_matched):
            mean_len = sum(len(v) for v in norm_values) / max(1, len(norm_values))
            if mean_len <= 2.0:
                passed = False
                adj_score *= 0.3

        return EntityScore(
            entity=self.entity,
            group=self.group,
            classification=self.classification,
            match_rate=match_rate,
            raw_match_rate=match_rate,
            score=adj_score,
            passed_threshold=passed,
            used_validator=False,
            validator_passed_rate=1.0 if matched_count > 0 else 0.0,
            context_matched=ctx_matched,
            context_boost=ctx_delta,
            sample_size=n,
        )
