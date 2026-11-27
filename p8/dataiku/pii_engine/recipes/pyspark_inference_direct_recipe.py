# -*- coding: utf-8 -*-
"""Dataiku PySpark recipe: infiere PII por columna usando `pii_engine`.

Configurar los tres `model.pkl`, el dataset de entrada y el de salida. La
receta usa Spark para leer y muestrear cuando el dataset lo permite; si Spark
no puede leerlo o un job falla, cae automaticamente al lector por chunks de
Dataiku (`iter_dataframes` + `reservoir_sample`). La conversion a pandas se
hace con `collect()` y Arrow desactivado para evitar incompatibilidades de
pyarrow. El analisis corre en el driver sobre una muestra acotada.

Variables de configuracion al inicio del archivo. Los prints con `flush`
permiten identificar la etapa exacta que falla en el log del job.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import dataiku
from dataiku import spark as dkuspark
from pyspark.sql import SparkSession

INPUT_DATASET = "data_test_final_2_copy"
OUTPUT_DATASET = "pii_results_test"

BINARY_MODEL_FOLDER = "model_binary_v1"
ENTITY_MODEL_FOLDER = "model_entity_v1"
CONTENT_MODEL_FOLDER = "model_content_v1"
MODEL_FILENAME = "model.pkl"

BINARY_MODEL_PATH = "/dataiku/dss_data/managed_folders/DSS_PII/sv35BzIv/model.pkl"
ENTITY_MODEL_PATH = "/dataiku/dss_data/managed_folders/DSS_PII/26orYGij/model.pkl"
CONTENT_MODEL_PATH = "/dataiku/dss_data/managed_folders/DSS_PII/2TvdM80q/model.pkl"

INPUT_MODE = "dataiku"
PARQUET_PATH = ""

SAMPLING_MODE = "random"
ROW_RATIO = 0.02
ROW_MIN_ROWS: Optional[int] = None
ROW_MAX_ROWS: Optional[int] = 5000
USE_EXACT_ROW_COUNT = False
COLUMN_SAMPLE_SIZE = 500
SAMPLING_SEED = 42
CHUNK_SIZE = 50_000

CONVERT_MODE = "collect"
DISABLE_ARROW = True

RESULT_COLUMNS = [
    "table",
    "column",
    "pii",
    "prob_pii",
    "entity",
    "prob_entity",
    "evidence",
    "match_rate",
    "sample_size",
    "needs_review",
    "classification",
    "engine_version",
    "model_version",
    "config_version",
    "schema_version",
]


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _target_rows(total_rows: int) -> int:
    from pii_engine.core.row_sampling import resolve_row_sample_size

    return resolve_row_sample_size(
        total_rows,
        ROW_RATIO,
        min_rows=ROW_MIN_ROWS,
        max_rows=ROW_MAX_ROWS,
    )


def _resolve_model_paths() -> dict:
    """Resuelve los tres `model.pkl` desde managed folders con respaldo local."""
    folders = {
        "binary": (BINARY_MODEL_FOLDER, BINARY_MODEL_PATH),
        "entity": (ENTITY_MODEL_FOLDER, ENTITY_MODEL_PATH),
        "content": (CONTENT_MODEL_FOLDER, CONTENT_MODEL_PATH),
    }
    resolved = {}
    for label, (folder_name, fallback) in folders.items():
        path = None
        try:
            folder = dataiku.Folder(folder_name)
            get_path = getattr(folder, "get_path", None)
            if callable(get_path):
                root = Path(get_path())
                candidate = root / MODEL_FILENAME if root.is_dir() else root
                if candidate.is_file():
                    path = candidate
        except Exception as exc:
            _log(
                "No se pudo resolver folder %s (%s: %s)"
                % (folder_name, type(exc).__name__, exc)
            )
        if path is None:
            path = Path(str(fallback)).expanduser()
        resolved[label] = path
    return resolved


def _validate_model_paths(paths: dict) -> None:
    missing = [
        "%s=%r" % (label, str(path))
        for label, path in paths.items()
        if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError("Faltan modelos: " + "; ".join(missing))


def _load_spark_dataframe(spark: Any, dataset: Any) -> Any:
    if INPUT_MODE == "parquet":
        if not PARQUET_PATH:
            raise ValueError("PARQUET_PATH es obligatorio cuando INPUT_MODE='parquet'")
        return spark.read.parquet(PARQUET_PATH)
    if INPUT_MODE != "dataiku":
        raise ValueError("INPUT_MODE debe ser 'dataiku' o 'parquet'")
    return dkuspark.get_dataframe(spark, dataset)


def _prepare_spark_sample(spark_df: Any) -> Any:
    mode = str(SAMPLING_MODE).strip().lower()
    if mode == "head":
        limit = int(ROW_MAX_ROWS or 50_000)
        return spark_df.limit(limit)
    if mode != "random":
        raise ValueError("SAMPLING_MODE debe ser 'head' o 'random'")

    if USE_EXACT_ROW_COUNT:
        total_rows = int(spark_df.count())
        if total_rows <= 0:
            return spark_df.limit(0)
        target_rows = _target_rows(total_rows)
        if target_rows >= total_rows:
            return spark_df
        fraction = min(1.0, (float(target_rows) / float(total_rows)) * 1.10)
        return spark_df.sample(
            withReplacement=False,
            fraction=fraction,
            seed=int(SAMPLING_SEED),
        ).limit(int(target_rows))

    target_rows = int(ROW_MAX_ROWS or 50_000)
    fraction = min(1.0, max(float(ROW_RATIO), 0.0))
    return spark_df.sample(
        withReplacement=False,
        fraction=fraction,
        seed=int(SAMPLING_SEED),
    ).limit(target_rows)


def _to_pandas(spark: Any, spark_df: Any) -> pd.DataFrame:
    if DISABLE_ARROW:
        spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "false")
    if CONVERT_MODE == "toPandas":
        return spark_df.toPandas()
    if CONVERT_MODE != "collect":
        raise ValueError("CONVERT_MODE debe ser 'collect' o 'toPandas'")
    rows = spark_df.collect()
    if not rows:
        return pd.DataFrame(columns=list(spark_df.columns))
    return pd.DataFrame([row.asDict() for row in rows])


def _extract_record_count(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in ("recordCount", "record_count", "recordsCount", "nbRecords", "rows"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _dataset_total_rows(dataset: Any) -> Optional[int]:
    for method_name in ("get_info", "get_metadata"):
        method = getattr(dataset, method_name, None)
        if not callable(method):
            continue
        try:
            count = _extract_record_count(method())
        except Exception:
            count = None
        if count is not None:
            return count
    return None


def _read_dataiku_sample(dataset: Any) -> pd.DataFrame:
    """Lee una muestra por chunks de Dataiku sin depender de Spark."""
    from pii_engine.core.row_sampling import reservoir_sample, sample_dataframe

    total_rows = _dataset_total_rows(dataset)
    if total_rows is not None and total_rows >= 0:
        target_rows = _target_rows(total_rows)
    else:
        target_rows = int(ROW_MAX_ROWS or 50_000)

    if target_rows <= 0:
        return pd.DataFrame()

    chunks = None
    iterator = getattr(dataset, "iter_dataframes", None)
    if callable(iterator):
        try:
            chunks = iterator(chunksize=int(CHUNK_SIZE))
        except TypeError:
            try:
                chunks = iterator()
            except TypeError:
                chunks = None

    if chunks is not None:
        return reservoir_sample(chunks, target_rows, seed=int(SAMPLING_SEED))

    frame = dataset.get_dataframe()
    return sample_dataframe(
        frame,
        max_rows=target_rows,
        mode="random",
        seed=int(SAMPLING_SEED),
    )


def main() -> None:
    _log("pii_engine direct recipe: inicio")
    _log("python: %s" % sys.version.replace("\n", " "))

    input_dataset = dataiku.Dataset(INPUT_DATASET)

    spark = None
    try:
        spark = SparkSession.builder.getOrCreate()
        _log("spark: %s" % spark.version)
        spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "false")
    except Exception as exc:
        _log(
            "Spark no disponible (%s: %s)"
            % (type(exc).__name__, exc)
        )
        spark = None

    sample_df = None
    if spark is not None:
        try:
            spark_df = _load_spark_dataframe(spark, input_dataset)
            _log("spark columns: %d" % len(spark_df.columns))
            sampled = _prepare_spark_sample(spark_df)
            sample_df = _to_pandas(spark, sampled)
            _log("sample (spark): %s" % (sample_df.shape,))
        except Exception as exc:
            _log(
                "Lectura Spark fallo (%s: %s); usando fallback Dataiku"
                % (type(exc).__name__, exc)
            )
            sample_df = None

    if sample_df is None or sample_df.empty:
        sample_df = _read_dataiku_sample(input_dataset)
        _log("sample (dataiku): %s" % (sample_df.shape,))

    paths = _resolve_model_paths()
    _validate_model_paths(paths)
    _log(
        "modelos: binary=%s entity=%s content=%s"
        % (paths["binary"], paths["entity"], paths["content"])
    )

    from pii_engine.core.analyzer import PIIAnalyzer

    analyzer = PIIAnalyzer(
        binary_model_bundle=str(paths["binary"]),
        entity_model_bundle=str(paths["entity"]),
        content_model_path=str(paths["content"]),
    )
    results = analyzer.analyze(
        data=sample_df,
        table_meta=INPUT_DATASET,
        sample_size=int(COLUMN_SAMPLE_SIZE),
        seed=int(SAMPLING_SEED),
    )
    _log("results: %d" % len(results))

    output_df = pd.DataFrame([result.to_dict() for result in results])
    if output_df.empty:
        output_df = pd.DataFrame(columns=RESULT_COLUMNS)
    else:
        output_df = output_df.reindex(columns=RESULT_COLUMNS)

    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(output_df)
    _log("written: %d" % len(output_df))


try:
    main()
except Exception:
    print(traceback.format_exc(), file=sys.stderr, flush=True)
    raise
