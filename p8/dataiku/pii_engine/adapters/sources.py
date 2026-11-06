# -*- coding: utf-8 -*-
"""Lectores de fuentes de datos para los adaptadores PII."""
from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO, Iterator, List, Optional, Union

import pandas as pd

from pii_engine.adapters.contracts import NotSupportedError
from pii_engine.core.row_sampling import (
    DEFAULT_ROW_SEED,
    DEFAULT_SAMPLING_MODE,
    count_chunks,
    normalize_sampling_mode,
    resolve_row_sample_size,
    reservoir_sample,
    sample_dataframe,
)

DEFAULT_MAX_READ_ROWS = 50_000
DEFAULT_CHUNK_SIZE = 50_000

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"


def _load_sampling_defaults() -> dict:
    try:
        import yaml

        payload = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return payload.get("sampling", {}) or {}
    except Exception:
        return {}


_SAMPLING_DEFAULTS = _load_sampling_defaults()
DEFAULT_ROW_RATIO = float(_SAMPLING_DEFAULTS.get("row_ratio") or 0.02)
DEFAULT_ROW_MIN_ROWS = _SAMPLING_DEFAULTS.get("row_min_rows")
DEFAULT_ROW_MAX_ROWS = _SAMPLING_DEFAULTS.get("row_max_rows")

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


def _rewind(source: _Source) -> bool:
    if isinstance(source, (str, Path)):
        return True
    seek = getattr(source, "seek", None)
    if not callable(seek):
        return False
    try:
        seek(0)
        return True
    except Exception:
        return False


def _count_csv_rows(
    source: _Source,
    chunksize: int,
    options: dict,
) -> int:
    chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
    return count_chunks(chunks)


def read_csv_sample(
    source: _Source,
    max_rows: Optional[int] = None,
    usecols: Optional[List[str]] = None,
    dtype: Optional[Any] = str,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    chunksize: int = DEFAULT_CHUNK_SIZE,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
    **read_kwargs: Any,
) -> pd.DataFrame:
    """
    Lee un CSV conservando una muestra de filas.

    Con `ratio`, cuenta las filas en una primera pasada y muestrea exactamente
    `ceil(total * ratio)` en la segunda. Sin `ratio`, `max_rows` es el tamaño
    fijo legacy. `max_rows` actúa como tope opcional cuando hay `ratio`.
    """
    mode = normalize_sampling_mode(mode)
    options = dict(usecols=usecols, dtype=dtype, **read_kwargs)

    if ratio is None:
        if max_rows is None:
            max_rows = DEFAULT_MAX_READ_ROWS
        max_rows = _validate_max_rows(max_rows)
        if mode == "head":
            return pd.read_csv(source, nrows=max_rows, low_memory=False, **options)
        chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
        return reservoir_sample(chunks, max_rows, seed=seed)

    total_rows = _count_csv_rows(source, chunksize, options)
    target_rows = resolve_row_sample_size(
        total_rows, ratio, min_rows=min_rows, max_rows=max_rows
    )
    if target_rows <= 0:
        return pd.DataFrame(columns=usecols)
    if not _rewind(source):
        raise ValueError(
            "La fuente CSV no permite volver a leerla para aplicar el muestreo por ratio"
        )
    if mode == "head":
        return pd.read_csv(
            source, nrows=target_rows, low_memory=False, **options
        )
    chunks = pd.read_csv(source, chunksize=int(chunksize), **options)
    return reservoir_sample(chunks, target_rows, seed=seed)


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


def _parquet_num_rows(source: _Source) -> Optional[int]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return None
    try:
        parquet_file = pq.ParquetFile(source)
    except Exception:
        return None
    metadata = getattr(parquet_file, "metadata", None)
    num_rows = getattr(metadata, "num_rows", None)
    if num_rows is None:
        return None
    return int(num_rows)


def read_parquet_sample(
    source: _Source,
    max_rows: Optional[int] = None,
    columns: Optional[List[str]] = None,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    chunksize: int = DEFAULT_CHUNK_SIZE,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
) -> pd.DataFrame:
    """Lee un parquet con muestreo aleatorio uniforme de filas (o head legacy)."""
    mode = normalize_sampling_mode(mode)

    if ratio is None:
        if max_rows is None:
            max_rows = DEFAULT_MAX_READ_ROWS
        max_rows = _validate_max_rows(max_rows)
        target_rows = max_rows
    else:
        total_rows = _parquet_num_rows(source)
        if total_rows is None:
            frames = _iter_parquet_frames(source, columns, int(chunksize))
            total_rows = count_chunks(frames)
            if not _rewind(source):
                raise ValueError(
                    "La fuente parquet no permite volver a leerla para aplicar el muestreo por ratio"
                )
        target_rows = resolve_row_sample_size(
            total_rows, ratio, min_rows=min_rows, max_rows=max_rows
        )
        if target_rows <= 0:
            return pd.DataFrame(columns=columns)
        if not _rewind(source):
            raise ValueError(
                "La fuente parquet no permite volver a leerla para aplicar el muestreo por ratio"
            )

    frames = _iter_parquet_frames(source, columns, int(chunksize))
    if mode == "random":
        return reservoir_sample(frames, target_rows, seed=seed)

    parts: List[pd.DataFrame] = []
    remaining = target_rows
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
    max_rows: Optional[int] = None,
    sheet: Optional[Union[str, int]] = None,
    seed: int = DEFAULT_ROW_SEED,
    mode: str = DEFAULT_SAMPLING_MODE,
    ratio: Optional[float] = None,
    min_rows: Optional[int] = None,
) -> pd.DataFrame:
    """
    Lee una hoja de Excel y aplica muestreo de filas.

    pandas no tiene lector Excel por chunks: la hoja se materializa y se
    muestrea con `sample_dataframe`.
    """
    mode = normalize_sampling_mode(mode)
    sheet_name: Any = 0 if sheet is None else sheet

    if ratio is None:
        if max_rows is None:
            max_rows = DEFAULT_MAX_READ_ROWS
        max_rows = _validate_max_rows(max_rows)
        if mode == "head":
            return pd.read_excel(source, sheet_name=sheet_name, nrows=max_rows)
        df = pd.read_excel(source, sheet_name=sheet_name)
        return sample_dataframe(df, max_rows, mode="random", seed=seed)

    df = pd.read_excel(source, sheet_name=sheet_name)
    return sample_dataframe(
        df,
        max_rows=max_rows,
        mode=mode,
        seed=seed,
        ratio=ratio,
        min_rows=min_rows,
    )


class LocalSourceReader:
    """Lector local de DataFrames y archivos CSV/parquet/Excel."""

    def __init__(
        self,
        max_read_rows: Optional[int] = None,
        sampling_mode: str = DEFAULT_SAMPLING_MODE,
        sampling_seed: int = DEFAULT_ROW_SEED,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        row_ratio: Optional[float] = DEFAULT_ROW_RATIO,
        row_min_rows: Optional[int] = DEFAULT_ROW_MIN_ROWS,
        row_max_rows: Optional[int] = DEFAULT_ROW_MAX_ROWS,
    ):
        self.sampling_mode = normalize_sampling_mode(sampling_mode)
        self.sampling_seed = int(sampling_seed)
        self.chunk_size = max(1, int(chunk_size))
        self.row_ratio = None if row_ratio is None else float(row_ratio)
        self.row_min_rows = None if row_min_rows is None else int(row_min_rows)
        if row_max_rows is None and max_read_rows is not None:
            row_max_rows = max_read_rows
        self.row_max_rows = None if row_max_rows is None else int(row_max_rows)
        if self.row_ratio is None and self.row_max_rows is None:
            self.row_max_rows = DEFAULT_MAX_READ_ROWS
        self.max_read_rows = self.row_max_rows

    def load_dataframe(
        self,
        df: Any,
        columns: Optional[List[str]] = None,
    ) -> Any:
        if df is None:
            raise ValueError("El DataFrame de entrada es obligatorio")
        return sample_dataframe(
            df,
            max_rows=self.row_max_rows,
            mode=self.sampling_mode,
            seed=self.sampling_seed,
            ratio=self.row_ratio,
            min_rows=self.row_min_rows,
        )

    def load_file(
        self,
        file_obj: _Source,
        filename: str,
        columns: Optional[List[str]] = None,
        sheet: Optional[Union[str, int]] = None,
    ) -> pd.DataFrame:
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
                return read_csv_sample(
                    stream,
                    self.row_max_rows,
                    usecols=cols,
                    dtype=str,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                    chunksize=self.chunk_size,
                    ratio=self.row_ratio,
                    min_rows=self.row_min_rows,
                )
            if suffix in PARQUET_SUFFIXES:
                return read_parquet_sample(
                    stream,
                    self.row_max_rows,
                    columns=cols,
                    seed=self.sampling_seed,
                    mode=self.sampling_mode,
                    chunksize=self.chunk_size,
                    ratio=self.row_ratio,
                    min_rows=self.row_min_rows,
                )
            return read_excel_sample(
                stream,
                self.row_max_rows,
                sheet=sheet,
                seed=self.sampling_seed,
                mode=self.sampling_mode,
                ratio=self.row_ratio,
                min_rows=self.row_min_rows,
            )
        finally:
            if close_stream:
                stream.close()

    def load_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
    ) -> Any:
        raise NotSupportedError(
            "LocalSourceReader no lee datasets de Dataiku; use DataikuSourceReader"
        )

    def health_info(self) -> dict:
        return {
            "max_read_rows": self.max_read_rows,
            "sampling_mode": self.sampling_mode,
            "sampling_seed": self.sampling_seed,
            "row_ratio": self.row_ratio,
            "row_min_rows": self.row_min_rows,
            "row_max_rows": self.row_max_rows,
        }

    def reset(self) -> None:
        pass


class DataikuSourceReader(LocalSourceReader):
    """Lector de datasets de Dataiku DSS, además de archivos y DataFrames."""

    @staticmethod
    def _import_dataiku():
        try:
            import dataiku  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "El paquete dataiku no está disponible; ejecute el adaptador dentro de DSS"
            ) from exc
        return dataiku

    @staticmethod
    def _extract_record_count(payload: Any) -> Optional[int]:
        if not isinstance(payload, dict):
            return None
        for key in (
            "recordCount",
            "record_count",
            "recordsCount",
            "nbRecords",
            "rows",
        ):
            value = payload.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        return None

    def _iter_dataset_chunks(self, dataset: Any, cols: Optional[List[str]]) -> Any:
        iterator = getattr(dataset, "iter_dataframes", None)
        if not callable(iterator):
            return None
        try:
            return iterator(columns=cols, chunksize=self.chunk_size)
        except TypeError:
            try:
                return iterator()
            except TypeError:
                return None

    def _get_dataset_row_count(
        self, dataset: Any, cols: Optional[List[str]]
    ) -> Optional[int]:
        for method_name in ("get_info", "get_metadata"):
            method = getattr(dataset, method_name, None)
            if not callable(method):
                continue
            try:
                count = self._extract_record_count(method())
            except Exception:
                count = None
            if count is not None:
                return count

        get_metrics = getattr(dataset, "get_last_metric_values", None)
        if callable(get_metrics):
            try:
                collector = get_metrics()
                metric = collector.get_metric_by_id("records:COUNT_RECORDS")
                if isinstance(metric, dict):
                    value = metric.get("value")
                else:
                    value = getattr(metric, "value", None)
                if value is None and hasattr(metric, "get_value"):
                    value = metric.get_value()
                if value is not None:
                    return int(value)
            except Exception:
                pass

        chunks = self._iter_dataset_chunks(dataset, cols)
        if chunks is not None:
            try:
                return count_chunks(chunks)
            except Exception:
                pass

        try:
            df = dataset.get_dataframe(columns=cols)
        except TypeError:
            df = dataset.get_dataframe()
            if cols:
                df = df[[c for c in cols if c in df.columns]]
        return int(len(df))

    def _target_rows(self, dataset: Any, cols: Optional[List[str]]) -> int:
        if self.row_ratio is None:
            if self.row_max_rows is None:
                return DEFAULT_MAX_READ_ROWS
            return self.row_max_rows
        total_rows = self._get_dataset_row_count(dataset, cols)
        if total_rows is None:
            raise ValueError(
                "No se pudo determinar el total de filas del dataset para aplicar el ratio"
            )
        return resolve_row_sample_size(
            total_rows,
            self.row_ratio,
            min_rows=self.row_min_rows,
            max_rows=self.row_max_rows,
        )

    def _read_dataset_sample(self, dataset: Any, cols: Optional[List[str]]) -> Any:
        """Muestrea filas del dataset sin depender del muestreo nativo de DSS."""
        target_rows = self._target_rows(dataset, cols)
        if target_rows <= 0:
            return pd.DataFrame(columns=cols)

        if self.sampling_mode == "head":
            try:
                return dataset.get_dataframe(
                    columns=cols,
                    limit=target_rows,
                )
            except TypeError:
                df = dataset.get_dataframe()
                if cols:
                    df = df[[c for c in cols if c in df.columns]]
                return df.head(target_rows)

        chunks = self._iter_dataset_chunks(dataset, cols)
        if chunks is not None:
            return reservoir_sample(
                chunks,
                target_rows,
                seed=self.sampling_seed,
            )

        try:
            df = dataset.get_dataframe(columns=cols)
        except TypeError:
            df = dataset.get_dataframe()
            if cols:
                df = df[[c for c in cols if c in df.columns]]
        return sample_dataframe(
            df,
            max_rows=target_rows,
            mode="random",
            seed=self.sampling_seed,
        )

    def load_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
    ) -> Any:
        if not dataset_name or not str(dataset_name).strip():
            raise ValueError("El nombre del dataset es obligatorio")
        dataiku = self._import_dataiku()
        dataset = dataiku.Dataset(str(dataset_name))
        cols = list(columns) if columns is not None else None
        return self._read_dataset_sample(dataset, cols)
