# -*- coding: utf-8 -*-
"""
Adaptador de despliegue para Dataiku DSS.

`DataikuAdapter` se conserva como wrapper de compatibilidad sobre `PIIAdapter`.
La resolución de managed folders vive en `DataikuManagedFolderProvider` y la
lectura de datasets en `DataikuSourceReader`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from pii_engine.adapters.adapter import PIIAdapter
from pii_engine.adapters.contracts import ModelArtifactError
from pii_engine.adapters.model_providers import (
    DEFAULT_BINARY_FOLDER,
    DEFAULT_CONTENT_FOLDER,
    DEFAULT_ENTITY_FOLDER,
    DEFAULT_MODEL_FILENAME,
    DataikuManagedFolderProvider,
)
from pii_engine.adapters.sources import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_ROW_MAX_ROWS,
    DEFAULT_ROW_MIN_ROWS,
    DEFAULT_ROW_RATIO,
    DataikuSourceReader,
)


class DataikuAdapter(PIIAdapter):
    """Fachada entre Dataiku y el motor PII."""

    def __init__(
        self,
        binary_folder: str = DEFAULT_BINARY_FOLDER,
        entity_folder: str = DEFAULT_ENTITY_FOLDER,
        content_folder: str = DEFAULT_CONTENT_FOLDER,
        model_filename: str = DEFAULT_MODEL_FILENAME,
        max_read_rows: Optional[int] = None,
        sampling_mode: str = "random",
        sampling_seed: int = 42,
        row_ratio: Optional[float] = DEFAULT_ROW_RATIO,
        row_min_rows: Optional[int] = DEFAULT_ROW_MIN_ROWS,
        row_max_rows: Optional[int] = DEFAULT_ROW_MAX_ROWS,
        cache_dir: Optional[Union[str, Path]] = None,
        analyzer_factory: Optional[Any] = None,
        strict_models: bool = True,
    ):
        self.binary_folder = str(binary_folder)
        self.entity_folder = str(entity_folder)
        self.content_folder = str(content_folder)
        self.model_filename = str(model_filename)
        self.strict_models = bool(strict_models)

        self._model_provider = DataikuManagedFolderProvider(
            binary_folder=binary_folder,
            entity_folder=entity_folder,
            content_folder=content_folder,
            model_filename=model_filename,
            cache_dir=cache_dir,
            strict_models=strict_models,
        )
        self._source_reader = DataikuSourceReader(
            max_read_rows=max_read_rows,
            sampling_mode=sampling_mode,
            sampling_seed=sampling_seed,
            chunk_size=DEFAULT_CHUNK_SIZE,
            row_ratio=row_ratio,
            row_min_rows=row_min_rows,
            row_max_rows=row_max_rows,
        )
        super().__init__(
            model_provider=self._model_provider,
            source_reader=self._source_reader,
            analyzer_factory=analyzer_factory,
        )

    @property
    def max_read_rows(self) -> Optional[int]:
        return self._source_reader.max_read_rows

    @property
    def row_ratio(self) -> Optional[float]:
        return self._source_reader.row_ratio

    @property
    def row_min_rows(self) -> Optional[int]:
        return self._source_reader.row_min_rows

    @property
    def row_max_rows(self) -> Optional[int]:
        return self._source_reader.row_max_rows

    def _read_dataset_sample(self, dataset: Any, cols: Optional[list]) -> Any:
        return self._source_reader._read_dataset_sample(dataset, cols)


__all__ = [
    "DataikuAdapter",
    "ModelArtifactError",
    "DEFAULT_BINARY_FOLDER",
    "DEFAULT_ENTITY_FOLDER",
    "DEFAULT_CONTENT_FOLDER",
    "DEFAULT_MODEL_FILENAME",
]
