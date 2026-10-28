# -*- coding: utf-8 -*-
"""
`ModelRecognizer`: interfaz de carga e inferencia para el modelo de contenido (Grupo D).

Entidades cubiertas (Sección 8.2):
  - `nombre`
  - `apellido`
  - `direccion`
  - `empresa`

El bundle `model_content_v1/model.joblib` contiene `labels`, `temperature`,
`tau`, `theta`, `other_label` y `model_version`. La inferencia se hace **una sola
vez por columna** y se reutiliza entre todas las entidades D; la decisión de
columna se toma con la misma agregación calibrada del entrenamiento:
cada valor vota por su clase si `p >= tau` y se acepta la clase con cobertura
`>= theta`; si ninguna llega, gana `otro`.

Soporta cargar `model.joblib` desde una ruta local o un Dataiku Managed Folder,
ya sea como estimador sklearn directo (`predict_proba`) o como bundle diccionario
(`{"model": ..., "classes": ..., "tau": ..., "theta": ...}`).
Si el artefacto aún no está disponible, se abstiene de forma segura sin interrumpir el motor.
"""
from collections import OrderedDict
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from pii_engine.core.schema import ColumnSample, EntityScore
from pii_engine.recognizers.base import Recognizer
from pii_engine.recognizers.context import ContextEnhancer

# Valores que no aportan señal y diluirían el match_rate (mismo conjunto que
# model_content/model.py). sampling.py solo quita nulos/vacíos, no estos tokens.
NULL_TOKENS = {
    "", "null", "none", "nan", "n/a", "na", "nd", "s/d", "sin dato", "sin datos",
    "-", "--", ".", "..", "0", "undefined",
}

# El entrenamiento descarta valores > 120 caracteres; en inferencia se truncan
# para respetar la misma distribución.
MAX_VALUE_LEN = 120

# Probabilidades y coberturas por defecto si el artefacto no trae metadatos.
DEFAULT_TAU = 0.50
DEFAULT_THETA = 0.30
DEFAULT_OTHER_LABEL = "otro"


def _clean_values(values: List[str]) -> List[str]:
    out = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in NULL_TOKENS:
            continue
        if len(s) > MAX_VALUE_LEN:
            s = s[:MAX_VALUE_LEN]
        out.append(s)
    return out


def _apply_temperature(proba: np.ndarray, temperature: float) -> np.ndarray:
    """Escalado por temperatura: softmax(log p / T). T=1 no cambia nada."""
    proba = np.asarray(proba, dtype=float)
    t = float(temperature) if temperature else 1.0
    if t <= 0:
        t = 1.0
    logp = np.log(np.clip(proba, 1e-9, 1.0)) / t
    logp -= logp.max(axis=1, keepdims=True)
    q = np.exp(logp)
    return q / q.sum(axis=1, keepdims=True)


class ContentModelLoader:
    """
    Cargador con caché en memoria para compartir un mismo `model.joblib` entre
    las 4 entidades del Grupo D si usan un modelo multiclase único.
    """
    _cache: Dict[str, Any] = {}

    @classmethod
    def load(cls, model_path: Optional[Union[str, Path]]) -> Optional[Any]:
        if not model_path:
            return None
        key = str(model_path)
        if key in cls._cache:
            return cls._cache[key]

        p = Path(model_path)
        if p.is_dir():
            p = p / "model.joblib"
        if not p.exists():
            return None

        import joblib
        artifact = joblib.load(p)
        cls._cache[key] = artifact
        return artifact

    @classmethod
    def register_in_memory(cls, key: str, artifact: Any) -> None:
        """Permite inyectar un modelo ya cargado (p. ej. desde Dataiku folder o tests)."""
        cls._cache[key] = artifact
        ContentModelScorer.clear_cache()

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()
        ContentModelScorer.clear_cache()


class ContentModelScorer:
    """
    Inferencia y agregación compartida para el modelo de contenido.

    Cachea el resultado por `(ruta del modelo, hash de los valores limpiados)` para
    que las 4 entidades D de una misma columna no repitan `predict_proba`.
    """

    _cache: "OrderedDict[Tuple[str, int, int], Dict[str, Any]]" = OrderedDict()
    _cache_max = 64
    _lock = threading.Lock()

    @classmethod
    def clear_cache(cls) -> None:
        with cls._lock:
            cls._cache.clear()

    @classmethod
    def _cache_get(cls, key: Tuple[str, int, int]) -> Optional[Dict[str, Any]]:
        with cls._lock:
            if key in cls._cache:
                cls._cache.move_to_end(key)
                return cls._cache[key]
        return None

    @classmethod
    def _cache_put(cls, key: Tuple[str, int, int], value: Dict[str, Any]) -> None:
        with cls._lock:
            cls._cache[key] = value
            while len(cls._cache) > cls._cache_max:
                cls._cache.popitem(last=False)

    @classmethod
    def score(
        cls,
        model_path: Optional[Union[str, Path]],
        values: List[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Devuelve las probabilidades calibradas y la agregación por columna, o
        `None` si el modelo no está disponible o la entrada queda vacía.
        """
        cleaned = _clean_values(values)
        if not cleaned or not model_path:
            return None

        key = (str(model_path), hash(tuple(cleaned)), len(cleaned))
        cached = cls._cache_get(key)
        if cached is not None:
            return cached

        artifact = ContentModelLoader.load(model_path)
        if artifact is None:
            return None

        if isinstance(artifact, dict):
            model = artifact.get("model")
            labels = artifact.get("classes")
            if labels is None:
                labels = artifact.get("labels")
            if labels is None and hasattr(model, "classes_"):
                labels = list(model.classes_)
            temperature = float(artifact.get("temperature", 1.0))
            tau = float(artifact.get("tau", DEFAULT_TAU))
            theta = float(artifact.get("theta", DEFAULT_THETA))
            other_label = str(artifact.get("other_label", DEFAULT_OTHER_LABEL))
            has_metadata = True
        else:
            model = artifact
            labels = list(getattr(model, "classes_", []))
            temperature = 1.0
            tau = DEFAULT_TAU
            theta = DEFAULT_THETA
            other_label = DEFAULT_OTHER_LABEL
            has_metadata = False

        if model is None or not hasattr(model, "predict_proba"):
            return None

        labels = [str(label) for label in labels]
        raw = np.asarray(model.predict_proba(cleaned), dtype=float)
        if raw.ndim == 1 or raw.shape[0] != len(cleaned):
            return None
        if not labels or raw.shape[1] != len(labels):
            labels = [str(c) for c in getattr(model, "classes_", [])]
        if not labels or raw.shape[1] != len(labels):
            return None

        proba = _apply_temperature(raw, temperature)
        top_idx = proba.argmax(axis=1)
        conf = proba.max(axis=1)

        coverage: Dict[str, float] = {}
        for j, label in enumerate(labels):
            if label == other_label:
                continue
            coverage[label] = float(np.mean((top_idx == j) & (conf >= tau)))

        if coverage:
            winner = max(coverage, key=coverage.get)
            if coverage[winner] < theta:
                winner = other_label
        else:
            winner = other_label

        result = {
            "labels": labels,
            "proba": proba,
            "coverage": coverage,
            "winner": winner,
            "other_label": other_label,
            "tau": tau,
            "theta": theta,
            "has_metadata": has_metadata,
            "mean_probs": {
                label: float(np.mean(proba[:, j]))
                for j, label in enumerate(labels)
            },
        }
        cls._cache_put(key, result)
        return result


class ModelRecognizer(Recognizer):
    """
    Recognizer que puntúa valores de una muestra mediante un modelo de contenido (`model.joblib`).
    """

    def __init__(
        self,
        entity: str,
        model_path: Optional[Union[str, Path]] = None,
        group: str = "D",
        classification: int = 1,
        min_match_rate: float = 0.50,
        value_prob_threshold: float = 0.50,
        context_words: Optional[List[str]] = None,
        negative_context_words: Optional[List[str]] = None,
        context_mode: str = "recommended",
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
        self.model_path = str(model_path) if model_path else None
        self.value_prob_threshold = float(value_prob_threshold)
        self.context_enhancer = ContextEnhancer(
            positive_words=self.context_words,
            negative_words=self.negative_context_words,
        )

    @property
    def model_version(self) -> Optional[str]:
        """Versión declarada por el bundle (`model_version`), si existe."""
        artifact = ContentModelLoader.load(self.model_path)
        if isinstance(artifact, dict):
            version = artifact.get("model_version")
            if version:
                return str(version)
        return None

    def _empty_score(
        self,
        sample_size: int,
        context_matched: bool,
        raw_match_rate: float = 0.0,
    ) -> EntityScore:
        return EntityScore(
            entity=self.entity,
            group=self.group,
            classification=self.classification,
            match_rate=0.0,
            raw_match_rate=raw_match_rate,
            score=0.0,
            passed_threshold=False,
            used_validator=False,
            validator_passed_rate=0.0,
            context_matched=context_matched,
            context_boost=0.0,
            sample_size=sample_size,
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

        # Rechazar si la columna es numérica pura (Grupo D son entidades textuales)
        if sample.is_numeric_dtype or n == 0:
            return self._empty_score(n, ctx_matched)

        if self.requires_context and not (expected_by_name or ctx_matched):
            return self._empty_score(n, ctx_matched)

        aggregated = ContentModelScorer.score(self.model_path, sample.values)
        if aggregated is None:
            return self._empty_score(n, ctx_matched)

        coverage = aggregated["coverage"].get(self.entity)
        if coverage is None:
            # La entidad no es una clase del bundle (p. ej. `otro` o un label ausente).
            return self._empty_score(n, ctx_matched)

        mean_prob = float(aggregated["mean_probs"].get(self.entity, 0.0))
        is_winner = aggregated["winner"] == self.entity

        base_score = 0.65 * coverage + 0.35 * mean_prob
        if self.context_mode == "recommended" and not (expected_by_name or ctx_matched):
            base_score *= 0.65

        adj_score, ctx_matched, ctx_delta = self.context_enhancer.boost(
            sample.column, base_score, expected_by_name=expected_by_name
        )

        if aggregated["has_metadata"]:
            required_coverage = float(aggregated["theta"])
        else:
            required_coverage = self.min_match_rate

        passed = (
            is_winner
            and coverage >= required_coverage
            and (not self.requires_context or expected_by_name or ctx_matched)
        )

        return EntityScore(
            entity=self.entity,
            group=self.group,
            classification=self.classification,
            match_rate=coverage,
            raw_match_rate=mean_prob,
            score=adj_score,
            passed_threshold=passed,
            used_validator=False,
            validator_passed_rate=coverage,
            context_matched=ctx_matched,
            context_boost=ctx_delta,
            sample_size=n,
        )
