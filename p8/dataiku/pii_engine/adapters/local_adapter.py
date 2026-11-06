# -*- coding: utf-8 -*-
"""
Adaptador local (sin Dataiku) y lectores de archivos del motor PII.

`LocalAdapter` se conserva como wrapper de compatibilidad sobre `PIIAdapter`.
La lógica de lectura vive en `pii_engine.adapters.sources`.
"""
from __future__ import annotations

from typing import Any, Optional

from pii_engine.adapters.adapter import PIIAdapter
from pii_engine.adapters.model_providers import LocalModelProvider
from pii_engine.adapters.sources import (
    CSV_SUFFIXES,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_READ_ROWS,
    DEFAULT_ROW_MAX_ROWS,
    DEFAULT_ROW_MIN_ROWS,
    DEFAULT_ROW_RATIO,
    EXCEL_SUFFIXES,
    PARQUET_SUFFIXES,
    SUPPORTED_FILE_SUFFIXES,
    DataikuSourceReader,
    LocalSourceReader,
    read_csv_sample,
    read_excel_sample,
    read_parquet_sample,
)


class LocalAdapter(PIIAdapter):
    """Fachada local entre un canal sin Dataiku y el motor PII."""

    def __init__(
        self,
        max_read_rows: Optional[int] = None,
        sampling_mode: str = "random",
        sampling_seed: int = 42,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        row_ratio: Optional[float] = DEFAULT_ROW_RATIO,
        row_min_rows: Optional[int] = DEFAULT_ROW_MIN_ROWS,
        row_max_rows: Optional[int] = DEFAULT_ROW_MAX_ROWS,
        analyzer: Optional[Any] = None,
        analyzer_factory: Optional[Any] = None,
        analyzer_getter: Optional[Any] = None,
    ):
        self._source_reader = LocalSourceReader(
            max_read_rows=max_read_rows,
            sampling_mode=sampling_mode,
            sampling_seed=sampling_seed,
            chunk_size=chunk_size,
            row_ratio=row_ratio,
            row_min_rows=row_min_rows,
            row_max_rows=row_max_rows,
        )
        super().__init__(
            model_provider=LocalModelProvider(),
            source_reader=self._source_reader,
            analyzer=analyzer,
            analyzer_factory=analyzer_factory,
            analyzer_getter=analyzer_getter,
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

    @property
    def sampling_mode(self) -> str:
        return self._source_reader.sampling_mode

    @property
    def sampling_seed(self) -> int:
        return self._source_reader.sampling_seed

    @property
    def chunk_size(self) -> int:
        return self._source_reader.chunk_size

    def build_analyzer(self, refresh: bool = False) -> Any:
        return super().build_analyzer(refresh=refresh)


__all__ = [
    "LocalAdapter",
    "LocalSourceReader",
    "DataikuSourceReader",
    "DEFAULT_MAX_READ_ROWS",
    "DEFAULT_CHUNK_SIZE",
    "CSV_SUFFIXES",
    "PARQUET_SUFFIXES",
    "EXCEL_SUFFIXES",
    "SUPPORTED_FILE_SUFFIXES",
    "read_csv_sample",
    "read_parquet_sample",
    "read_excel_sample",
]
