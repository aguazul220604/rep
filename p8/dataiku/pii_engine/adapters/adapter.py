# -*- coding: utf-8 -*-
"""Fachada única de adaptadores del motor PII."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pii_engine.adapters.contracts import (
    ModelProvider,
    ModelArtifacts,
    NotSupportedError,
    SourceReader,
)
from pii_engine.adapters.model_providers import LocalModelProvider
from pii_engine.adapters.sources import LocalSourceReader
from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.schema import ENGINE_VERSION, ColumnResult


class PIIAdapter:
    """
    Orquesta muestreo, lectura de fuentes y ciclo de vida del analizador.

    `model_provider` resuelve los artefactos del modelo y `source_reader` la
    entrada de datos. Las implementaciones concretas de Dataiku se inyectan
    desde los wrappers de compatibilidad, sin importar `dataiku` aquí.
    """

    def __init__(
        self,
        model_provider: Optional[ModelProvider] = None,
        source_reader: Optional[SourceReader] = None,
        analyzer: Optional[Any] = None,
        analyzer_factory: Optional[Any] = None,
        analyzer_getter: Optional[Any] = None,
    ):
        self.model_provider = model_provider or LocalModelProvider()
        self.source_reader = source_reader or LocalSourceReader()
        self._analyzer = analyzer
        self._analyzer_factory = analyzer_factory or PIIAnalyzer
        self._analyzer_getter = analyzer_getter
        self._lock = threading.RLock()

    def build_analyzer(self, refresh: bool = False) -> Any:
        with self._lock:
            if self._analyzer is not None and not refresh:
                return self._analyzer

            if self._analyzer_getter is not None:
                analyzer = self._analyzer_getter()
            else:
                artifacts = self.model_provider.resolve_artifacts()
                analyzer = self._analyzer_factory(
                    binary_model_bundle=artifacts.binary_bundle,
                    entity_model_bundle=artifacts.entity_bundle,
                    content_model_path=artifacts.content_model_path,
                )
            self._analyzer = analyzer
            return analyzer

    def reset(self) -> None:
        with self._lock:
            self._analyzer = None
            self.model_provider.reset()
            self.source_reader.reset()

    def resolve_model_path(self, folder_name: str) -> Any:
        resolver = getattr(self.model_provider, "resolve_model_path", None)
        if not callable(resolver):
            raise NotSupportedError(
                "El proveedor de modelos no resuelve rutas de managed folders"
            )
        return resolver(folder_name)

    def _analyze_frame(
        self,
        frame: Any,
        table_name: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        return self.build_analyzer().analyze(
            data=frame,
            table_meta=str(table_name),
            columns=list(columns) if columns is not None else None,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_dataframe(
        self,
        df: Any,
        table_name: str = "dataframe",
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        apply_row_sampling: bool = True,
    ) -> List[ColumnResult]:
        if df is None:
            raise ValueError("El DataFrame de entrada es obligatorio")
        cols = list(columns) if columns is not None else None
        if apply_row_sampling:
            frame = self.source_reader.load_dataframe(df, columns=cols)
        else:
            frame = df
        return self._analyze_frame(
            frame,
            table_name=table_name,
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_file(
        self,
        file_obj: Any,
        filename: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        sheet: Optional[Any] = None,
    ) -> List[ColumnResult]:
        cols = list(columns) if columns is not None else None
        frame = self.source_reader.load_file(
            file_obj,
            filename,
            columns=cols,
            sheet=sheet,
        )
        return self._analyze_frame(
            frame,
            table_name=Path(str(filename)).name or "archivo",
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_dataset(
        self,
        dataset_name: str,
        columns: Optional[Iterable[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        cols = list(columns) if columns is not None else None
        frame = self.source_reader.load_dataset(dataset_name, columns=cols)
        return self._analyze_frame(
            frame,
            table_name=str(dataset_name),
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def health(self) -> Dict[str, Any]:
        source_info = self.source_reader.health_info()
        info: Dict[str, Any] = {
            "status": "ok",
            "engine_version": ENGINE_VERSION,
            "engine_ready": False,
        }
        info.update(source_info)
        info["sampling"] = {
            "mode": source_info.get("sampling_mode"),
            "seed": source_info.get("sampling_seed"),
            "max_read_rows": source_info.get("max_read_rows"),
            "row_ratio": source_info.get("row_ratio"),
            "row_min_rows": source_info.get("row_min_rows"),
            "row_max_rows": source_info.get("row_max_rows"),
        }

        folders_info = getattr(self.model_provider, "folders_info", None)
        if callable(folders_info):
            info["folders"] = folders_info()

        try:
            analyzer = self.build_analyzer()
            info["engine_ready"] = True
            version_getter = getattr(analyzer, "_model_version", None)
            info["model_version"] = (
                version_getter() if callable(version_getter) else "unknown"
            )
        except Exception as exc:
            info["status"] = "degraded"
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info
