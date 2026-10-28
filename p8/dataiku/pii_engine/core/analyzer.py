# -*- coding: utf-8 -*-
"""
Orquestador principal `PIIAnalyzer` y función de alto nivel `analyze()`
(Secciones 4 y 5 de la documentación).

Flujo por columna (Sección 5.3):
  1. Muestreo de valores únicos no nulos en Polars (`sample_table`).
  2. Predicción por nombre de columna (`NameModelPredictor`: binario + entidad).
  3. Si el nombre sugiere una entidad PII (Clasificación 1) y hay contenido:
     - Evalúa el `Recognizer` de la entidad esperada.
     - Si `passed_threshold` es True -> pasa directo a Fusión.
     - Si no supera el umbral -> ejecuta escaneo contra las entidades elegibles.
  4. Si el nombre NO sugiere PII y hay contenido:
     - Ejecuta escaneo contra las entidades elegibles (Grupo A y entidades sin contexto obligatorio).
  5. Fusión de evidencia y calibración (`EvidenceFuser`) -> `ColumnResult`.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from pii_engine.core.fusion import EvidenceFuser
from pii_engine.core.name_models import NameModelPredictor
from pii_engine.core.sampling import DEFAULT_SAMPLE_SIZE, DEFAULT_SEED, sample_table
from pii_engine.core.schema import (
    ENGINE_VERSION,
    OTRO_PII,
    ColumnResult,
    ColumnSample,
    EntityScore,
    TableMeta,
)
from pii_engine.recognizers.registry import DEFAULT_CONFIG_DIR, RecognizerRegistry


class PIIAnalyzer:
    """
    Motor agnóstico de detección e identificación de datos PII.
    """

    def __init__(
        self,
        entities_config: Optional[Union[str, Path, Dict[str, Any]]] = None,
        thresholds_config: Optional[Union[str, Path, Dict[str, Any]]] = None,
        binary_model_bundle: Optional[Union[str, Path, Dict[str, Any]]] = None,
        entity_model_bundle: Optional[Union[str, Path, Dict[str, Any]]] = None,
        content_model_path: Optional[Union[str, Path]] = None,
    ):
        self.registry = RecognizerRegistry(entities_config)
        if content_model_path is not None:
            self.registry.set_content_model_path(content_model_path)

        if thresholds_config is None:
            t_path = DEFAULT_CONFIG_DIR / "thresholds.yaml"
            self.thresholds_cfg = (
                yaml.safe_load(t_path.read_text(encoding="utf-8"))
                if t_path.exists()
                else {}
            )
        elif isinstance(thresholds_config, (str, Path)):
            self.thresholds_cfg = yaml.safe_load(
                Path(thresholds_config).read_text(encoding="utf-8")
            )
        elif isinstance(thresholds_config, dict):
            self.thresholds_cfg = thresholds_config
        else:
            raise TypeError(f"thresholds_config no soportado: {type(thresholds_config)!r}")

        sampling_cfg = self.thresholds_cfg.get("sampling", {})
        name_cfg = self.thresholds_cfg.get("name_models", {})
        name_resolution_cfg = self.thresholds_cfg.get("name_resolution", {})
        fusion_cfg = self.thresholds_cfg.get("fusion", {})

        self.default_sample_size = int(
            sampling_cfg.get("default_sample_size", DEFAULT_SAMPLE_SIZE)
        )
        self.default_seed = int(sampling_cfg.get("seed", DEFAULT_SEED))

        self.name_predictor = NameModelPredictor(
            binary_bundle=binary_model_bundle,
            entity_bundle=entity_model_bundle,
            binary_threshold=float(name_cfg.get("binary_threshold", 0.409)),
            entity_min_confidence=float(name_cfg.get("entity_min_confidence", 0.45)),
            name_resolution_cfg=name_resolution_cfg,
        )
        self.fuser = EvidenceFuser(fusion_cfg=fusion_cfg, name_cfg=name_cfg)

    def _scan_eligible_entities(
        self,
        sample: ColumnSample,
        exclude_entity: Optional[str] = None,
    ) -> Optional[EntityScore]:
        """
        Escanea la muestra contra todas las entidades elegibles por contenido
        y devuelve el mejor `EntityScore` que supere su umbral (o el de mayor score).
        Prioriza Grupo A con validador fuerte en caso de empate.
        """
        if sample.sample_size == 0:
            return None

        best_passed: Optional[EntityScore] = None
        best_any: Optional[EntityScore] = None

        for rec in self.registry.get_scannable_recognizers(exclude_entity=exclude_entity):
            es = rec.score(sample, expected_by_name=False)
            if best_any is None or es.score > best_any.score:
                best_any = es

            if es.passed_threshold:
                if best_passed is None:
                    best_passed = es
                else:
                    # Prioridad: 1) Validador fuerte del Grupo A, 2) Mayor score
                    curr_key = (1 if es.used_validator else 0, es.score, es.match_rate)
                    prev_key = (
                        1 if best_passed.used_validator else 0,
                        best_passed.score,
                        best_passed.match_rate,
                    )
                    if curr_key > prev_key:
                        best_passed = es

        return best_passed if best_passed is not None else best_any

    def analyze(
        self,
        data: Optional[Any] = None,
        table_meta: Optional[Union[TableMeta, str, Dict[str, Any]]] = None,
        columns: Optional[List[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        """
        Analiza una tabla (o lista de nombres de columna si `data` es None) y devuelve
        un `ColumnResult` por cada columna.
        """
        if isinstance(table_meta, TableMeta):
            t_name = table_meta.table
        elif isinstance(table_meta, str):
            t_name = table_meta
        elif isinstance(table_meta, dict):
            t_name = str(table_meta.get("table", "unknown_table"))
        else:
            t_name = "unknown_table"

        s_size = sample_size if sample_size is not None else self.default_sample_size
        s_seed = seed if seed is not None else self.default_seed

        samples = sample_table(
            df=data,
            columns=columns,
            sample_size=s_size,
            seed=s_seed,
        )
        col_names = list(samples.keys())
        if not col_names:
            return []

        name_preds = self.name_predictor.predict(col_names)
        model_version = self._model_version()
        results: List[ColumnResult] = []

        for col in col_names:
            sample = samples[col]
            n_pred = name_preds[col]
            expected_score: Optional[EntityScore] = None
            scanned_score: Optional[EntityScore] = None

            if n_pred.is_pii_name:
                exp_ent = n_pred.expected_entity
                if exp_ent and exp_ent != OTRO_PII and sample.sample_size > 0:
                    rec = self.registry.get(exp_ent)
                    if rec is not None:
                        expected_score = rec.score(sample, expected_by_name=True)

                # Si no superó el umbral de la entidad esperada (o era otro_pii), escanear otras entidades
                if (
                    sample.sample_size > 0
                    and (expected_score is None or not expected_score.passed_threshold)
                ):
                    scanned_score = self._scan_eligible_entities(
                        sample, exclude_entity=exp_ent
                    )
            else:
                if sample.sample_size > 0:
                    scanned_score = self._scan_eligible_entities(sample)

            col_result = self.fuser.fuse(
                table_name=t_name,
                sample=sample,
                name_pred=n_pred,
                expected_content_score=expected_score,
                scanned_content_score=scanned_score,
                engine_version=ENGINE_VERSION,
                model_version=model_version,
                config_version=self.registry.config_version,
            )
            results.append(col_result)

        return results

    def _model_version(self) -> str:
        """Compone la versión de todos los modelos usados por el motor."""
        parts = [self.name_predictor.model_version_tag]
        content_version = self.registry.content_model_version()
        if content_version:
            parts.append(f"content:{content_version}")
        return ",".join(parts)


_DEFAULT_ANALYZER: Optional[PIIAnalyzer] = None


def analyze(
    data: Optional[Any] = None,
    table_meta: Optional[Union[TableMeta, str, Dict[str, Any]]] = None,
    columns: Optional[List[str]] = None,
    sample_size: Optional[int] = None,
    seed: Optional[int] = None,
    analyzer: Optional[PIIAnalyzer] = None,
) -> List[ColumnResult]:
    """
    Punto de entrada funcional del motor PII (usable desde webapp, API, Spark y plugin).
    """
    global _DEFAULT_ANALYZER
    engine = analyzer
    if engine is None:
        if _DEFAULT_ANALYZER is None:
            _DEFAULT_ANALYZER = PIIAnalyzer()
        engine = _DEFAULT_ANALYZER

    return engine.analyze(
        data=data,
        table_meta=table_meta,
        columns=columns,
        sample_size=sample_size,
        seed=seed,
    )
