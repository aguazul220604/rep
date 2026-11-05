# -*- coding: utf-8 -*-
"""
Adaptador local (sin Dataiku) y lectores de archivos del motor PII.

Expone la misma operación de alto nivel que necesita cualquier canal
(`analyze_dataframe`, `analyze_file`) y resuelve la lectura de CSV, parquet y
Excel con muestreo aleatorio de filas por reservoir. El núcleo (`pii_engine.core`)
no lee archivos; esta es la capa de I/O.

`LocalAdapter` no importa `dataiku` en ningún punto, por lo que puede usarse en
scripts, APIs y pruebas sin DSS. `DataikuAdapter` lo compone para los archivos y
solo añade el acceso a datasets del flow.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, BinaryIO, Iterator, List, Optional, Union

import pandas as pd

from pii_engine.core.analyzer import PIIAnalyzer
from pii_engine.core.row_sampling import (
    DEFAULT_ROW_SEED,
    DEFAULT_SAMPLING_MODE,
    normalize_sampling_mode,
    reservoir_sample,
    sample_dataframe,
)
from pii_engine.core.schema import ColumnResult

DEFAULT_MAX_READ_ROWS = 50_000
DEFAULT_CHUNK_SIZE = 50_000

CSV_SUFFIXES = {".csv"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
EXCEL_SUFFIXES = {".xls", ".xlsx", ".xlsm"}
SUPPORTED_FILE_SUFFIXES = CSV_SUFFIXES | PARQUET_SUFFIXES | EXCEL_SUFFIXES

_Source = Union[str, Path, BinaryIO, Any]


def _validate_max_rows(max_rows: int) -> int:
    value = int(max_rows)
    if value <= 0:
        raise ValueError("max_read_rows debe ser mayor que cero")
    return value


def read_csv_sample(
    source: _Source,
    max_rows: int,
    usecols: Optional[List[str]] = None,
    dtype: Optional[Any] = str,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    chunksize: int = DEFAULT_CHUNK_SIZE,
    **read_kwargs: Any,
) -> pd.DataFrame:
    """
    Lee un CSV conservando hasta `max_rows` filas muestreadas aleatoriamente.

    En `mode="head"` se leen solo las primeras `max_rows` filas (comportamiento
    legacy). En `mode="random"` se recorre el archivo por chunks y se aplica un
    reservoir uniforme, con memoria O(max_rows).
    """
    max_rows = _validate_max_rows(max_rows)
    mode = normalize_sampling_mode(mode)
    options = dict(usecols=usecols, dtype=dtype, **read_kwargs)

    if mode == "head":
        return pd.read_csv(source, nrows=max_rows, low_memory=False, **options)

    chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
    return reservoir_sample(chunks, max_rows, seed=seed)


def _iter_parquet_frames(
    source: _Source,
    columns: Optional[List[str]],
    batch_size: int,
) -> Iterator[pd.DataFrame]:
    """Itera un parquet por batches con pyarrow; fallback a lectura completa."""
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        yield pd.read_parquet(source, columns=columns)
        return

    try:
        parquet_file = pq.ParquetFile(source)
    except Exception:
        # Fuente no soportada por pyarrow (p. ej. streams exóticos): pandas.
        if hasattr(source, "seek"):
            try:
                source.seek(0)
            except Exception:
                pass
        yield pd.read_parquet(source, columns=columns)
        return

    for batch in parquet_file.iter_batches(
        batch_size=int(batch_size), columns=columns
    ):
        yield batch.to_pandas()


def read_parquet_sample(
    source: _Source,
    max_rows: int,
    columns: Optional[List[str]] = None,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    chunksize: int = DEFAULT_CHUNK_SIZE,
) -> pd.DataFrame:
    """Lee un parquet con muestreo aleatorio uniforme de filas (o head legacy)."""
    max_rows = _validate_max_rows(max_rows)
    mode = normalize_sampling_mode(mode)
    frames = _iter_parquet_frames(source, columns, int(chunksize))

    if mode == "random":
        return reservoir_sample(frames, max_rows, seed=seed)

    parts: List[pd.DataFrame] = []
    remaining = max_rows
    for frame in frames:
        if remaining <= 0:
            break
        take = frame.head(remaining)
        if len(take):
            parts.append(take)
            remaining -= len(take)
    if not parts:
        return pd.DataFrame(columns=columns)
    return pd.concat(parts, ignore_index=True)


def read_excel_sample(
    source: _Source,
    max_rows: int,
    sheet: Optional[Union[str, int]] = None,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
) -> pd.DataFrame:
    """
    Lee una hoja de Excel y aplica muestreo de filas.

    pandas no tiene lector Excel por chunks: en `mode="random"` se materializa
    la hoja y se muestrea (el tamaño del archivo está acotado en la webapp).
    """
    max_rows = _validate_max_rows(max_rows)
    mode = normalize_sampling_mode(mode)
    sheet_name: Any = 0 if sheet is None else sheet

    if mode == "head":
        return pd.read_excel(source, sheet_name=sheet_name, nrows=max_rows)

    df = pd.read_excel(source, sheet_name=sheet_name)
    return sample_dataframe(df, max_rows, mode="random", seed=seed)


class LocalAdapter:
    """
    Fachada local entre un canal sin Dataiku y el motor PII.

    `analyzer_getter` permite que `DataikuAdapter` inyecte el analizador que ya
    resolvió con sus bundles de modelos; en uso local se construye `PIIAnalyzer()`.
    """

    def __init__(
        self,
        max_read_rows: int = DEFAULT_MAX_READ_ROWS,
        sampling_mode: str = DEFAULT_SAMPLING_MODE,
        sampling_seed: int = DEFAULT_ROW_SEED,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        analyzer: Optional[Any] = None,
        analyzer_factory: Optional[Any] = None,
        analyzer_getter: Optional[Any] = None,
    ):
        self.max_read_rows = _validate_max_rows(max_read_rows)
        self.sampling_mode = normalize_sampling_mode(sampling_mode)
        self.sampling_seed = int(sampling_seed)
        self.chunk_size = max(1, int(chunk_size))
        self._analyzer = analyzer
        self._analyzer_factory = analyzer_factory or PIIAnalyzer
        self._analyzer_getter = analyzer_getter
        self._lock = threading.RLock()

    def build_analyzer(self) -> Any:
        with self._lock:
            if self._analyzer is None:
                if self._analyzer_getter is not None:
                    self._analyzer = self._analyzer_getter()
                else:
                    self._analyzer = self._analyzer_factory()
            return self._analyzer

    def reset(self) -> None:
        with self._lock:
            self._analyzer = None

    def analyze_dataframe(
        self,
        df: Any,
        table_name: str = "dataframe",
        columns: Optional[Any] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> List[ColumnResult]:
        if df is None:
            raise ValueError("El DataFrame de entrada es obligatorio")
        return self.build_analyzer().analyze(
            data=df,
            table_meta=str(table_name),
            columns=list(columns) if columns is not None else None,
            sample_size=sample_size,
            seed=seed,
        )

    def analyze_file(
        self,
        file_obj: _Source,
        filename: str,
        columns: Optional[Any] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        sheet: Optional[Union[str, int]] = None,
    ) -> List[ColumnResult]:
        """
        Analiza un CSV, parquet o Excel. `file_obj` puede ser una ruta o un
        stream (por ejemplo `request.files["file"].stream`).
        """
        name = str(filename or "")
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_FILE_SUFFIXES:
            raise ValueError("Formato no soportado: use CSV, parquet o Excel")

        cols = list(columns) if columns is not None else None
        close_stream = False
        if isinstance(file_obj, (str, Path)):
            stream: Any = open(file_obj, "rb")
            close_stream = True
        else:
            stream = file_obj

        try:
            if suffix in CSV_SUFFIXES:
                # `dtype=str` evita que pandas infiera identificadores numéricos
                # (CLABE, cuenta, NSS, etc.) y destruya sus ceros iniciales.
                df = read_csv_sample(
                    stream,
                    self.max_read_rows,
                    usecols=cols,
                    dtype=str,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                    chunksize=self.chunk_size,
                )
            elif suffix in PARQUET_SUFFIXES:
                df = read_parquet_sample(
                    stream,
                    self.max_read_rows,
                    columns=cols,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                    chunksize=self.chunk_size,
                )
            else:
                df = read_excel_sample(
                    stream,
                    self.max_read_rows,
                    sheet=sheet,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                )
        finally:
            if close_stream:
                stream.close()

        return self.analyze_dataframe(
            df,
            table_name=Path(name).name or "archivo",
            columns=cols,
            sample_size=sample_size,
            seed=seed,
        )

    def health(self) -> dict:
        """Estado del motor local sin exponer valores de datos."""
        info: dict = {
            "status": "ok",
            "engine_ready": False,
            "max_read_rows": self.max_read_rows,
            "sampling_mode": self.sampling_mode,
            "sampling_seed": self.sampling_seed,
        }
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
