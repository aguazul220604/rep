# -*- coding: utf-8 -*-
"""
Dataiku Python recipe: sinteticos_incidentes_v1

Genera un dataset sintetico de incidentes con la estructura del Excel de
muestra (columnas A-AO). La recipe es autocontenida: no requiere dataset de
entrada y todos los catalogos estan embebidos.

Uso en Dataiku DSS
------------------
1. Crear un Python recipe sin input.
2. Definir el output dataset (por defecto: dataset_sintetico_incidentes).
3. Pegar este codigo y ejecutar.

Uso local (sin Dataiku)
-----------------------
    python recipe_sinteticos_incidentes_v1.py
Genera synthetic_incidents_preview.csv para revisar el resultado.
"""

import random
from datetime import datetime, timedelta

import pandas as pd

try:
    import dataiku
    HAS_DATAIKU = True
except ImportError:
    dataiku = None
    HAS_DATAIKU = False

# ======================= CONFIG =======================
OUTPUT_DATASET = "dataset_sintetico_incidentes"
LOCAL_PREVIEW_PATH = "synthetic_incidents_preview.csv"
N_ROWS = 10000
SEED = 42

# Fecha de referencia para "Añejamiento". Con 2026-10-25 se reproducen los
# 693 dias de los incidentes del 1-dic-2024 y los 692 del 2-dic-2024.
REFERENCE_DATE = datetime(2026, 10, 25)

# Ventana de fechas de la muestra.
WINDOW_START = datetime(2024, 12, 1)
WINDOW_END = datetime(2024, 12, 14, 23, 59, 59)

# 1.0 = todos los incidentes cerrados, como en la muestra.
CLOSED_RATIO = 1.0

# False = u_business_application y cmdb_ci se muestrean de forma independiente
# (la muestra original no tiene pares perfectamente consistentes).
STRICT_APP_CI_PAIRS = False

# Si Faker no esta instalado en el entorno, se usan listas estaticas.
USE_FAKER = True
# ======================================================

# ======================= CATALOGOS ====================
BUSINESS_APPS = [
    "Enterprise Entitlement Management System",
    "Remote Access - Citi VPN with Cisco AnyConnect",
    "Citi AML Case Investigation Platform - Mexico",
    "CTS LATAM VMware Infrastructure",
    "Microsoft Office Pro Plus",
    "Zoom Cloud Video Meetings",
    "Global Sanctions Risk Management",
    "CitiScreening Customer Screening - NAM",
]

CMDB_CIS = [
    "ZOOM CLOUD VIDEO MEETINGS-NAM-PROD-1",
    "REMOTE ACCESS - CITI VPN WITH CISCO ANYCONNECT-LATAM-PROD-1",
    "MICROSOFT OFFICE PRO PLUS-NAM-PROD-1",
    "ENTERPRISE ENTITLEMENT MANAGEMENT SYSTEM-NAM-PROD-CTO",
    "CITI AML CASE INVESTIGATION PLATFORM - MEXICO-LATAM-MX-PROD-1",
    "GLOBAL SANCTIONS RISK MANAGEMENT-NAM-PROD-ANALYZE-CTO",
    "CITISCREENING CUSTOMER SCREENING - NAM-NAM-NA-PROD-1",
    "CTS LATAM VMWARE INFRASTRUCTURE-LATAM-MX-PROD-1",
]

APP_CI_PAIRS = [
    ("Enterprise Entitlement Management System",
     "ENTERPRISE ENTITLEMENT MANAGEMENT SYSTEM-NAM-PROD-CTO"),
    ("Remote Access - Citi VPN with Cisco AnyConnect",
     "REMOTE ACCESS - CITI VPN WITH CISCO ANYCONNECT-LATAM-PROD-1"),
    ("Citi AML Case Investigation Platform - Mexico",
     "CITI AML CASE INVESTIGATION PLATFORM - MEXICO-LATAM-MX-PROD-1"),
    ("CTS LATAM VMware Infrastructure",
     "CTS LATAM VMWARE INFRASTRUCTURE-LATAM-MX-PROD-1"),
    ("Microsoft Office Pro Plus",
     "MICROSOFT OFFICE PRO PLUS-NAM-PROD-1"),
    ("Zoom Cloud Video Meetings",
     "ZOOM CLOUD VIDEO MEETINGS-NAM-PROD-1"),
    ("Global Sanctions Risk Management",
     "GLOBAL SANCTIONS RISK MANAGEMENT-NAM-PROD-ANALYZE-CTO"),
    ("CitiScreening Customer Screening - NAM",
     "CITISCREENING CUSTOMER SCREENING - NAM-NAM-NA-PROD-1"),
]

LOCATIONS = [
    {"location": "SUC. PORTAL SAN ANGELEDIF.",
     "u_building": "54249", "city": "CIUDAD DE MEXICO",
     "state": "CIUDAD DE MEXICO", "country": "MEXICO"},
    {"location": "SUC. PORTAL SAN ANGELEDIF_BANAMEX",
     "u_building": "90195", "city": "CIUDAD DE MEXICO",
     "state": "CIUDAD DE MEXICO", "country": "MEXICO"},
    {"location": "EDIF. PICO DE VERAPAZ 331",
     "u_building": "19530", "city": "CIUDAD DE MEXICO",
     "state": "DISTRITO FEDERAL", "country": "MEXICO"},
    {"location": "COL. NARVARTE PONIENTE",
     "u_building": "56356", "city": "CIUDAD DE MEXICO",
     "state": "DISTRITO FEDERAL", "country": "MEXICO"},
    {"location": "SUC. REVOLUCION (REUB)",
     "u_building": "29397", "city": "PUEBLA",
     "state": "PUEBLA", "country": "MEXICO"},
]

DEPARTMENTS = [
    "1040 LF - Branch Support [L12]",
    "1040 LF - PBWM Head Office & Senior Management [L11]",
    "1040 LF - PBWM Head Office & Senior Management - Enterprise Risk [L11]",
    "14210 LF System Administrator Operation [L13]",
    "3020 LF - E&A Tech Strategy & Consulting [L11]",
    "1040 LF - Accounting Fraud [L13]",
    "1040 LF Core Anti Money Laundering [L10]",
    "72410 LF - Branches Office [L12]",
    "1040 LF - Leveraged Support Services International [L12]",
]

ASSIGNMENT_GROUPS = [
    "CTI LATAM 1T MX SVCDESK SUPPORT",
    "GFT GBL PA NCS L2 SUPPORT",
    "GFT GLBL PA INCIDENT CITISCREENING L2 HORIZONTAL SUPPORT",
    "CTI GBL CIS VIRTUALIZATION OPERATIONS SUPPORT",
    "CTI GBL VAHD SUPPORT",
    "CTI GLBL CYBER SECURITY L2 SUPPORT",
    "GFT LATAM PA MX AML MONITORING SUPPORT",
]

ORIGINATING_GROUPS = [
    "CTI LATAM 1T MX SVCDESK SUPPORT",
    "PBWM LATAM BNMX MX PRB VALIDATION SUPPORT",
    "CTI LATAM MX SERVER L3 SUPPORT",
    "CTI GBL VAHD SUPPORT",
    "CTI GLBL CYBER SECURITY L2 SUPPORT",
    "CISO GLBL GIAM OPS GPO MARKETPLACE SOLUTIONS SUPPORT",
]

SHORT_DESCRIPTIONS = [
    "no puedo entrar al sistema",
    "Microsoft excel is not working",
    "MCX outlook no abre",
    "servidor fallando",
    "La Vpn no funciona",
    "La aplicación no esta disponible",
    "MX - ZOOM- PROBLEMAS AL ACCEDER A ZOOM",
    "GPA - Revisión y monitoreo de procesos",
    "Zoom no funciona",
    "MX vpn problemas de conexion: error de conexion no se pudo resolver la direccion del servidor",
    "No se aabre la aplicación",
    "No puedo iniciar sesión en la aplicación",
    "Error al abrir Outlook",
    "VPN sin conexión",
    "Equipo lento",
    "Pantalla azul",
    "No se puede acceder a la red compartida",
    "Aplicación se cierra inesperadamente",
    "Problema con el token de acceso",
    "No recibe correos",
    "Impresora no responde",
    "Solicitud de acceso a carpeta",
    "Error 500 al ingresar",
    "Teams no carga",
    "Excel se queda cargando",
    "No se puede conectar al escritorio remoto",
    "Usuario bloqueado",
    "Restablecer contraseña",
    "Falla de disco",
    "Actualización de software",
    "Solicitud de nuevo equipo",
]

CLOSE_NOTES = [
    "<<TemplateVersion>> 1.7",
    "Usuario de acuerdo.",
    "Raised in error - Incident withdrawn",
    "working on change CHG0008559930",
    "User redirected to Live Agent support for further assistance.",
    "usuario desconecto sus monitores y pudo entrar sin problemas.",
    "Se apoya a localizar favoritos",
    "-FUNCIONA",
    "Se restablece contraseña y se valida acceso.",
    "Se reemplaza equipo y se entrega a usuario.",
    "Se documenta solución y se cierra incidente.",
]
CLOSE_NOTE_WEIGHTS = [
    0.30, 0.20, 0.10, 0.08, 0.08, 0.06,
    0.05, 0.05, 0.03, 0.03, 0.02,
]

COMMENTS = [
    "Se escala a segundo nivel para revisión.",
    "Usuario confirma que el servicio funciona.",
    "Se solicita evidencia adicional.",
    "Se aplica workaround temporal.",
    "Se coordina ventana de cambio.",
]

RESOLUTION_PAIRS = [
    ("Unlocked Account", "Access/Login Failure"),
    ("Replaced hardware", "Hardware/peripheral Issue"),
    ("Application", "Functionality Error"),
    ("Knowledge Provided", "Inquiry"),
    ("Configuration corrected", "Functionality Error"),
]

PRIORITIES = ["4 - Low", "3 - Moderate"]
PRIORITY_WEIGHTS = [0.75, 0.25]

CONTACT_TYPES = ["Phone", "Chat", "VAHD"]
CONTACT_TYPE_WEIGHTS = [0.70, 0.15, 0.15]

APP_CRITICALITY = ["3 - Medium", "2 - High"]
APP_CRITICALITY_WEIGHTS = [0.75, 0.25]

TSA_VALUES = [
    "TSA LATAM", "TSA NAM", "TSA GLOBAL",
    "TSA MX", "TSA NAM-MX", "TSA LATAM-MX",
]
TSA_WEIGHTS = [0.30, 0.20, 0.15, 0.15, 0.10, 0.10]

COUNTRY_WEIGHTS = [0.80, 0.15, 0.05]

REASSIGNMENT_WEIGHTS = [0.60, 0.25, 0.15]

FOLLOWUP_PEOPLE = [
    "Adrián", "Alejandro", "Ana", "Andrés", "Carlos", "Carmen",
    "Daniel", "Diego", "Eduardo", "Elena", "Fernando", "Gabriel",
    "Hugo", "Iván", "Javier", "Laura", "Lucía", "Luis",
    "Manuel", "María", "Óscar", "Pablo", "Patricia", "Raúl",
    "Sergio", "Sofía", "Tomás", "Víctor",
]

FIRST_NAMES = [
    "Adrián", "Alejandro", "Ana", "Andrés", "Carlos", "Carmen",
    "Daniel", "Diego", "Eduardo", "Elena", "Fernando", "Gabriel",
    "Hugo", "Iván", "Javier", "Laura", "Lucía", "Luis",
    "Manuel", "María", "Óscar", "Pablo", "Patricia", "Raúl",
    "Sergio", "Sofía", "Tomás", "Víctor",
]

LAST_NAMES = [
    "García", "Rodríguez", "Martínez", "Hernández", "López",
    "González", "Pérez", "Sánchez", "Ramírez", "Torres",
    "Flores", "Rivera", "Gómez", "Díaz", "Cruz",
    "Morales", "Ortiz", "Gutiérrez", "Chávez", "Ramos",
    "Ruiz", "Mendoza", "Vargas", "Castillo", "Jiménez",
    "Moreno", "Romero", "Herrera", "Medina", "Aguilar",
]

# Distribucion de dias desde el inicio de la ventana (0 = 1-dic-2024).
DAY_WEIGHTS = [
    0.55, 0.30, 0.05, 0.02, 0.02, 0.01, 0.01,
    0.01, 0.01, 0.01, 0.0025, 0.0025, 0.0025, 0.0025,
]

EXPECTED_COLUMNS = [
    "number",
    "short_description",
    "u_business_application",
    "cmdb_ci",
    "cmdb_ci.environment",
    "priority",
    "sys_created_on",
    "opened_at",
    "resolved_at",
    "assignment_group",
    "close_notes",
    "u_resolution_category",
    "state",
    "subcategory",
    "parent_incident",
    "location",
    "location.u_building",
    "location.city",
    "location.state",
    "contact_type",
    "location.zip",
    "caller_id.department",
    "caller_id.name",
    "assigned_to",
    "reassignment_count",
    "caller_id",
    "comments_and_work_notes",
    "problem_id",
    "u_originating_group",
    "sys_created_by",
    "u_impacted_environments",
    "major_incident_state",
    "u_fuji_incident",
    "cmdb_ci_u_external_id",
    "location.country",
    "cmdb_ci_u_country",
    "App_Criticality",
    "TSA Activo",
    "Persona Asignada para seguimiento",
    "Añejamiento",
    "Periodo de añejamiento",
]

# ======================= HELPERS ======================
def _weighted(rng, values, weights):
    return rng.choices(values, weights=weights, k=1)[0]


def _make_fake():
    if not USE_FAKER:
        return None
    try:
        from faker import Faker
        fake = Faker("es_MX")
        fake.seed_instance(SEED)
        return fake
    except Exception:
        return None


def _person_name(rng, fake):
    if fake is not None:
        return fake.name()
    return "{} {}".format(rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES))


def _employee_id(rng):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    prefix = rng.choice(letters) + rng.choice(letters)
    return "{}{:05d}".format(prefix, rng.randint(0, 99999))


def _random_created(rng):
    day = rng.choices(
        range(len(DAY_WEIGHTS)), weights=DAY_WEIGHTS, k=1
    )[0]
    seconds = rng.randint(7 * 3600, 23 * 3600 + 59 * 60)
    return WINDOW_START + timedelta(days=day, seconds=seconds)


def _random_resolved(rng, opened_at):
    r = rng.random()
    if r < 0.50:
        return opened_at + timedelta(minutes=rng.randint(5, 60))
    if r < 0.80:
        return opened_at + timedelta(minutes=rng.randint(61, 24 * 60))
    return opened_at + timedelta(
        days=rng.randint(1, 5), minutes=rng.randint(0, 24 * 60)
    )


def _age_bucket(days):
    if days > 90:
        return "More than 90 days"
    if days > 30:
        return "31-90 days"
    if days > 7:
        return "8-30 days"
    return "7 days or less"


# ======================= GENERACION ===================
def build_dataframe():
    rng = random.Random(SEED)
    fake = _make_fake()
    rows = []
    fuji_next = 142000000

    for i in range(N_ROWS):
        created = _random_created(rng)
        opened = created + timedelta(minutes=rng.randint(0, 5))
        state = "Closed" if rng.random() < CLOSED_RATIO else "In Progress"
        resolved = _random_resolved(rng, opened) if state == "Closed" else None

        if STRICT_APP_CI_PAIRS:
            app, ci = rng.choice(APP_CI_PAIRS)
        else:
            app = rng.choice(BUSINESS_APPS)
            ci = rng.choice(CMDB_CIS)

        loc = rng.choice(LOCATIONS)
        assignment = rng.choice(ASSIGNMENT_GROUPS)
        if rng.random() < 0.80:
            originating = assignment
        else:
            originating = rng.choice(ORIGINATING_GROUPS)

        if state == "Closed":
            close_note = _weighted(rng, CLOSE_NOTES, CLOSE_NOTE_WEIGHTS)
        else:
            close_note = None

        if rng.random() < 0.20:
            res_cat, subcat = None, None
        else:
            res_cat, subcat = rng.choice(RESOLUTION_PAIRS)

        caller_name = _person_name(rng, fake)
        assigned_to = _person_name(rng, fake)
        tries = 0
        while assigned_to == caller_name and tries < 5:
            assigned_to = _person_name(rng, fake)
            tries += 1

        if rng.random() < 0.45:
            u_fuji = "INC{:010d}".format(fuji_next)
            fuji_next += 1
        else:
            u_fuji = None

        age_days = (REFERENCE_DATE.date() - opened.date()).days

        rows.append({
            "number": "INC{:010d}".format(2762516 + i),
            "short_description": rng.choice(SHORT_DESCRIPTIONS),
            "u_business_application": app,
            "cmdb_ci": ci,
            "cmdb_ci.environment": "PROD",
            "priority": _weighted(rng, PRIORITIES, PRIORITY_WEIGHTS),
            "sys_created_on": created,
            "opened_at": opened,
            "resolved_at": resolved,
            "assignment_group": assignment,
            "close_notes": close_note,
            "u_resolution_category": res_cat,
            "state": state,
            "subcategory": subcat,
            "parent_incident": None,
            "location": loc["location"],
            "location.u_building": loc["u_building"],
            "location.city": loc["city"],
            "location.state": loc["state"],
            "contact_type": _weighted(
                rng, CONTACT_TYPES, CONTACT_TYPE_WEIGHTS
            ),
            "location.zip": None,
            "caller_id.department": rng.choice(DEPARTMENTS),
            "caller_id.name": caller_name,
            "assigned_to": assigned_to,
            "reassignment_count": _weighted(
                rng, [0, 1, 2], REASSIGNMENT_WEIGHTS
            ),
            "caller_id": caller_name,
            "comments_and_work_notes": (
                None if rng.random() < 0.90 else rng.choice(COMMENTS)
            ),
            "problem_id": None,
            "u_originating_group": originating,
            "sys_created_by": (
                "rest_user" if rng.random() < 0.45
                else _employee_id(rng)
            ),
            "u_impacted_environments": None,
            "major_incident_state": None,
            "u_fuji_incident": u_fuji,
            "cmdb_ci_u_external_id": rng.randint(150000, 180000),
            "location.country": loc["country"],
            "cmdb_ci_u_country": _weighted(
                rng, [None, "Mexico", "MEXICO"], COUNTRY_WEIGHTS
            ),
            "App_Criticality": _weighted(
                rng, APP_CRITICALITY, APP_CRITICALITY_WEIGHTS
            ),
            "TSA Activo": _weighted(rng, TSA_VALUES, TSA_WEIGHTS),
            "Persona Asignada para seguimiento": rng.choice(FOLLOWUP_PEOPLE),
            "Añejamiento": age_days,
            "Periodo de añejamiento": "{} {}".format(
                age_days, _age_bucket(age_days)
            ),
        })

    df = pd.DataFrame(rows, columns=EXPECTED_COLUMNS)

    for col in ("sys_created_on", "opened_at", "resolved_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce")

    for col in ("reassignment_count", "cmdb_ci_u_external_id", "Añejamiento"):
        df[col] = df[col].astype("int64")

    return df


# ======================= VALIDACION ===================
def validate(df):
    errors = []

    if list(df.columns) != EXPECTED_COLUMNS:
        errors.append(
            "Columnas incorrectas. Esperadas {}; obtenidas {}.".format(
                len(EXPECTED_COLUMNS), len(df.columns)
            )
        )
    if len(df) != N_ROWS:
        errors.append(
            "Filas incorrectas. Esperadas {}; obtenidas {}.".format(
                N_ROWS, len(df)
            )
        )

    if df["number"].duplicated().any():
        errors.append("La columna number contiene duplicados.")

    fuji = df["u_fuji_incident"].dropna()
    if fuji.duplicated().any():
        errors.append("La columna u_fuji_incident contiene duplicados.")

    if (df["resolved_at"].dropna() < df["opened_at"]).any():
        errors.append("Existen resolved_at anteriores a opened_at.")

    if (df["opened_at"] < df["sys_created_on"]).any():
        errors.append("Existen opened_at anteriores a sys_created_on.")

    if (
        (df["sys_created_on"] < WINDOW_START).any()
        or (df["sys_created_on"] > WINDOW_END).any()
    ):
        errors.append("sys_created_on fuera de la ventana configurada.")

    if (df["caller_id"].fillna("") != df["caller_id.name"].fillna("")).any():
        errors.append("caller_id no coincide con caller_id.name.")

    if (df["Añejamiento"].fillna(0) < 0).any():
        errors.append("La columna Añejamiento contiene valores negativos.")

    closed = df["state"] == "Closed"
    if df.loc[closed, "resolved_at"].isna().any():
        errors.append("Hay incidentes Closed sin resolved_at.")

    domains = {
        "state": {"Closed", "In Progress", "Resolved"},
        "priority": {"1 - Critical", "2 - High", "3 - Moderate", "4 - Low"},
        "contact_type": {"Phone", "Chat", "VAHD", "Email", "Web"},
        "cmdb_ci.environment": {"PROD", "DEV", "QA", "UAT"},
        "App_Criticality": {
            "1 - Critical", "2 - High", "3 - Medium", "4 - Low"
        },
    }
    for col, allowed in domains.items():
        bad = set(df[col].dropna().unique()) - allowed
        if bad:
            errors.append(
                "{} contiene valores no permitidos: {}".format(
                    col, sorted(bad)
                )
            )

    for col in df.select_dtypes(include=["object"]).columns:
        if df[col].astype(str).str.contains("#REF!", regex=False).any():
            errors.append("{} contiene #REF!.".format(col))

    if errors:
        raise ValueError("Validacion fallida:\n- " + "\n- ".join(errors))


# ======================= MAIN =========================
def main():
    df = build_dataframe()
    validate(df)

    print("Filas generadas: {}".format(len(df)))
    print("Columnas: {}".format(len(df.columns)))
    print("Nulos por columna:")
    print(df.isna().sum().to_string())
    print("Distribucion state:")
    print(df["state"].value_counts().to_string())
    print("Distribucion priority:")
    print(df["priority"].value_counts().to_string())
    print("Primeras filas:")
    print(df.head(10).to_string())

    if HAS_DATAIKU:
        dataiku.Dataset(OUTPUT_DATASET).write_with_schema(df)
        print("Dataset escrito en Dataiku: {}".format(OUTPUT_DATASET))
    else:
        df.to_csv(LOCAL_PREVIEW_PATH, index=False, encoding="utf-8-sig")
        print("dataiku no disponible. CSV local: {}".format(LOCAL_PREVIEW_PATH))


main()
