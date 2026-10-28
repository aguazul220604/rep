# -*- coding: utf-8 -*-
"""
`RecognizerRegistry`: carga e inicializa los 40 recognizers desde `entities.yaml`.

Soporta las estrategias definidas en la arquitectura:
  - `regex`      -> `RegexRecognizer` (Grupos A y B)
  - `dictionary` -> `DictionaryRecognizer` (Grupo C)
  - `model`      -> `ModelRecognizer` (Grupo D)
  - `none`       -> `HumanReviewRecognizer` (Grupo E / `otro_pii`)
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from pii_engine.core.schema import ColumnSample, EntityScore
from pii_engine.recognizers.base import Recognizer
from pii_engine.recognizers.dictionary_recognizer import DictionaryRecognizer
from pii_engine.recognizers.model_recognizer import ModelRecognizer
from pii_engine.recognizers.regex_recognizer import RegexRecognizer

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class HumanReviewRecognizer(Recognizer):
    """
    Recognizer pasivo para Clasificación 2 (`otro_pii`), donde no existe estructura
    única de contenido y siempre requiere revisión humana (`needs_review = true`).
    """

    def __init__(self, entity: str = "otro_pii", group: str = "E", classification: int = 2):
        super().__init__(
            entity=entity,
            group=group,
            classification=classification,
            min_match_rate=1.0,
            context_mode="name_only",
        )

    def score(
        self,
        sample: ColumnSample,
        expected_by_name: bool = False,
    ) -> EntityScore:
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
            context_matched=expected_by_name,
            context_boost=0.0,
            sample_size=sample.sample_size,
        )


class RecognizerRegistry:
    """
    Registro central de Recognizers cargado desde configuración YAML.
    """

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        self.recognizers: Dict[str, Recognizer] = {}
        self.config_version: str = "1.0"
        self.base_dir: Path = DEFAULT_CONFIG_DIR
        if config_path is not None:
            self.load(config_path)
        else:
            default_entities = DEFAULT_CONFIG_DIR / "entities.yaml"
            if default_entities.exists():
                self.load(default_entities)

    def load(self, config_source: Union[str, Path, Dict[str, Any]]) -> "RecognizerRegistry":
        if isinstance(config_source, (str, Path)):
            path = Path(config_source)
            self.base_dir = path.parent
            raw_cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif isinstance(config_source, dict):
            raw_cfg = config_source
        else:
            raise TypeError(f"Configuración no soportada: {type(config_source)!r}")

        self.config_version = str(raw_cfg.get("version", "1.0"))
        entities_cfg: Dict[str, Dict[str, Any]] = raw_cfg.get("entities", {})
        self.recognizers.clear()

        for entity_name, spec in entities_cfg.items():
            strategy = str(spec.get("strategy", "regex")).lower()
            group = str(spec.get("group", "A"))
            classification = int(spec.get("classification", 1))
            min_match_rate = float(spec.get("min_match_rate", 0.50))
            context_words = spec.get("context_words", [])
            neg_words = spec.get("negative_context_words", [])
            context_mode = str(spec.get("context_mode", "optional"))

            if strategy == "regex":
                rec: Recognizer = RegexRecognizer(
                    entity=entity_name,
                    patterns=spec.get("patterns", []),
                    group=group,
                    classification=classification,
                    validator_name=spec.get("validator"),
                    min_match_rate=min_match_rate,
                    context_words=context_words,
                    negative_context_words=neg_words,
                    context_mode=context_mode,
                    allow_in_free_text=bool(spec.get("allow_in_free_text", True)),
                )
            elif strategy == "dictionary":
                cat_rel = spec.get("catalog")
                cat_path = (self.base_dir / cat_rel) if cat_rel else None
                rec = DictionaryRecognizer(
                    entity=entity_name,
                    values=spec.get("values", []),
                    catalog_path=cat_path,
                    group=group,
                    classification=classification,
                    min_match_rate=min_match_rate,
                    context_words=context_words,
                    negative_context_words=neg_words,
                    context_mode=context_mode,
                    allow_substring_match=bool(spec.get("allow_substring_match", False)),
                )
            elif strategy == "model":
                model_rel = spec.get("model")
                model_path: Optional[Union[str, Path]] = None
                if model_rel:
                    cand = self.base_dir.parent / model_rel
                    model_path = cand if cand.exists() else model_rel
                rec = ModelRecognizer(
                    entity=entity_name,
                    model_path=model_path,
                    group=group,
                    classification=classification,
                    min_match_rate=min_match_rate,
                    value_prob_threshold=float(spec.get("value_prob_threshold", 0.50)),
                    context_words=context_words,
                    negative_context_words=neg_words,
                    context_mode=context_mode,
                )
            elif strategy == "none":
                rec = HumanReviewRecognizer(
                    entity=entity_name,
                    group=group,
                    classification=classification,
                )
            else:
                raise ValueError(f"Estrategia desconocida {strategy!r} en entidad {entity_name!r}")

            self.recognizers[entity_name] = rec

        return self

    def get(self, entity: str) -> Optional[Recognizer]:
        # Normalizar alias histórico 'estado civil' -> 'estado_civil'
        if entity == "estado civil":
            entity = "estado_civil"
        return self.recognizers.get(entity)

    def all_entities(self) -> List[str]:
        return list(self.recognizers.keys())

    def set_content_model_path(self, model_path: Union[str, Path]) -> None:
        """
        Sobrescribe la ruta del bundle de contenido para todas las entidades D.
        Útil cuando el adaptador de Dataiku resuelve el Managed Folder en runtime.
        """
        self.content_model_path = str(model_path)
        for rec in self.recognizers.values():
            if isinstance(rec, ModelRecognizer):
                rec.model_path = self.content_model_path

    def content_model_version(self) -> Optional[str]:
        """Devuelve la versión declarada por el bundle de contenido, si está disponible."""
        for rec in self.recognizers.values():
            if isinstance(rec, ModelRecognizer):
                version = rec.model_version
                if version:
                    return version
        return None

    def get_scannable_recognizers(self, exclude_entity: Optional[str] = None) -> List[Recognizer]:
        """
        Devuelve los Recognizers candidatos para un escaneo de contenido (Sección 5.3 nodo K).
        Excluye:
          - Clasificación 2 (`otro_pii`)
          - Entidades marcadas como `name_only` (`cvv`, `pin_nip`, `saldo`, `credito`, `biometrico`)
        Ordena primero Grupo A (señal fuerte), luego Grupo C, D y B.
        """
        group_order = {"A": 0, "C": 1, "D": 2, "B": 3, "E": 4}
        candidates = [
            rec
            for name, rec in self.recognizers.items()
            if name != exclude_entity
            and rec.classification == 1
            and not rec.is_name_only
        ]
        candidates.sort(key=lambda r: group_order.get(r.group, 9))
        return candidates
