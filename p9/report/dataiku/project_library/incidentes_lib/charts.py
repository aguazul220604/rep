# -*- coding: utf-8 -*-
"""Figuras Plotly del dashboard de incidentes."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from .parsing import PRIORITY_ORDER, WEEKDAYS_ES

TEAL_DARK = "#003B49"
TEAL = "#005A6E"
RED = "#E1251B"
AMBER = "#F2A900"
MINT = "#4FB3A9"
TEXT = "#E8F1F2"
MUTED = "#9BB8BD"
GRID = "rgba(255,255,255,0.10)"

COLORWAY = [RED, AMBER, MINT, "#7FA8FF", "#FF8A65", "#C77DFF", "#A5D6A7"]

PRIORITY_COLORS = {
    "1 - Critical": "#8E1B16",
    "2 - High": RED,
    "3 - Moderate": AMBER,
    "4 - Low": MINT,
    "No especificada": "#6F8F95",
}

APP_LABELS = {
    "Enterprise Entitlement Management System": "Enterprise Entitlement",
    "Remote Access - Citi VPN with Cisco AnyConnect": "Citi VPN / AnyConnect",
    "Citi AML Case Investigation Platform - Mexico": "AML Case Investigation",
    "CTS LATAM VMware Infrastructure": "VMware Infrastructure",
    "Microsoft Office Pro Plus": "Microsoft Office Pro Plus",
    "Zoom Cloud Video Meetings": "Zoom Cloud Meetings",
    "Global Sanctions Risk Management": "Sanctions Risk Mgmt",
    "CitiScreening Customer Screening - NAM": "CitiScreening - NAM",
}

GROUP_LABELS = {
    "GFT GLBL PA INCIDENT CITISCREENING L2 HORIZONTAL SUPPORT": "CitiScreening L2",
    "CTI LATAM 1T MX SVCDESK SUPPORT": "MX Service Desk",
    "GFT LATAM PA MX AML MONITORING SUPPORT": "AML Monitoring",
    "CTI GLBL CYBER SECURITY L2 SUPPORT": "Cyber Security L2",
    "CTI GBL VAHD SUPPORT": "VAHD Support",
    "GFT GBL PA NCS L2 SUPPORT": "NCS L2",
    "CTI GBL CIS VIRTUALIZATION OPERATIONS SUPPORT": "Virtualization Ops",
}

GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}

MTTR_BINS = [-0.001, 1, 4, 8, 24, 48, 144, float("inf")]
MTTR_LABELS = [
    "< 1 h",
    "1–4 h",
    "4–8 h",
    "8–24 h",
    "24–48 h",
    "48–144 h",
    "> 144 h",
]


def _short_label(value: str, mapping: dict[str, str], limit: int = 30) -> str:
    text = mapping.get(value, value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _style(
    figure: go.Figure,
    cartesian: bool = True,
    showlegend: bool = False,
    legend_top: bool = False,
) -> go.Figure:
    figure.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Segoe UI, Arial, sans-serif", color=TEXT, size=11),
        margin=dict(l=10, r=12, t=10, b=10),
        showlegend=showlegend,
        legend=dict(
            orientation="h",
            yanchor="bottom" if not legend_top else "top",
            y=-0.18 if not legend_top else 1.02,
            x=0,
            font=dict(color=MUTED, size=10),
            bgcolor="rgba(0,0,0,0)",
        ),
        hoverlabel=dict(
            bgcolor="#002630",
            bordercolor="rgba(255,255,255,0.25)",
            font=dict(color="#FFFFFF", size=11),
        ),
        colorway=COLORWAY,
    )
    if cartesian:
        figure.update_xaxes(
            showgrid=False,
            zeroline=False,
            linecolor=GRID,
            tickfont=dict(color=MUTED, size=10),
            title_font=dict(color=MUTED, size=10),
            automargin=True,
        )
        figure.update_yaxes(
            showgrid=True,
            gridcolor=GRID,
            zeroline=False,
            linecolor=GRID,
            tickfont=dict(color=MUTED, size=10),
            title_font=dict(color=MUTED, size=10),
            automargin=True,
        )
    return figure


def empty_figure(message: str = "Sin datos para visualizar") -> go.Figure:
    figure = go.Figure()
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font=dict(color="#8FAEB4", size=13),
    )
    _style(figure, cartesian=False)
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    return figure


def daily_figure(frame: pd.DataFrame, exclude_peak: bool = False) -> go.Figure:
    if frame.empty:
        return empty_figure()
    daily = (
        frame.groupby("opened_date", as_index=False)
        .size()
        .rename(columns={"size": "incidentes"})
        .sort_values("opened_date")
    )
    if daily.empty:
        return empty_figure()
    if exclude_peak and len(daily) > 3:
        threshold = daily["incidentes"].median() * 10
        candidate = daily.loc[daily["incidentes"] > threshold]
        if not candidate.empty and len(daily) - len(candidate) >= 3:
            daily = daily.loc[daily["incidentes"] <= threshold]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=daily["opened_date"],
            y=daily["incidentes"],
            mode="lines+markers",
            line=dict(color=RED, width=2.5, shape="linear"),
            marker=dict(size=5, color=RED, line=dict(color=TEAL_DARK, width=1)),
            fill="tozeroy",
            fillcolor="rgba(225,37,27,0.16)",
            hovertemplate="%{x|%d/%m/%Y}<br><b>%{y:,}</b> incidentes<extra></extra>",
            name="Incidentes",
        )
    )
    figure.update_layout(showlegend=False)
    figure.update_xaxes(tickformat="%d/%m", nticks=10)
    figure.update_yaxes(title_text="Incidentes", rangemode="tozero")
    return _style(figure)


def priority_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    counts = frame["priority"].value_counts()
    ordered = [value for value in PRIORITY_ORDER if value in counts.index]
    ordered += [value for value in counts.index if value not in ordered]
    if not ordered:
        return empty_figure()
    values = [int(counts[value]) for value in ordered]
    figure = go.Figure(
        go.Pie(
            labels=ordered,
            values=values,
            hole=0.60,
            sort=False,
            marker=dict(
                colors=[PRIORITY_COLORS.get(value, "#6F8F95") for value in ordered],
                line=dict(color=TEAL_DARK, width=2),
            ),
            textinfo="percent",
            textfont=dict(color="#FFFFFF", size=11),
            hovertemplate="%{label}<br><b>%{value:,}</b> incidentes<br>%{percent}<extra></extra>",
        )
    )
    total = sum(values)
    figure.add_annotation(
        text=f"<b>{total:,}</b><br><span style='font-size:10px'>incidentes</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(color=TEXT, size=15),
    )
    return _style(figure, cartesian=False, showlegend=True)


def contact_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    counts = frame["contact_type"].value_counts()
    if counts.empty:
        return empty_figure()
    labels = [str(value) for value in counts.index]
    values = [int(value) for value in counts.values]
    colors = [COLORWAY[index % len(COLORWAY)] for index in range(len(labels))]
    figure = go.Figure(
        go.Pie(
            labels=labels,
            values=values,
            hole=0.60,
            sort=False,
            marker=dict(colors=colors, line=dict(color=TEAL_DARK, width=2)),
            textinfo="percent",
            textfont=dict(color="#FFFFFF", size=11),
            hovertemplate="%{label}<br><b>%{value:,}</b> incidentes<br>%{percent}<extra></extra>",
        )
    )
    total = sum(values)
    figure.add_annotation(
        text=f"<b>{total:,}</b><br><span style='font-size:10px'>incidentes</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(color=TEXT, size=15),
    )
    return _style(figure, cartesian=False, showlegend=True)


def apps_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    grouped = (
        frame.groupby("app")
        .agg(
            incidentes=("number", "count"),
            mttr_mediana=("mttr_hours", "median"),
        )
        .reset_index()
        .sort_values("incidentes", ascending=True)
        .tail(8)
    )
    if grouped.empty:
        return empty_figure()
    grouped["label"] = grouped["app"].map(
        lambda value: _short_label(str(value), APP_LABELS, limit=26)
    )
    grouped["mttr_mediana"] = grouped["mttr_mediana"].fillna(0)
    figure = go.Figure(
        go.Bar(
            x=grouped["incidentes"],
            y=grouped["label"],
            orientation="h",
            marker=dict(
                color=grouped["mttr_mediana"],
                colorscale=[
                    [0.0, MINT],
                    [0.5, AMBER],
                    [1.0, RED],
                ],
                line=dict(color=TEAL_DARK, width=1),
                colorbar=dict(
                    title=dict(text="MTTR<br>mediana (h)", font=dict(color=MUTED, size=9)),
                    thickness=8,
                    len=0.65,
                    tickfont=dict(color=MUTED, size=9),
                    outlinewidth=0,
                ),
            ),
            text=grouped["incidentes"],
            textposition="outside",
            cliponaxis=False,
            customdata=list(
                zip(grouped["app"], grouped["mttr_mediana"].round(2))
            ),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "%{x:,} incidentes<br>MTTR mediana: %{customdata[1]:.2f} h"
                "<extra></extra>"
            ),
        )
    )
    figure.update_xaxes(title_text="Incidentes")
    figure.update_yaxes(showgrid=False, tickfont=dict(color=TEXT, size=10))
    return _style(figure)


def mttr_distribution_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    buckets = pd.cut(
        frame["mttr_hours"], bins=MTTR_BINS, labels=MTTR_LABELS, right=False
    )
    counts = buckets.value_counts().reindex(MTTR_LABELS, fill_value=0)
    if int(counts.sum()) == 0:
        return empty_figure("Sin tiempos de resolución disponibles")
    total = int(counts.sum())
    colors = [MINT, "#78C6B8", AMBER, "#F4C24A", "#F08A5D", RED, "#A31612"]
    text = [
        f"{int(value):,} · {value / total * 100:.1f}%"
        for value in counts.values
    ]
    figure = go.Figure(
        go.Bar(
            x=MTTR_LABELS,
            y=counts.values,
            marker=dict(color=colors, line=dict(color=TEAL_DARK, width=1)),
            text=text,
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{x}<br><b>%{y:,}</b> incidentes<extra></extra>",
        )
    )
    figure.update_yaxes(title_text="Incidentes")
    figure.update_xaxes(tickfont=dict(color=MUTED, size=9))
    return _style(figure)


def mttr_by_group_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    grouped = (
        frame.groupby("assignment_group")["mttr_hours"]
        .agg(["median", "count"])
        .dropna()
        .sort_values("median", ascending=True)
    )
    if grouped.empty:
        return empty_figure("Sin tiempos de resolución disponibles")
    labels = [
        _short_label(str(value), GROUP_LABELS, limit=24)
        for value in grouped.index
    ]
    figure = go.Figure()
    palette = [MINT, "#60B7AA", "#7FC8BD", AMBER, "#F3B33D", "#EE7A52", RED]
    for index, (group, row) in enumerate(grouped.iterrows()):
        values = frame.loc[frame["assignment_group"] == group, "mttr_hours"].dropna()
        figure.add_trace(
            go.Box(
                x=values,
                y=[labels[index]] * len(values),
                orientation="h",
                name=labels[index],
                marker=dict(color=palette[index % len(palette)]),
                line=dict(width=1.2),
                fillcolor="rgba(79,179,169,0.12)",
                boxpoints=False,
                showlegend=False,
                hovertemplate=(
                    f"<b>{labels[index]}</b><br>"
                    "MTTR: %{x:.2f} h<extra></extra>"
                ),
            )
        )
    figure.update_xaxes(
        type="log",
        title_text="Horas (escala log)",
        showgrid=True,
        gridcolor=GRID,
    )
    figure.update_yaxes(showgrid=False, tickfont=dict(color=TEXT, size=9))
    return _style(figure)


def resolution_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    counts = frame["resolution_category"].value_counts()
    if counts.empty:
        return empty_figure()
    ordered = counts.sort_values(ascending=True)
    labels = [str(value) for value in ordered.index]
    values = [int(value) for value in ordered.values]
    colors = [
        AMBER if label == "Sin categoría" else MINT for label in labels
    ]
    figure = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker=dict(color=colors, line=dict(color=TEAL_DARK, width=1)),
            text=values,
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}<br><b>%{x:,}</b> incidentes<extra></extra>",
        )
    )
    figure.update_xaxes(title_text="Incidentes")
    figure.update_yaxes(showgrid=False, tickfont=dict(color=TEXT, size=10))
    return _style(figure)


def tsa_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    counts = frame["tsa"].value_counts()
    if counts.empty:
        return empty_figure()
    labels = [str(value) for value in counts.index]
    values = [int(value) for value in counts.values]
    figure = go.Figure(
        go.Treemap(
            labels=labels,
            parents=[""] * len(labels),
            values=values,
            branchvalues="total",
            textinfo="label+percent root",
            textfont=dict(color="#FFFFFF", size=12),
            marker=dict(
                colors=values,
                colorscale=[
                    [0.0, "#0E5A66"],
                    [0.45, MINT],
                    [1.0, RED],
                ],
                line=dict(color=TEAL_DARK, width=2),
                showscale=False,
            ),
            hovertemplate="<b>%{label}</b><br>%{value:,} incidentes<br>%{percentRoot} del total<extra></extra>",
        )
    )
    return _style(figure, cartesian=False)


def reassignments_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    values = pd.to_numeric(frame["reassignment_count"], errors="coerce")
    counts = values.value_counts().sort_index()
    if counts.empty:
        return empty_figure("Sin datos de reasignaciones")
    labels = [f"{int(value)}" for value in counts.index]
    total = int(counts.sum())
    text = [
        f"{int(value):,} · {value / total * 100:.1f}%"
        for value in counts.values
    ]
    colors = [MINT, AMBER, RED, "#A31612", "#6F8F95"]
    figure = go.Figure(
        go.Bar(
            x=labels,
            y=counts.values,
            marker=dict(
                color=[colors[index % len(colors)] for index in range(len(labels))],
                line=dict(color=TEAL_DARK, width=1),
            ),
            text=text,
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{x} reasignaciones<br><b>%{y:,}</b> incidentes<extra></extra>",
        )
    )
    figure.update_xaxes(title_text="Reasignaciones")
    figure.update_yaxes(title_text="Incidentes")
    return _style(figure)


def heatmap_figure(frame: pd.DataFrame) -> go.Figure:
    if frame.empty:
        return empty_figure()
    pivot = (
        frame.pivot_table(
            index="weekday_num",
            columns="hour",
            values="number",
            aggfunc="count",
            fill_value=0,
        )
        .reindex(range(7), fill_value=0)
        .reindex(columns=range(24), fill_value=0)
    )
    row_totals = pivot.sum(axis=1).replace(0, 1)
    normalized = pivot.div(row_totals, axis=0) * 100
    figure = go.Figure(
        go.Heatmap(
            z=normalized.values,
            x=[f"{hour:02d}" for hour in range(24)],
            y=WEEKDAYS_ES,
            colorscale=[
                [0.0, "#003B49"],
                [0.35, "#0E6B72"],
                [0.70, AMBER],
                [1.0, RED],
            ],
            colorbar=dict(
                title=dict(text="% del día", font=dict(color=MUTED, size=9)),
                thickness=8,
                len=0.7,
                tickfont=dict(color=MUTED, size=9),
                outlinewidth=0,
            ),
            hovertemplate="%{y} · %{x}:00<br><b>%{z:.1f}%</b> del día<extra></extra>",
        )
    )
    figure.update_xaxes(title_text="Hora de apertura", tickfont=dict(size=9))
    figure.update_yaxes(showgrid=False)
    return _style(figure)
