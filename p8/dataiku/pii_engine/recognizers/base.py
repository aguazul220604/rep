# -*- coding: utf-8 -*-
"""
Interfaz base del patrón Strategy para los Recognizers de contenido (Sección 7.1).
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from pii_engine.core.schema import ColumnSample, EntityScore


class Recognizer(ABC):
    """
    Interfaz común para evaluar si una muestra de valores corresponde a una entidad PII.
    """

    def __init__(
        self,
        entity: str,
        group: str = "A",
        classification: int = 1,
        min_match_rate: float = 0.50,
        context_words: Optional[List[str]] = None,
        negative_context_words: Optional[List[str]] = None,
        context_mode: str = "optional",  # "none" | "optional" | "recommended" | "required" | "name_only"
    ):
        self.entity = entity
        self.group = group
        self.classification = int(classification)
        self.min_match_rate = float(min_match_rate)
        self.context_words = list(context_words or [])
        self.negative_context_words = list(negative_context_words or [])
        self.context_mode = context_mode

    @property
    def is_name_only(self) -> bool:
        return self.context_mode == "name_only"

    @property
    def requires_context(self) -> bool:
        return self.context_mode in ("required", "name_only")

    @abstractmethod
    def score(
        self,
        sample: ColumnSample,
        expected_by_name: bool = False,
    ) -> EntityScore:
        """
        Evalúa la muestra de la columna y devuelve un EntityScore.
        """
        raise NotImplementedError
