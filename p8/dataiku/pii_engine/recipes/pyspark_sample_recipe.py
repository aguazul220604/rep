# -*- coding: utf-8 -*-
"""Dataiku PySpark recipe: muestrea un dataset para el motor PII.

Spark solo lee y muestrea. La muestra se escribe en un dataset intermedio que
consume la receta Python de inferencia. Esta receta no importa `pii_engine` ni
carga modelos, por lo que evita el conflicto entre el driver Spark y sklearn.

Variables de configuracion al inicio del archivo.
"""
from __future__ import annotations

import math
import os
import sys
import traceback
from typing import Any, Optional

import dataiku
from dataiku import spark as dkuspark
from pyspark.sql import SparkSession

INPUT_DATASET = "data_test_final_2_copy"
OUTPUT_SAMPLE_DATASET = "pii_sample_data_test_final_2_copy"

INPUT_DATASET = os.environ.get("DKU_INPUT_0_DATASET_ID", INPUT_DATASET)
OUTPUT_SAMPLE_DATASET = os.environ.get(
    "DKU_OUTPUT_0_DATASET_ID", OUTPUT_SAMPLE_DATASET
)

INPUT_MODE = "dataiku"
PARQUET_PATH = ""

SAMPLING_MODE = "random"
ROW_RATIO = 0.02
ROW_MIN_ROWS: Optional[int] = None
ROW_MAX_ROWS: Optional[int] = 5000
USE_EXACT_ROW_COUNT = False
SAMPLING_SEED = 42


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _validate_config() -> None:
    mode = str(SAMPLING_MODE).strip().lower()
    if mode not in {"head", "random"}:
        raise ValueError("SAMPLING_MODE debe ser 'head' o 'random'")
    if not 0 < float(ROW_RATIO) <= 1:
        raise ValueError("ROW_RATIO debe estar en (0, 1]")
    if INPUT_MODE not in {"dataiku", "parquet"}:
        raise ValueError("INPUT_MODE debe ser 'dataiku' o 'parquet'")
    if INPUT_MODE == "parquet" and not PARQUET_PATH:
        raise ValueError("PARQUET_PATH es obligatorio cuando INPUT_MODE='parquet'")


def _target_rows(total_rows: int) -> int:
    total = int(total_rows)
    if total <= 0:
        return 0
    target = max(1, int(math.ceil(total * float(ROW_RATIO))))
    if ROW_MIN_ROWS is not None:
        target = max(target, int(ROW_MIN_ROWS))
    if ROW_MAX_ROWS is not None:
        target = min(target, int(ROW_MAX_ROWS))
    return min(target, total)


def _load_spark_dataframe(spark: Any, dataset: Any) -> Any:
    if INPUT_MODE == "parquet":
        return spark.read.parquet(PARQUET_PATH)
    return dkuspark.get_dataframe(spark, dataset)


def _prepare_spark_sample(spark_df: Any) -> Any:
    mode = str(SAMPLING_MODE).strip().lower()
    if mode == "head":
        limit = int(ROW_MAX_ROWS or 50_000)
        return spark_df.limit(limit)

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


def main() -> None:
    _validate_config()
    _log("pii_engine spark sample recipe: inicio")
    _log("python: %s" % sys.version.replace("\n", " "))

    input_dataset = dataiku.Dataset(INPUT_DATASET)
    spark = SparkSession.builder.getOrCreate()
    _log("spark: %s" % spark.version)

    spark_df = _load_spark_dataframe(spark, input_dataset)
    _log("spark columns: %d" % len(spark_df.columns))

    sampled = _prepare_spark_sample(spark_df)
    sample_rows = int(sampled.count())
    _log("sample rows: %d" % sample_rows)

    output_dataset = dataiku.Dataset(OUTPUT_SAMPLE_DATASET)
    dkuspark.write_with_schema(output_dataset, sampled)
    _log("written sample: %d" % sample_rows)


try:
    main()
except Exception:
    print(traceback.format_exc(), file=sys.stderr, flush=True)
    raise
