# -*- coding: utf-8 -*-
"""
Refuerzo y validación de contexto por nombre de columna (`ContextEnhancer`).

Normaliza el nombre de la columna igual que `pii_utils.normalize_name` y evalúa
palabras de contexto positivas y negativas por entidad.
"""
import re
import unicodedata
from typing import Iterable, List, Tuple

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_column_name(name: str) -> str:
    """Normaliza un nombre de columna ('Num_Cliente' / 'numCliente' -> 'num cliente')."""
    if name is None:
        return ""
    s = str(name)
    s = _CAMEL.sub(r"\1 \2", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NON_ALNUM.sub(" ", s)
    return s.strip()


def normalize_text_value(val: str) -> str:
    """Normaliza un valor de texto para comparaciones en diccionarios (sin acentos, minúsculas)."""
    if val is None:
        return ""
    s = unicodedata.normalize("NFKD", str(val))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


class ContextEnhancer:
    """
    Evalúa si el nombre de la columna contiene pistas léxicas que apoyan o contradicen
    una entidad y calcula el ajuste de score correspondiente.
    """

    def __init__(
        self,
        positive_words: Iterable[str] = (),
        negative_words: Iterable[str] = (),
        boost_amount: float = 0.12,
        penalty_amount: float = 0.25,
    ):
        self.positive_words: List[str] = [normalize_column_name(w) for w in positive_words if w]
        self.negative_words: List[str] = [normalize_column_name(w) for w in negative_words if w]
        self.boost_amount = float(boost_amount)
        self.penalty_amount = float(penalty_amount)

    @staticmethod
    def _matches_any(norm_col: str, candidates: List[str]) -> bool:
        if not norm_col or not candidates:
            return False
        tokens = set(norm_col.split())
        padded = f" {norm_col} "
        for cand in candidates:
            if not cand:
                continue
            if " " in cand:
                if f" {cand} " in padded or cand in norm_col:
                    return True
            else:
                if cand in tokens:
                    return True
                # Subcadena en token compuesto de al menos 4 letras (p.ej. 'nomcte' -> 'nom', 'clabe')
                if len(cand) >= 4 and cand in norm_col:
                    return True
        return False

    def evaluate(self, column_name: str, expected_by_name: bool = False) -> Tuple[bool, float]:
        """
        Devuelve `(context_matched, delta_score)` para el nombre de columna dado.
        """
        norm_col = normalize_column_name(column_name)
        has_neg = self._matches_any(norm_col, self.negative_words)
        if has_neg and not expected_by_name:
            return False, -self.penalty_amount

        has_pos = expected_by_name or self._matches_any(norm_col, self.positive_words)
        if has_pos:
            return True, self.boost_amount
        return False, 0.0

    def boost(self, column_name: str, base_score: float, expected_by_name: bool = False) -> Tuple[float, bool, float]:
        """
        Aplica el ajuste de contexto al `base_score` y devuelve `(adjusted_score, matched, delta)`.
        """
        if base_score <= 0.0:
            matched, _ = self.evaluate(column_name, expected_by_name=expected_by_name)
            return 0.0, matched, 0.0
        matched, delta = self.evaluate(column_name, expected_by_name=expected_by_name)
        adjusted = max(0.0, min(1.0, base_score + delta))
        return adjusted, matched, delta
