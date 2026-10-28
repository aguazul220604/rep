# -*- coding: utf-8 -*-
"""
Muestreo por columna sobre Polars DataFrame / LazyFrame o pandas DataFrame.

Criterios (Sección 6 de la documentación):
  - Extrae valores no nulos y distintos por columna.
  - Tamaño fijo configurable (por defecto 2,000) con semilla reproducible.
  - Detecta si la columna corresponde a texto libre (longitud media, proporción
    de espacios y número medio de tokens) para aplicar `contains` vs `fullmatch`.
"""
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd

try:
    import polars as pl
    HAS_POLARS = True
except ImportError:
    pl = None  # type: ignore
    HAS_POLARS = False

from pii_engine.core.schema import ColumnSample

DEFAULT_SAMPLE_SIZE = 2000
DEFAULT_SEED = 42


def detect_free_text(
    values: List[str],
    mean_length_threshold: float = 48.0,
    mean_tokens_threshold: float = 6.5,
) -> Tuple[bool, float]:
    """
    Determina si una muestra de strings corresponde a una columna de texto libre
    (observaciones, comentarios, descripciones abiertas).
    """
    if not values:
        return False, 0.0

    if HAS_POLARS:
        s = pl.Series("v", values, dtype=pl.Utf8)
        lengths = s.str.len_chars()
        mean_len = float(lengths.mean() or 0.0)
        if mean_len < 20.0:
            return False, mean_len
        space_counts = s.str.count_matches(r"\s+")
        mean_tokens = float((space_counts + 1).mean() or 1.0)
        long_share = float((lengths > 60).mean() or 0.0)
    else:
        s_pd = pd.Series(values, dtype="string")
        lengths = s_pd.str.len()
        mean_len = float(lengths.mean() or 0.0)
        if mean_len < 20.0:
            return False, mean_len
        space_counts = s_pd.str.count(r"\s+")
        mean_tokens = float((space_counts + 1).mean() or 1.0)
        long_share = float((lengths > 60).mean() or 0.0)

    is_free = (
        (mean_len >= mean_length_threshold and mean_tokens >= mean_tokens_threshold)
        or (long_share >= 0.35 and mean_tokens >= 5.0)
    )
    return is_free, mean_len


def _sample_polars_series(
    series: "pl.Series",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
    free_text_len_threshold: float = 48.0,
    free_text_tokens_threshold: float = 6.5,
) -> ColumnSample:
    numeric_types = (
        pl.Int8, pl.Int16, pl.Int32, pl.Int64,
        pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        pl.Float32, pl.Float64, pl.Decimal,
    )
    col_name = series.name
    dtype_str = str(series.dtype)
    is_numeric = isinstance(series.dtype, numeric_types) or series.dtype in numeric_types

    non_null = series.drop_nulls()
    if len(non_null) == 0:
        return ColumnSample(
            column=col_name,
            dtype=dtype_str,
            values=[],
            sample_size=0,
            total_non_null=0,
            is_free_text=False,
            is_numeric_dtype=is_numeric,
            mean_length=0.0,
        )

    as_str = non_null.cast(pl.Utf8, strict=False).drop_nulls().str.strip_chars()
    as_str = as_str.filter(as_str != "")
    total_non_null = len(as_str)

    if total_non_null == 0:
        return ColumnSample(
            column=col_name,
            dtype=dtype_str,
            values=[],
            sample_size=0,
            total_non_null=0,
            is_free_text=False,
            is_numeric_dtype=is_numeric,
            mean_length=0.0,
        )

    unique_vals = as_str.unique(maintain_order=True)
    if len(unique_vals) > sample_size:
        sampled = unique_vals.sample(n=sample_size, with_replacement=False, seed=seed)
    else:
        sampled = unique_vals

    values = sampled.to_list()
    is_free_text, mean_len = detect_free_text(
        values,
        mean_length_threshold=free_text_len_threshold,
        mean_tokens_threshold=free_text_tokens_threshold,
    )

    return ColumnSample(
        column=col_name,
        dtype=dtype_str,
        values=values,
        sample_size=len(values),
        total_non_null=total_non_null,
        is_free_text=is_free_text,
        is_numeric_dtype=is_numeric,
        mean_length=mean_len,
    )


def _sample_pandas_series(
    series: pd.Series,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
    free_text_len_threshold: float = 48.0,
    free_text_tokens_threshold: float = 6.5,
) -> ColumnSample:
    col_name = str(series.name or "")
    dtype_str = str(series.dtype)
    is_numeric = bool(pd.api.types.is_numeric_dtype(series.dtype)) and not bool(
        pd.api.types.is_bool_dtype(series.dtype)
    )

    non_null = series.dropna()
    if len(non_null) == 0:
        return ColumnSample(
            column=col_name,
            dtype=dtype_str,
            values=[],
            sample_size=0,
            total_non_null=0,
            is_free_text=False,
            is_numeric_dtype=is_numeric,
            mean_length=0.0,
        )

    as_str = non_null.astype(str).str.strip()
    as_str = as_str[as_str != ""]
    total_non_null = len(as_str)

    if total_non_null == 0:
        return ColumnSample(
            column=col_name,
            dtype=dtype_str,
            values=[],
            sample_size=0,
            total_non_null=0,
            is_free_text=False,
            is_numeric_dtype=is_numeric,
            mean_length=0.0,
        )

    unique_vals = pd.Series(pd.unique(as_str))
    if len(unique_vals) > sample_size:
        sampled = unique_vals.sample(n=sample_size, replace=False, random_state=seed)
    else:
        sampled = unique_vals

    values = [str(v) for v in sampled.tolist()]
    is_free_text, mean_len = detect_free_text(
        values,
        mean_length_threshold=free_text_len_threshold,
        mean_tokens_threshold=free_text_tokens_threshold,
    )

    return ColumnSample(
        column=col_name,
        dtype=dtype_str,
        values=values,
        sample_size=len(values),
        total_non_null=total_non_null,
        is_free_text=is_free_text,
        is_numeric_dtype=is_numeric,
        mean_length=mean_len,
    )


def sample_column(
    series: Any,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
    free_text_len_threshold: float = 48.0,
    free_text_tokens_threshold: float = 6.5,
) -> ColumnSample:
    """
    Extrae una muestra de valores únicos no nulos de una columna (Polars o pandas).
    """
    if HAS_POLARS and isinstance(series, pl.Series):
        return _sample_polars_series(
            series,
            sample_size=sample_size,
            seed=seed,
            free_text_len_threshold=free_text_len_threshold,
            free_text_tokens_threshold=free_text_tokens_threshold,
        )
    if isinstance(series, pd.Series):
        return _sample_pandas_series(
            series,
            sample_size=sample_size,
            seed=seed,
            free_text_len_threshold=free_text_len_threshold,
            free_text_tokens_threshold=free_text_tokens_threshold,
        )
    return _sample_pandas_series(
        pd.Series(series),
        sample_size=sample_size,
        seed=seed,
        free_text_len_threshold=free_text_len_threshold,
        free_text_tokens_threshold=free_text_tokens_threshold,
    )


def sample_table(
    df: Optional[Any],
    columns: Optional[List[str]] = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
) -> Dict[str, ColumnSample]:
    """
    Muestrea todas las columnas de una tabla (Polars, pandas o dict) o crea muestras vacías si `df` es None.
    """
    if df is None:
        if not columns:
            return {}
        return {
            col: ColumnSample(
                column=col,
                dtype="unknown",
                values=[],
                sample_size=0,
                total_non_null=0,
            )
            for col in columns
        }

    if HAS_POLARS and isinstance(df, pl.LazyFrame):
        df = df.collect()

    if HAS_POLARS and isinstance(df, pl.DataFrame):
        target_cols = columns if columns is not None else list(df.columns)
        samples: Dict[str, ColumnSample] = {}
        for col in target_cols:
            if col in df.columns:
                samples[col] = _sample_polars_series(df[col], sample_size=sample_size, seed=seed)
            else:
                samples[col] = ColumnSample(column=col, dtype="unknown", values=[], sample_size=0, total_non_null=0)
        return samples

    if isinstance(df, dict):
        df = pd.DataFrame(df)

    if isinstance(df, pd.DataFrame):
        target_cols = columns if columns is not None else [str(c) for c in df.columns]
        samples = {}
        for col in target_cols:
            if col in df.columns:
                samples[col] = _sample_pandas_series(df[col], sample_size=sample_size, seed=seed)
            else:
                samples[col] = ColumnSample(column=col, dtype="unknown", values=[], sample_size=0, total_non_null=0)
        return samples

    raise TypeError(f"Tipo de tabla no soportado: {type(df)!r}")
