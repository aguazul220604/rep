# -*- coding: utf-8 -*-
"""Contratos compartidos por los adaptadores del motor PII."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Union


class ModelArtifactError(RuntimeError):
    """El bundle de un modelo no está disponible o no se pudo descargar."""


class NotSupportedError(RuntimeError):
    """La fuente o el proveedor no soporta la operación solicitada."""


@dataclass(frozen=True)
class ModelArtifacts:
    binary_bundle: Optional[Union[str, Any]] = None
    entity_bundle: Optional[Union[str, Any]] = None
    content_model_path: Optional[Union[str, Any]] = None


@dataclass
class SamplingConfig:
    row_ratio: Optional[float] = None
    row_min_rows: Optional[int] = None
    row_max_rows: Optional[int] = 50_000
    column_sample_size: Optional[int] = 2_000
    seed: int = 42
    mode: str = "random"
    chunk_size: int = 50_000


class ModelProvider(Protocol):
    def resolve_artifacts(self) -> ModelArtifacts:
        ...

    def resolve_model_path(self, folder_name: str) -> Any:
        ...

    def reset(self) -> None:
        ...


class SourceReader(Protocol):
    def load_dataframe(
        self,
        df: Any,
        columns: Optional[List[str]] = None,
    ) -> Any:
        ...

    def load_file(
        self,
        file_obj: Any,
        filename: str,
        columns: Optional[List[str]] = None,
        sheet: Optional[Union[str, int]] = None,
    ) -> Any:
        ...

    def load_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
    ) -> Any:
        ...

    def health_info(self) -> Dict[str, Any]:
        ...

    def reset(self) -> None:
        ...
