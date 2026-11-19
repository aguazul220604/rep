# -*- coding: utf-8 -*-
"""Dataiku Python recipe: ingesta del reporte cargado desde la webapp.

Salidas requeridas en la receta:
- incidentes_limpios
- incidentes_carga_estado
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import dataiku

from incidentes_lib import config, parsing


def _folder():
    return dataiku.Folder(config.UPLOAD_FOLDER_ID)


def _read_json(folder, path: str) -> Dict[str, Any]:
    with folder.get_download_stream(path) as stream:
        return json.loads(stream.read().decode("utf-8"))


def _read_bytes(folder, path: str) -> bytes:
    with folder.get_download_stream(path) as stream:
        return stream.read()


def _dataiku_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Convierte dtypes extension de pandas a dtypes que Dataiku escribe mejor."""
    output = frame.copy()
    for column in output.columns:
        dtype = str(output[column].dtype)
        if dtype == "string":
            output[column] = output[column].astype(object)
        elif dtype == "Float64":
            output[column] = output[column].astype("float64")
        elif dtype == "Int64":
            output[column] = output[column].astype("float64")
    return output


def _write_status(
    status: str,
    message: str,
    pointer: Optional[Dict[str, Any]] = None,
    rows: int = 0,
    columns: int = 0,
) -> None:
    pointer = pointer or {}
    frame = pd.DataFrame(
        [
            {
                "status": status,
                "message": message[:2000],
                "file_name": pointer.get("original_name"),
                "file_path": pointer.get("path"),
                "run_id": pointer.get("run_id"),
                "uploaded_at": pointer.get("uploaded_at"),
                "processed_at": datetime.now().isoformat(timespec="seconds"),
                "rows": rows,
                "columns": columns,
            }
        ]
    )
    dataiku.Dataset(config.STATUS_DATASET).write_with_schema(
        _dataiku_frame(frame)
    )


def main() -> None:
    pointer: Dict[str, Any] = {}
    try:
        folder = _folder()
        pointer = _read_json(folder, config.LATEST_POINTER)
        path = pointer.get("path")
        original_name = pointer.get("original_name") or path
        if not path:
            raise parsing.DataValidationError(
                "No hay una ruta de archivo en latest.json."
            )

        raw = _read_bytes(folder, path)
        frame, metadata = parsing.read_dataset(raw, original_name)
        dataiku.Dataset(config.OUTPUT_DATASET).write_with_schema(
            _dataiku_frame(frame)
        )
        _write_status(
            status="SUCCESS",
            message="Reporte procesado correctamente.",
            pointer=pointer,
            rows=metadata.get("rows", len(frame)),
            columns=metadata.get("columns", len(frame.columns)),
        )
        print(
            "Procesado: {} filas, {} columnas, archivo={}".format(
                len(frame), len(frame.columns), original_name
            )
        )

    except parsing.DataValidationError as exc:
        _write_status(
            status="FAILED",
            message=str(exc),
            pointer=pointer,
            rows=0,
            columns=0,
        )
        print("Validacion fallida: {}".format(exc))

    except Exception as exc:
        traceback.print_exc()
        _write_status(
            status="FAILED",
            message="Error inesperado en la ingesta: {}".format(exc),
            pointer=pointer,
            rows=0,
            columns=0,
        )
        raise


main()
