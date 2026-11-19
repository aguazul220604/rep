# -*- coding: utf-8 -*-
"""Parsing, validacion y utilidades del reporte de incidentes."""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


class DataValidationError(ValueError):
    """Error de negocio mostrado al usuario cuando el archivo no es valido."""


REQUIRED_COLUMNS = [
    "number",
    "short_description",
    "u_business_application",
    "priority",
    "opened_at",
    "assignment_group",
    "state",
    "contact_type",
    "location.city",
    "u_resolution_category",
    "reassignment_count",
]

DATE_COLUMNS = ("sys_created_on", "opened_at", "resolved_at")

WEEKDAYS_ES = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]

PRIORITY_ORDER = [
    "1 - Critical",
    "2 - High",
    "3 - Moderate",
    "4 - Low",
    "No especificada",
]

TABLE_COLUMNS = [
    {"name": "Incidente", "id": "Incidente"},
    {"name": "Descripción", "id": "Descripción"},
    {"name": "Aplicación", "id": "Aplicación"},
    {"name": "Prioridad", "id": "Prioridad"},
    {"name": "Grupo", "id": "Grupo"},
    {"name": "Canal", "id": "Canal"},
    {"name": "Apertura", "id": "Apertura"},
    {"name": "MTTR (h)", "id": "MTTR (h)"},
    {"name": "Resolución", "id": "Resolución"},
    {"name": "TSA", "id": "TSA"},
    {"name": "Ciudad", "id": "Ciudad"},
]

COLUMN_FIXES = {
    "A\ufffdejamiento": "Añejamiento",
    "Periodo de a\ufffdejamiento": "Periodo de añejamiento",
    "AÃ±ejamiento": "Añejamiento",
    "Periodo de aÃ±ejamiento": "Periodo de añejamiento",
    "A�ejamiento": "Añejamiento",
    "Periodo de a�ejamiento": "Periodo de añejamiento",
}


def _clean_columns(columns: List[Any]) -> List[str]:
    cleaned = []
    for column in columns:
        name = str(column).replace("\ufeff", "").strip()
        cleaned.append(COLUMN_FIXES.get(name, name))
    return cleaned


def _read_bytes(contents: bytes, filename: str) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith(".csv") or lower.endswith(".txt"):
        last_error: Optional[Exception] = None
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                frame = pd.read_csv(io.BytesIO(contents), encoding=encoding)
                if frame.shape[1] == 1:
                    frame = pd.read_csv(
                        io.BytesIO(contents), encoding=encoding, sep=";"
                    )
                return frame
            except UnicodeDecodeError as exc:
                last_error = exc
            except pd.errors.EmptyDataError as exc:
                raise DataValidationError("El archivo CSV está vacío.") from exc
        raise DataValidationError(
            "No fue posible leer el CSV. Verifica que el archivo no esté dañado."
        ) from last_error
    if lower.endswith(".xlsx"):
        try:
            return pd.read_excel(
                io.BytesIO(contents), sheet_name=0, engine="openpyxl"
            )
        except Exception as exc:
            raise DataValidationError(
                "No fue posible leer el Excel. Verifica que la primera hoja "
                "contenga el reporte y que el archivo no esté protegido."
            ) from exc
    if lower.endswith(".xls"):
        raise DataValidationError(
            "El formato .xls no está soportado. Guarda el reporte como .xlsx."
        )
    raise DataValidationError("Formato no soportado. Usa un archivo CSV o XLSX.")


def read_dataset(contents: bytes, filename: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Lee y normaliza el reporte. Devuelve el DataFrame y sus metadatos."""
    frame = _read_bytes(contents, filename)
    frame.columns = _clean_columns(list(frame.columns))
    frame = frame.dropna(how="all").reset_index(drop=True)

    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise DataValidationError(
            "El archivo no contiene las columnas requeridas: "
            + ", ".join(missing)
        )

    if frame.empty:
        raise DataValidationError("El archivo no contiene filas para analizar.")

    for column in DATE_COLUMNS:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")

    invalid_dates = int(frame["opened_at"].isna().sum())
    if invalid_dates:
        frame = frame.loc[frame["opened_at"].notna()].copy()
    if frame.empty:
        raise DataValidationError(
            "Ninguna fila tiene una fecha de apertura (opened_at) válida."
        )

    for column in ("reassignment_count", "Añejamiento"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["mttr_hours"] = (
        (frame["resolved_at"] - frame["opened_at"]).dt.total_seconds() / 3600.0
        if "resolved_at" in frame.columns
        else pd.Series(pd.NA, index=frame.index, dtype="Float64")
    )
    frame["mttr_hours"] = frame["mttr_hours"].where(frame["mttr_hours"] >= 0)
    frame["opened_date"] = frame["opened_at"].dt.normalize()
    frame["opened_day"] = frame["opened_at"].dt.date
    frame["hour"] = frame["opened_at"].dt.hour
    frame["weekday_num"] = frame["opened_at"].dt.dayofweek
    frame["weekday"] = frame["weekday_num"].map(dict(enumerate(WEEKDAYS_ES)))

    frame["app"] = (
        frame["u_business_application"].astype("string").fillna("No especificada")
    )
    frame["priority"] = frame["priority"].astype("string").fillna("No especificada")
    frame["assignment_group"] = (
        frame["assignment_group"].astype("string").fillna("No especificado")
    )
    frame["contact_type"] = (
        frame["contact_type"].astype("string").fillna("No especificado")
    )
    frame["city"] = frame["location.city"].astype("string").fillna("No especificada")
    frame["resolution_category"] = (
        frame["u_resolution_category"].astype("string").fillna("Sin categoría")
    )
    frame["state"] = frame["state"].astype("string").fillna("No especificado")
    frame["tsa"] = (
        frame["TSA Activo"].astype("string").fillna("No especificado")
        if "TSA Activo" in frame.columns
        else pd.Series("No especificado", index=frame.index, dtype="string")
    )
    frame["app_criticality"] = (
        frame["App_Criticality"].astype("string").fillna("No especificada")
        if "App_Criticality" in frame.columns
        else pd.Series("No especificada", index=frame.index, dtype="string")
    )
    frame["is_closed"] = frame["state"].str.lower().eq("closed")

    metadata = {
        "source": filename,
        "rows": int(len(frame)),
        "original_rows": int(len(frame) + invalid_dates),
        "invalid_dates": invalid_dates,
        "min_date": frame["opened_at"].min().date().isoformat(),
        "max_date": frame["opened_at"].max().date().isoformat(),
        "columns": int(len(frame.columns)),
    }
    return frame, metadata


def apply_filters(
    frame: pd.DataFrame,
    start_date: Optional[str],
    end_date: Optional[str],
    priorities: Optional[List[str]],
    apps: Optional[List[str]],
    groups: Optional[List[str]],
) -> pd.DataFrame:
    filtered = frame
    if start_date:
        filtered = filtered.loc[
            filtered["opened_at"].dt.date >= date.fromisoformat(str(start_date)[:10])
        ]
    if end_date:
        filtered = filtered.loc[
            filtered["opened_at"].dt.date <= date.fromisoformat(str(end_date)[:10])
        ]
    if priorities:
        filtered = filtered.loc[filtered["priority"].isin(priorities)]
    if apps:
        filtered = filtered.loc[filtered["app"].isin(apps)]
    if groups:
        filtered = filtered.loc[filtered["assignment_group"].isin(groups)]
    return filtered.copy()


def filter_options(frame: pd.DataFrame) -> Dict[str, List[str]]:
    priorities = [
        value
        for value in PRIORITY_ORDER
        if value in set(frame["priority"].dropna().unique())
    ]
    extra_priorities = sorted(
        set(frame["priority"].dropna().unique()) - set(priorities)
    )
    return {
        "priorities": priorities + extra_priorities,
        "apps": sorted(frame["app"].dropna().unique().tolist()),
        "groups": sorted(frame["assignment_group"].dropna().unique().tolist()),
    }


def make_kpis(frame: pd.DataFrame) -> Dict[str, Any]:
    mttr = frame["mttr_hours"].dropna()
    reassignments = pd.to_numeric(
        frame.get("reassignment_count"), errors="coerce"
    ).dropna()
    groups = frame["assignment_group"].nunique()
    return {
        "total": int(len(frame)),
        "mttr_median": float(mttr.median()) if not mttr.empty else None,
        "mttr_p90": float(mttr.quantile(0.90)) if not mttr.empty else None,
        "reassign_avg": float(reassignments.mean()) if not reassignments.empty else None,
        "groups": int(groups),
        "closed_pct": (
            float(frame["is_closed"].mean() * 100) if len(frame) else None
        ),
    }


def table_records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    if frame.empty:
        return []
    view = pd.DataFrame(
        {
            "Incidente": frame["number"].astype("string").fillna(""),
            "Descripción": frame["short_description"].astype("string").fillna(""),
            "Aplicación": frame["app"],
            "Prioridad": frame["priority"],
            "Grupo": frame["assignment_group"],
            "Canal": frame["contact_type"],
            "Apertura": frame["opened_at"].dt.strftime("%Y-%m-%d %H:%M"),
            "MTTR (h)": frame["mttr_hours"].round(2),
            "Resolución": frame["resolution_category"],
            "TSA": frame["tsa"],
            "Ciudad": frame["city"],
        }
    )
    view["MTTR (h)"] = view["MTTR (h)"].astype(object).where(
        view["MTTR (h)"].notna(), None
    )
    return view.to_dict("records")


def _mttr_buckets(frame: pd.DataFrame) -> pd.DataFrame:
    bins = [-0.001, 1, 4, 8, 24, 48, 144, float("inf")]
    labels = [
        "< 1 h",
        "1–4 h",
        "4–8 h",
        "8–24 h",
        "24–48 h",
        "48–144 h",
        "> 144 h",
    ]
    values = pd.cut(
        frame["mttr_hours"], bins=bins, labels=labels, right=False
    )
    result = (
        values.value_counts()
        .reindex(labels, fill_value=0)
        .rename_axis("Rango de resolución")
        .reset_index(name="Incidentes")
    )
    total = int(result["Incidentes"].sum())
    result["% del total"] = (
        (result["Incidentes"] / total * 100).round(1) if total else 0
    )
    return result


def _summary_frames(frame: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    total = len(frame)
    priority = (
        frame.groupby("priority", dropna=False)
        .agg(
            **{
                "Incidentes": ("number", "count"),
                "MTTR mediana (h)": ("mttr_hours", "median"),
            }
        )
        .reset_index()
        .rename(columns={"priority": "Prioridad"})
    )
    priority["MTTR mediana (h)"] = priority["MTTR mediana (h)"].round(2)
    priority["% del total"] = (priority["Incidentes"] / total * 100).round(1)

    apps = (
        frame.groupby("app")
        .agg(
            **{
                "Incidentes": ("number", "count"),
                "MTTR mediana (h)": ("mttr_hours", "median"),
            }
        )
        .reset_index()
        .rename(columns={"app": "Aplicación"})
        .sort_values("Incidentes", ascending=False)
    )
    apps["MTTR mediana (h)"] = apps["MTTR mediana (h)"].round(2)
    apps["% del total"] = (apps["Incidentes"] / total * 100).round(1)

    groups = (
        frame.groupby("assignment_group")
        .agg(
            **{
                "Incidentes": ("number", "count"),
                "MTTR mediana (h)": ("mttr_hours", "median"),
            }
        )
        .reset_index()
        .rename(columns={"assignment_group": "Grupo de asignación"})
        .sort_values("Incidentes", ascending=False)
    )
    groups["MTTR mediana (h)"] = groups["MTTR mediana (h)"].round(2)
    groups["% del total"] = (groups["Incidentes"] / total * 100).round(1)

    channels = (
        frame.groupby("contact_type")
        .agg(**{"Incidentes": ("number", "count")})
        .reset_index()
        .rename(columns={"contact_type": "Canal"})
        .sort_values("Incidentes", ascending=False)
    )
    channels["% del total"] = (channels["Incidentes"] / total * 100).round(1)

    tsa = (
        frame.groupby("tsa")
        .agg(**{"Incidentes": ("number", "count")})
        .reset_index()
        .rename(columns={"tsa": "TSA Activo"})
        .sort_values("Incidentes", ascending=False)
    )
    tsa["% del total"] = (tsa["Incidentes"] / total * 100).round(1)

    resolution = (
        frame.groupby("resolution_category")
        .agg(**{"Incidentes": ("number", "count")})
        .reset_index()
        .rename(columns={"resolution_category": "Categoría de resolución"})
        .sort_values("Incidentes", ascending=False)
    )
    resolution["% del total"] = (resolution["Incidentes"] / total * 100).round(1)

    mttr = _mttr_buckets(frame)
    return {
        "Prioridad": priority,
        "Aplicaciones": apps,
        "Grupos": groups,
        "Canales": channels,
        "TSA": tsa,
        "Resolución": resolution,
        "MTTR": mttr,
    }


def export_excel(frame: pd.DataFrame, metadata: Dict[str, Any]) -> io.BytesIO:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    kpis = make_kpis(frame)
    summary = pd.DataFrame(
        {
            "Indicador": [
                "Archivo",
                "Incidentes",
                "Periodo",
                "MTTR mediana (h)",
                "MTTR p90 (h)",
                "Reasignaciones promedio",
                "Grupos activos",
                "% cerrados",
            ],
            "Valor": [
                metadata.get("source", ""),
                "{:,}".format(kpis["total"]),
                "{} a {}".format(
                    metadata.get("min_date", ""), metadata.get("max_date", "")
                ),
                "—" if kpis["mttr_median"] is None else "{:.2f}".format(kpis["mttr_median"]),
                "—" if kpis["mttr_p90"] is None else "{:.2f}".format(kpis["mttr_p90"]),
                "—" if kpis["reassign_avg"] is None else "{:.2f}".format(kpis["reassign_avg"]),
                kpis["groups"],
                "—" if kpis["closed_pct"] is None else "{:.1f}%".format(kpis["closed_pct"]),
            ],
        }
    )

    data_columns = [
        column
        for column in (
            "number",
            "short_description",
            "u_business_application",
            "priority",
            "state",
            "assignment_group",
            "contact_type",
            "opened_at",
            "resolved_at",
            "mttr_hours",
            "u_resolution_category",
            "location.city",
            "TSA Activo",
            "reassignment_count",
            "App_Criticality",
        )
        if column in frame.columns
    ]
    data = frame[data_columns].copy()
    if "mttr_hours" in data.columns:
        data["mttr_hours"] = data["mttr_hours"].round(2)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        data.to_excel(writer, sheet_name="Datos", index=False)
        summary.to_excel(writer, sheet_name="Resumen", index=False)
        for sheet, table in _summary_frames(frame).items():
            table.to_excel(writer, sheet_name=sheet, index=False)

        workbook = writer.book
        header_fill = PatternFill("solid", fgColor="003B49")
        header_font = Font(bold=True, color="FFFFFF")
        for worksheet in workbook.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for cell in worksheet[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(vertical="center")
            for column_cells in worksheet.columns:
                max_length = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells
                )
                letter = get_column_letter(column_cells[0].column)
                worksheet.column_dimensions[letter].width = min(
                    max(max_length + 2, 12), 60
                )

    buffer.seek(0)
    return buffer


def analysis_meta(frame: pd.DataFrame, source: str) -> str:
    if frame is None or frame.empty:
        return "Sin resultados para los filtros seleccionados"
    period = "{} – {}".format(
        frame["opened_at"].min().strftime("%d/%m/%Y"),
        frame["opened_at"].max().strftime("%d/%m/%Y"),
    )
    return "{} incidentes · {} · {}".format(
        "{:,}".format(len(frame)), period, source or "reporte"
    )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
