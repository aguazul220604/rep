# -*- coding: utf-8 -*-
"""Backend Dash para la webapp nativa de Dataiku DSS 14.

Este archivo se pega en el editor Python de una webapp Dash creada por
Dataiku. DSS ya crea el objeto global `app` y arranca el servidor: no se
debe ejecutar app.run() ni crear otro Dash().
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dash import (
    Input,
    Output,
    State,
    callback_context,
    dash_table,
    dcc,
    html,
    no_update,
)
from dash.exceptions import PreventUpdate

from incidentes_lib import charts, config, parsing, styles


MESES_ES = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "septiembre",
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}
DIAS_ES = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]

DASH_OUTPUT_COUNT = 20
MAIN_OUTPUT_COUNT = 24
CACHE: Dict[str, Any] = {"run_id": None, "frame": None}

INDEX_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  {%metas%}
  <title>{%title%}</title>
  {%favicon%}
  {%css%}
  <style>
__APP_CSS__
  </style>
</head>
<body>
  {%app_entry%}
  <footer>
    {%config%}
    {%scripts%}
    {%renderer%}
  </footer>
</body>
</html>"""


def _today_es() -> str:
    today = datetime.now()
    return "{} {} de {} de {}".format(
        DIAS_ES[today.weekday()],
        today.day,
        MESES_ES[today.month],
        today.year,
    )


def _fmt_int(value) -> str:
    return "—" if value is None else "{:,}".format(int(value))


def _fmt_hours(value) -> str:
    return "—" if value is None else "{:,.2f} h".format(float(value))


def _fmt_decimal(value) -> str:
    return "—" if value is None else "{:.2f}".format(float(value))


def _kpi_card(label: str, value_id: str, hint: str) -> html.Div:
    return html.Div(
        className="kpi-card",
        children=[
            html.P(label, className="kpi-label"),
            html.H2("—", id=value_id, className="kpi-value"),
            html.P(hint, className="kpi-hint"),
        ],
    )


def _chart_card(
    title: str,
    subtitle: str,
    children,
    class_name: str = "",
    header_extra=None,
) -> html.Div:
    return html.Div(
        className="chart-card {}".format(class_name).strip(),
        children=[
            html.Div(
                className="card-head",
                children=[
                    html.Div(
                        className="card-titles",
                        children=[
                            html.H3(title),
                            html.P(subtitle),
                        ],
                    ),
                    header_extra if header_extra is not None else html.Span(),
                ],
            ),
            children,
        ],
    )


def build_layout():
    header = html.Div(
        className="app-header",
        children=[
            html.Div(
                className="brand",
                children=[
                    html.Img(
                        src=styles.LOGO_DATA_URI,
                        className="brand-logo",
                        alt="Banamex",
                    ),
                    html.Div(
                        className="brand-text",
                        children=[
                            html.H1("Banamex"),
                            html.P("Reporte de Incidentes"),
                        ],
                    ),
                ],
            ),
            html.Div(_today_es(), className="header-date"),
        ],
    )

    description = html.P(
        "Centralizador para realizar gestión operativa en el manejo del control "
        "de incidentes. Capacidad del sistema para subir los reportes generados "
        "desde ServiceNow correspondientes a las áreas: Incidentes y cambios.",
        className="page-description",
    )

    upload_section = html.Div(
        className="upload-section",
        children=[
            html.P(
                "Permitir la carga del archivo correspondiente al reporte de incidentes",
                className="upload-label",
            ),
            html.Div(
                className="upload-row",
                children=[
                    dcc.Upload(
                        id="upload-data",
                        className="upload-control",
                        multiple=False,
                        accept=".csv,.xlsx",
                        children=html.Div(
                            className="upload-inner",
                            children=[
                                html.Span("↑", className="upload-icon"),
                                html.Div(
                                    children=[
                                        html.Span(
                                            "Subir reporte", className="upload-title"
                                        ),
                                        html.Span(
                                            "CSV o Excel (.xlsx)",
                                            className="upload-hint",
                                        ),
                                    ]
                                ),
                            ],
                        ),
                    ),
                    html.Button(
                        "Autorizar análisis",
                        id="btn-analyze",
                        className="btn-analyze",
                        n_clicks=0,
                        disabled=True,
                    ),
                ],
            ),
            html.Div(
                id="upload-filename",
                className="upload-filename",
                children="Ningún archivo seleccionado",
            ),
            html.P(
                "El archivo se guarda en Dataiku y el procesamiento se ejecuta "
                "en el Flow del proyecto.",
                className="upload-flow-note",
            ),
            html.Div(id="analysis-error", className="error-box"),
        ],
    )

    filters = html.Div(
        className="filters",
        children=[
            html.Div(
                className="filter-field",
                children=[
                    html.Label("Periodo", className="filter-label"),
                    dcc.DatePickerRange(
                        id="filter-dates",
                        display_format="DD/MM/YYYY",
                        first_day_of_week=1,
                        start_date_placeholder_text="Desde",
                        end_date_placeholder_text="Hasta",
                        minimum_nights=0,
                        className="date-filter",
                    ),
                ],
            ),
            html.Div(
                className="filter-field",
                children=[
                    html.Label("Prioridad", className="filter-label"),
                    dcc.Dropdown(
                        id="filter-priority",
                        multi=True,
                        placeholder="Todas",
                        className="dark-dropdown",
                    ),
                ],
            ),
            html.Div(
                className="filter-field",
                children=[
                    html.Label("Aplicación", className="filter-label"),
                    dcc.Dropdown(
                        id="filter-app",
                        multi=True,
                        placeholder="Todas",
                        className="dark-dropdown",
                    ),
                ],
            ),
            html.Div(
                className="filter-field",
                children=[
                    html.Label("Grupo de asignación", className="filter-label"),
                    dcc.Dropdown(
                        id="filter-group",
                        multi=True,
                        placeholder="Todos",
                        className="dark-dropdown",
                    ),
                ],
            ),
            html.Button(
                "Limpiar filtros",
                id="btn-clear-filters",
                className="btn-ghost",
                n_clicks=0,
                disabled=True,
            ),
        ],
    )

    kpis = html.Div(
        className="kpi-grid",
        children=[
            _kpi_card("Incidentes", "kpi-total", "Registros analizados"),
            _kpi_card("MTTR mediana", "kpi-mttr-median", "Tiempo de resolución"),
            _kpi_card("MTTR p90", "kpi-mttr-p90", "Percentil 90"),
            _kpi_card("Reasignaciones", "kpi-reassign", "Promedio por incidente"),
            _kpi_card("Grupos activos", "kpi-groups", "Con carga en el periodo"),
        ],
    )

    daily_card = _chart_card(
        "Incidentes por día",
        "Evolución del volumen de reportes en el periodo",
        dcc.Graph(
            id="chart-daily",
            figure=charts.empty_figure("Sube un reporte para visualizar"),
            config=charts.GRAPH_CONFIG,
            className="chart-graph",
            style={"height": "270px"},
        ),
        class_name="daily-card",
        header_extra=dcc.Checklist(
            id="toggle-peak",
            options=[{"label": "Excluir días pico", "value": "peak"}],
            value=[],
            className="peak-toggle",
        ),
    )

    side_card = _chart_card(
        "Prioridad y canal",
        "Composición de la demanda recibida",
        html.Div(
            className="side-charts",
            children=[
                html.Div(
                    className="mini-chart",
                    children=[
                        html.P("Prioridad", className="mini-title"),
                        dcc.Graph(
                            id="chart-priority",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "215px"},
                        ),
                    ],
                ),
                html.Div(
                    className="mini-chart",
                    children=[
                        html.P("Canal de contacto", className="mini-title"),
                        dcc.Graph(
                            id="chart-contact",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "215px"},
                        ),
                    ],
                ),
            ],
        ),
        class_name="side-card",
    )

    apps_card = _chart_card(
        "Top aplicaciones",
        "Volumen por aplicación; color = MTTR mediana",
        dcc.Graph(
            id="chart-apps",
            figure=charts.empty_figure("Sin datos"),
            config=charts.GRAPH_CONFIG,
            style={"height": "270px"},
        ),
        class_name="apps-card",
    )

    primary_grid = html.Div(
        className="primary-grid",
        children=[daily_card, side_card, apps_card],
    )

    extra_section = html.Div(
        className="extra-section",
        children=[
            html.Div(
                className="section-head",
                children=[
                    html.H2("Análisis complementario"),
                    html.P(
                        "Tiempos, grupos, categorías y distribución operativa"
                    ),
                ],
            ),
            html.Div(
                className="extra-grid",
                children=[
                    _chart_card(
                        "Distribución de MTTR",
                        "Rangos de tiempo de resolución",
                        dcc.Graph(
                            id="chart-mttr-dist",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "285px"},
                        ),
                    ),
                    _chart_card(
                        "MTTR por grupo",
                        "Caja por grupo de asignación (escala log)",
                        dcc.Graph(
                            id="chart-mttr-group",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "285px"},
                        ),
                    ),
                    _chart_card(
                        "Categoría de resolución",
                        "Clasificación del cierre del incidente",
                        dcc.Graph(
                            id="chart-resolution",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "285px"},
                        ),
                    ),
                    _chart_card(
                        "Distribución por TSA",
                        "Participación de cada TSA activo",
                        dcc.Graph(
                            id="chart-tsa",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "285px"},
                        ),
                    ),
                    _chart_card(
                        "Reasignaciones",
                        "Número de reasignaciones por incidente",
                        dcc.Graph(
                            id="chart-reassign",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "285px"},
                        ),
                    ),
                    _chart_card(
                        "Mapa de calor por horario",
                        "% de incidentes de cada día por hora",
                        dcc.Graph(
                            id="chart-heatmap",
                            figure=charts.empty_figure("Sin datos"),
                            config=charts.GRAPH_CONFIG,
                            style={"height": "285px"},
                        ),
                    ),
                ],
            ),
        ],
    )

    download_row = html.Div(
        className="download-row",
        children=[
            html.Button(
                "Descargar análisis",
                id="btn-download",
                className="btn-download",
                n_clicks=0,
                disabled=True,
            ),
            dcc.Download(id="download-analysis"),
        ],
    )

    table_section = html.Div(
        className="table-card",
        children=[
            html.Div(
                className="table-head",
                children=[
                    html.Div(
                        className="card-titles",
                        children=[
                            html.H2("Visualización de datos"),
                            html.P(
                                "Detalle de incidentes del periodo seleccionado"
                            ),
                        ],
                    ),
                    html.Span(
                        "Sin registros", id="table-meta", className="table-meta"
                    ),
                ],
            ),
            dash_table.DataTable(
                id="analysis-table",
                columns=parsing.TABLE_COLUMNS,
                data=[],
                page_size=15,
                sort_action="native",
                filter_action="native",
                style_as_list_view=True,
                style_table={"overflowX": "auto"},
                style_header={
                    "backgroundColor": "#00323D",
                    "color": "#FFFFFF",
                    "fontWeight": "600",
                    "fontSize": "11px",
                    "textTransform": "uppercase",
                    "letterSpacing": "0.04em",
                    "border": "none",
                    "padding": "10px",
                    "textAlign": "left",
                },
                style_data={
                    "backgroundColor": "#FFFFFF",
                    "color": "#1B2B30",
                    "fontSize": "12px",
                    "border": "none",
                    "padding": "9px 10px",
                },
                style_data_conditional=[
                    {
                        "if": {"row_index": "odd"},
                        "backgroundColor": "#F3F7F7",
                    },
                    {
                        "if": {
                            "filter_query": "{MTTR (h)} > 24",
                            "column_id": "MTTR (h)",
                        },
                        "color": "#B3261E",
                        "fontWeight": "600",
                    },
                ],
                style_filter={
                    "backgroundColor": "#E8F1F2",
                    "color": "#1B2B30",
                    "fontSize": "11px",
                },
                style_cell={
                    "fontFamily": "Segoe UI, Arial, sans-serif",
                    "textAlign": "left",
                    "minWidth": "90px",
                    "maxWidth": "240px",
                    "whiteSpace": "normal",
                    "height": "auto",
                },
            ),
        ],
    )

    incidentes_content = html.Div(
        className="tab-content",
        children=[
            upload_section,
            html.Div(id="analysis-meta", className="analysis-meta"),
            filters,
            kpis,
            primary_grid,
            extra_section,
            download_row,
            table_section,
        ],
    )

    cambios_content = html.Div(
        className="empty-module",
        children=[
            html.Div("Cambios", className="empty-module-title"),
            html.P(
                "Módulo pendiente. La prueba de concepto se enfoca en el reporte "
                "de incidentes."
            ),
            html.Span("Sin dataset de cambios", className="empty-badge"),
        ],
    )

    tabs = dcc.Tabs(
        id="area-tabs",
        value="incidentes",
        parent_className="area-tabs",
        className="area-tabs-list",
        content_className="area-tabs-content",
        children=[
            dcc.Tab(
                label="Incidentes",
                value="incidentes",
                className="area-tab",
                selected_className="area-tab--selected",
                children=incidentes_content,
            ),
            dcc.Tab(
                label="Cambios",
                value="cambios",
                className="area-tab",
                selected_className="area-tab--selected",
                children=cambios_content,
            ),
        ],
    )

    overlay = html.Div(
        id="loading-overlay",
        className="overlay",
        style={"display": "none"},
        children=[
            html.Div(
                className="overlay-modal",
                children=[
                    html.Div(className="spinner"),
                    html.P("Realizando análisis"),
                ],
            )
        ],
    )

    return html.Div(
        className="app-shell",
        children=[
            dcc.Store(id="dataset-store", storage_type="session"),
            dcc.Interval(
                id="poll-interval",
                interval=config.POLL_INTERVAL_MS,
                n_intervals=0,
                disabled=True,
            ),
            header,
            html.Div(
                className="page",
                children=[description, tabs],
            ),
            html.Div(
                className="app-footer",
                children=[html.Span("Reporte de Incidentes")],
            ),
            overlay,
        ],
    )


def _safe_filename(filename: str) -> str:
    name = os.path.basename(str(filename or "reporte.csv"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return (name[:120] or "reporte.csv")


def _get_folder():
    import dataiku

    return dataiku.Folder(config.UPLOAD_FOLDER_ID)


def _get_project():
    import dataiku

    return dataiku.api_client().get_default_project()


def _upload_and_start(contents: str, filename: str) -> Tuple[Dict[str, Any], str]:
    raw = base64.b64decode(contents.split(",", 1)[1])
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise ValueError(
            "El archivo excede el límite de {} MB.".format(
                config.MAX_UPLOAD_BYTES // (1024 * 1024)
            )
        )
    safe_name = _safe_filename(filename)
    upload_id = uuid.uuid4().hex
    upload_path = "{}/{}/{}".format(config.UPLOAD_PREFIX, upload_id, safe_name)
    folder = _get_folder()
    folder.upload_stream(upload_path, io.BytesIO(raw))
    pointer = {
        "path": upload_path,
        "original_name": filename,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": upload_id,
    }
    folder.upload_stream(
        config.LATEST_POINTER,
        io.BytesIO(json.dumps(pointer, ensure_ascii=False).encode("utf-8")),
    )

    project = _get_project()
    scenario = project.get_scenario(config.SCENARIO_ID)
    trigger = scenario.run()
    run = trigger.wait_for_scenario_run(no_fail=True)
    if run is None:
        raise RuntimeError(
            "El escenario no pudo iniciarse. Verifica que no haya otro run activo."
        )
    return pointer, run.id


def _read_status() -> Optional[Dict[str, Any]]:
    import dataiku

    frame = dataiku.Dataset(config.STATUS_DATASET).get_dataframe()
    if frame.empty:
        return None
    row = frame.iloc[-1].to_dict()
    return {
        key: (None if pd.isna(value) else value)
        for key, value in row.items()
    }


def _normalize_output(frame: pd.DataFrame) -> pd.DataFrame:
    if "opened_at" in frame.columns:
        frame["opened_at"] = pd.to_datetime(frame["opened_at"], errors="coerce")
    if "resolved_at" in frame.columns:
        frame["resolved_at"] = pd.to_datetime(frame["resolved_at"], errors="coerce")
    if "mttr_hours" in frame.columns:
        frame["mttr_hours"] = pd.to_numeric(frame["mttr_hours"], errors="coerce")
    elif "resolved_at" in frame.columns:
        frame["mttr_hours"] = (
            (frame["resolved_at"] - frame["opened_at"]).dt.total_seconds() / 3600.0
        )
    if "opened_date" in frame.columns:
        frame["opened_date"] = pd.to_datetime(frame["opened_date"], errors="coerce")
    elif "opened_at" in frame.columns:
        frame["opened_date"] = frame["opened_at"].dt.normalize()
    if "hour" not in frame.columns and "opened_at" in frame.columns:
        frame["hour"] = frame["opened_at"].dt.hour
    if "weekday_num" not in frame.columns and "opened_at" in frame.columns:
        frame["weekday_num"] = frame["opened_at"].dt.dayofweek
    if "reassignment_count" in frame.columns:
        frame["reassignment_count"] = pd.to_numeric(
            frame["reassignment_count"], errors="coerce"
        )
    return frame


def _read_output() -> pd.DataFrame:
    import dataiku

    frame = dataiku.Dataset(config.OUTPUT_DATASET).get_dataframe()
    if frame.empty:
        raise ValueError("El dataset de salida está vacío.")
    return _normalize_output(frame)


def _cache_set(run_id: str, frame: pd.DataFrame) -> None:
    CACHE["run_id"] = run_id
    CACHE["frame"] = frame


def _cache_get(run_id: Optional[str]) -> Optional[pd.DataFrame]:
    if run_id and CACHE.get("run_id") == run_id:
        return CACHE.get("frame")
    return None


def _load_cached_or_dataset(store: Dict[str, Any]) -> pd.DataFrame:
    frame = _cache_get(store.get("scenario_run_id"))
    if frame is not None:
        return frame
    frame = _read_output()
    _cache_set(store.get("scenario_run_id"), frame)
    return frame


def _render(
    frame: pd.DataFrame,
    source: str,
    start_date: Optional[str],
    end_date: Optional[str],
    priorities: Optional[List[str]],
    apps: Optional[List[str]],
    groups: Optional[List[str]],
    exclude_peak: bool,
) -> List[Any]:
    filtered = parsing.apply_filters(
        frame, start_date, end_date, priorities, apps, groups
    )
    if filtered.empty:
        empty = [
            charts.empty_figure("Sin datos para los filtros seleccionados")
            for _ in range(10)
        ]
        return [
            "0",
            "—",
            "—",
            "—",
            "0",
            *empty,
            [],
            "0 de {:,} registros".format(len(frame)),
            "Sin resultados para los filtros seleccionados",
            True,
            False,
        ]

    kpis = parsing.make_kpis(filtered)
    figures = [
        charts.daily_figure(filtered, exclude_peak=exclude_peak),
        charts.priority_figure(filtered),
        charts.contact_figure(filtered),
        charts.apps_figure(filtered),
        charts.mttr_distribution_figure(filtered),
        charts.mttr_by_group_figure(filtered),
        charts.resolution_figure(filtered),
        charts.tsa_figure(filtered),
        charts.reassignments_figure(filtered),
        charts.heatmap_figure(filtered),
    ]
    return [
        _fmt_int(kpis["total"]),
        _fmt_hours(kpis["mttr_median"]),
        _fmt_hours(kpis["mttr_p90"]),
        _fmt_decimal(kpis["reassign_avg"]),
        _fmt_int(kpis["groups"]),
        *figures,
        parsing.table_records(filtered),
        "{:,} de {:,} registros".format(len(filtered), len(frame)),
        parsing.analysis_meta(filtered, source),
        False,
        False,
    ]


def _no_updates(count: int) -> List[Any]:
    return [no_update] * count


def _error_outputs(message: str, store: Optional[Dict[str, Any]] = None) -> List[Any]:
    new_store = dict(store or {})
    new_store["status"] = "error"
    new_store["message"] = message
    dash_outputs = _no_updates(DASH_OUTPUT_COUNT)
    dash_outputs[18] = True
    dash_outputs[19] = True
    return [
        new_store,
        {"display": "none"},
        True,
        message,
    ] + dash_outputs


def _running_outputs() -> List[Any]:
    return [
        no_update,
        {"display": "flex"},
        False,
        "",
    ] + _no_updates(DASH_OUTPUT_COUNT)


def _triggered_id() -> Optional[str]:
    if not callback_context.triggered:
        return None
    return callback_context.triggered[0]["prop_id"].split(".")[0]


def register_callbacks(dash_app) -> None:
    @dash_app.callback(
        Output("upload-filename", "children"),
        Output("btn-analyze", "disabled"),
        Input("upload-data", "filename"),
        prevent_initial_call=True,
    )
    def show_selected_file(filename):
        if not filename:
            return "Ningún archivo seleccionado", True
        valid = filename.lower().endswith((".csv", ".xlsx"))
        if not valid:
            return "{} · formato no soportado; usa CSV o XLSX".format(filename), True
        return filename, False

    @dash_app.callback(
        Output("dataset-store", "data"),
        Output("loading-overlay", "style"),
        Output("poll-interval", "disabled"),
        Output("analysis-error", "children"),
        Output("kpi-total", "children"),
        Output("kpi-mttr-median", "children"),
        Output("kpi-mttr-p90", "children"),
        Output("kpi-reassign", "children"),
        Output("kpi-groups", "children"),
        Output("chart-daily", "figure"),
        Output("chart-priority", "figure"),
        Output("chart-contact", "figure"),
        Output("chart-apps", "figure"),
        Output("chart-mttr-dist", "figure"),
        Output("chart-mttr-group", "figure"),
        Output("chart-resolution", "figure"),
        Output("chart-tsa", "figure"),
        Output("chart-reassign", "figure"),
        Output("chart-heatmap", "figure"),
        Output("analysis-table", "data"),
        Output("table-meta", "children"),
        Output("analysis-meta", "children"),
        Output("btn-download", "disabled"),
        Output("btn-clear-filters", "disabled"),
        Input("btn-analyze", "n_clicks"),
        Input("poll-interval", "n_intervals"),
        Input("filter-dates", "start_date"),
        Input("filter-dates", "end_date"),
        Input("filter-priority", "value"),
        Input("filter-app", "value"),
        Input("filter-group", "value"),
        Input("toggle-peak", "value"),
        State("upload-data", "contents"),
        State("upload-data", "filename"),
        State("dataset-store", "data"),
        prevent_initial_call=True,
    )
    def main_callback(
        n_clicks,
        n_intervals,
        start_date,
        end_date,
        priorities,
        apps,
        groups,
        peak,
        contents,
        filename,
        store,
    ):
        triggered = _triggered_id()
        store = store or {}

        if triggered == "btn-analyze":
            if store.get("status") == "running":
                return _no_updates(MAIN_OUTPUT_COUNT)
            if not contents or not filename:
                return _error_outputs(
                    "Selecciona un archivo CSV o XLSX antes de continuar.", store
                )
            try:
                pointer, run_id = _upload_and_start(contents, filename)
            except Exception as exc:
                return _error_outputs(
                    "No se pudo iniciar el procesamiento: {}".format(exc), store
                )
            new_store = {
                "status": "running",
                "scenario_run_id": run_id,
                "file_name": pointer.get("original_name"),
                "upload_path": pointer.get("path"),
                "started_at_epoch": time.time(),
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
            dash_outputs = _no_updates(DASH_OUTPUT_COUNT)
            dash_outputs[18] = True
            dash_outputs[19] = True
            return [
                new_store,
                {"display": "flex"},
                False,
                "",
            ] + dash_outputs

        if triggered == "poll-interval":
            if store.get("status") != "running":
                return _no_updates(MAIN_OUTPUT_COUNT)
            started = store.get("started_at_epoch") or 0
            if time.time() - started > config.SCENARIO_TIMEOUT_SECONDS:
                return _error_outputs(
                    "El procesamiento excedió el tiempo máximo de espera. "
                    "Revisa el escenario en Dataiku.",
                    store,
                )
            try:
                project = _get_project()
                scenario = project.get_scenario(config.SCENARIO_ID)
                run = scenario.get_run(store.get("scenario_run_id"))
                run.refresh()
            except Exception as exc:
                return _error_outputs(
                    "No se pudo consultar el escenario: {}".format(exc), store
                )
            if run.running:
                return _running_outputs()
            try:
                outcome = run.outcome
                status = _read_status() or {}
                status_code = str(status.get("status") or "").upper()
                if status_code == "FAILED" or outcome not in ("SUCCESS", "WARNING"):
                    message = status.get("message") or (
                        "El procesamiento terminó con estado {}.".format(outcome)
                    )
                    return _error_outputs(message, store)
                frame = _read_output()
                _cache_set(store.get("scenario_run_id"), frame)
                new_store = dict(store)
                new_store["status"] = "done"
                new_store["finished_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )
                dash_outputs = _render(
                    frame,
                    new_store.get("file_name", ""),
                    start_date,
                    end_date,
                    priorities,
                    apps,
                    groups,
                    bool(peak),
                )
                return [
                    new_store,
                    {"display": "none"},
                    True,
                    "",
                ] + dash_outputs
            except Exception as exc:
                return _error_outputs(
                    "No se pudo cargar el dataset procesado: {}".format(exc),
                    store,
                )

        if store.get("status") != "done":
            return _no_updates(MAIN_OUTPUT_COUNT)

        try:
            frame = _load_cached_or_dataset(store)
        except Exception as exc:
            return _error_outputs(
                "No se pudo leer el dataset procesado: {}".format(exc), store
            )

        dash_outputs = _render(
            frame,
            store.get("file_name", ""),
            start_date,
            end_date,
            priorities,
            apps,
            groups,
            bool(peak),
        )
        return _no_updates(4) + dash_outputs

    @dash_app.callback(
        Output("filter-dates", "start_date"),
        Output("filter-dates", "end_date"),
        Output("filter-priority", "value"),
        Output("filter-app", "value"),
        Output("filter-group", "value"),
        Output("toggle-peak", "value"),
        Input("btn-clear-filters", "n_clicks"),
        State("dataset-store", "data"),
        prevent_initial_call=True,
    )
    def clear_filters(n_clicks, store):
        if not n_clicks or not store or store.get("status") != "done":
            raise PreventUpdate
        return None, None, None, None, None, []

    @dash_app.callback(
        Output("download-analysis", "data"),
        Input("btn-download", "n_clicks"),
        State("dataset-store", "data"),
        State("filter-dates", "start_date"),
        State("filter-dates", "end_date"),
        State("filter-priority", "value"),
        State("filter-app", "value"),
        State("filter-group", "value"),
        prevent_initial_call=True,
    )
    def download_analysis(
        n_clicks, store, start_date, end_date, priorities, apps, groups
    ):
        if not n_clicks or not store or store.get("status") != "done":
            raise PreventUpdate
        try:
            frame = _load_cached_or_dataset(store)
        except Exception:
            raise PreventUpdate
        filtered = parsing.apply_filters(
            frame, start_date, end_date, priorities, apps, groups
        )
        if filtered.empty:
            raise PreventUpdate
        metadata = {
            "source": store.get("file_name", "reporte"),
            "min_date": filtered["opened_at"].min().date().isoformat(),
            "max_date": filtered["opened_at"].max().date().isoformat(),
            "rows": int(len(filtered)),
        }
        buffer = parsing.export_excel(filtered, metadata)
        filename = "analisis_incidentes_{}.xlsx".format(
            datetime.now().strftime("%Y%m%d_%H%M")
        )
        if hasattr(dcc, "send_bytes"):
            return dcc.send_bytes(buffer.getvalue(), filename)
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        with open(path, "wb") as stream:
            stream.write(buffer.getvalue())
        try:
            return dcc.send_file(path, filename=filename)
        except TypeError:
            return dcc.send_file(path)


def configure(dash_app) -> None:
    dash_app.index_string = INDEX_TEMPLATE.replace(
        "__APP_CSS__", styles.APP_CSS
    )
    try:
        dash_app.title = config.APP_TITLE
    except Exception:
        pass
    dash_app.layout = build_layout()
    register_callbacks(dash_app)


# Dataiku ejecuta este archivo con un objeto global `app` ya creado.
# No se debe ejecutar app.run() ni instanciar Dash() aqui.
if "app" in globals():
    configure(app)
