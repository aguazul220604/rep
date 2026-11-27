# -*- coding: utf-8 -*-
"""Dataiku PySpark recipe: prueba directa de `pii_engine` sin DataikuAdapter.

Configurar los tres `model.pkl`, el dataset de entrada y el de salida. La
receta usa Spark para leer y muestrear, `collect()` + pandas por defecto para
evitar Arrow y llama directamente a `PIIAnalyzer`. Los prints con `flush`
permiten identificar la etapa exacta que falla.
"""
from __future__ import annotations

import sys

import pandas as pd

import dataiku
from dataiku import spark as dkuspark
from pyspark.sql import SparkSession

INPUT_DATASET = "data_test_final_2_copy"
OUTPUT_DATASET = "pii_results_test"

BINARY_MODEL_PATH = "/dataiku/dss_data/managed_folders/DSS_PII/sv35BzIv/model.pkl"
ENTITY_MODEL_PATH = "/dataiku/dss_data/managed_folders/DSS_PII/26orYGij/model.pkl"
CONTENT_MODEL_PATH = "/dataiku/dss_data/managed_folders/DSS_PII/2TvdM80q/model.pkl"

INPUT_MODE = "dataiku"
PARQUET_PATH = ""

SAMPLING_MODE = "head"
ROW_RATIO = 0.02
ROW_MAX_ROWS = 5000
COLUMN_SAMPLE_SIZE = 500
SAMPLING_SEED = 42

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


def _load_spark_dataframe(spark, dataset):
    if INPUT_MODE == "parquet":
        if not PARQUET_PATH:
            raise ValueError("PARQUET_PATH es obligatorio cuando INPUT_MODE='parquet'")
        return spark.read.parquet(PARQUET_PATH)
    if INPUT_MODE != "dataiku":
        raise ValueError("INPUT_MODE debe ser 'dataiku' o 'parquet'")
    return dkuspark.get_dataframe(spark, dataset)


def _sample_spark_dataframe(spark_df):
    if SAMPLING_MODE == "head":
        return spark_df.limit(int(ROW_MAX_ROWS))
    if SAMPLING_MODE != "random":
        raise ValueError("SAMPLING_MODE debe ser 'head' o 'random'")
    fraction = min(1.0, max(float(ROW_RATIO), 0.0))
    return spark_df.sample(
        withReplacement=False,
        fraction=fraction,
        seed=int(SAMPLING_SEED),
    ).limit(int(ROW_MAX_ROWS))


def _to_pandas(spark, spark_df):
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


def _validate_model_paths():
    from pathlib import Path

    required = {
        "binary": BINARY_MODEL_PATH,
        "entity": ENTITY_MODEL_PATH,
        "content": CONTENT_MODEL_PATH,
    }
    missing = []
    for label, raw_path in required.items():
        path = Path(str(raw_path)).expanduser()
        if not path.is_file():
            missing.append("%s=%s" % (label, str(path)))
    if missing:
        raise FileNotFoundError("Faltan modelos: " + "; ".join(missing))


def main():
    print("python:", sys.version, flush=True)

    spark = SparkSession.builder.appName("pii_engine_spark_test").getOrCreate()
    print("spark:", spark.version, flush=True)

    dataset = dataiku.Dataset(INPUT_DATASET)
    spark_df = _load_spark_dataframe(spark, dataset)
    print("spark columns:", len(spark_df.columns), flush=True)

    sample_spark = _sample_spark_dataframe(spark_df)
    sample_pdf = _to_pandas(spark, sample_spark)
    print("sample:", sample_pdf.shape, flush=True)

    _validate_model_paths()
    from pii_engine.core.analyzer import PIIAnalyzer

    analyzer = PIIAnalyzer(
        binary_model_bundle=BINARY_MODEL_PATH,
        entity_model_bundle=ENTITY_MODEL_PATH,
        content_model_path=CONTENT_MODEL_PATH,
    )
    results = analyzer.analyze(
        data=sample_pdf,
        table_meta=INPUT_DATASET,
        sample_size=int(COLUMN_SAMPLE_SIZE),
        seed=int(SAMPLING_SEED),
    )
    print("results:", len(results), flush=True)

    output_df = pd.DataFrame([result.to_dict() for result in results])
    if output_df.empty:
        output_df = pd.DataFrame(columns=RESULT_COLUMNS)
    else:
        output_df = output_df.reindex(columns=RESULT_COLUMNS)

    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(output_df)
    print("written:", len(output_df), flush=True)


main()
