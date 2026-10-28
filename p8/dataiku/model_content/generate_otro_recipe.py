# -*- coding: utf-8 -*-
"""
Python recipe (Dataiku): genera la clase "otro" para el modelo de contenido.

Entrada : dataset de entrenamiento con columnas value, type
          (clases: nombre, apellido, direccion, empresa)
Salida  : mismo dataset + filas type="otro" (value, type)

"otro" = valores que NO son ninguna de las 4 clases difíciles. Incluye
valores de entidades que ya cubre el regex (teléfonos, RFC, correos...),
catálogos, montos, fechas, códigos, conceptos y texto libre.

El estilo (mayúsculas/minúsculas/título) y la proporción de acentos de "otro"
se igualan a los de las clases positivas para que el modelo no aprenda
atajos de formato en lugar de contenido.
"""
import hashlib
import random
import re
import string
import unicodedata
import uuid

import numpy as np
import pandas as pd
import dataiku
from faker import Faker

# ======================= CONFIG =======================
INPUT_DATASET = "training_dataset"            # <-- cambia al nombre de tu dataset de entrada
OUTPUT_DATASET = "training_dataset_with_otro"  # <-- cambia al nombre de tu dataset de salida
VALUE_COL, TYPE_COL = "value", "type"
OTRO_LABEL = "otro"

# Tamaño de "otro" = OTRO_RATIO * tamaño promedio de las otras clases
OTRO_RATIO = 2.0
SEED = 42
MAX_LEN = 120

LOCALES = ["es_MX", "es_ES", "en_US"]
LOCALE_WEIGHTS = [0.60, 0.15, 0.25]

# Opcional: columnas REALES que sepas con certeza que NO contienen
# nombre, apellido, dirección ni empresa. Hay que añadir esos datasets
# como inputs de la recipe. Ejemplo: [("catalogo_productos", "descripcion")]
REAL_NEGATIVE_SOURCES = []
REAL_MAX_PER_COLUMN = 500
REAL_READ_LIMIT = 100000
# ======================================================

random.seed(SEED)
np.random.seed(SEED)

fakers = {}
for i, loc in enumerate(LOCALES):
    f = Faker(loc)
    f.seed_instance(SEED + i)
    fakers[loc] = f
mx = Faker("es_MX")
mx.seed_instance(SEED + 99)


# ----------------------- helpers -----------------------
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s):
    s = strip_accents(str(s))
    return re.sub(r"\s+", " ", s).strip().casefold()


def has_accent(s):
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", str(s)))


def style_of(s):
    s = str(s)
    if not any(c.isalpha() for c in s):
        return None
    if s.isupper():
        return "upper"
    if s.islower():
        return "lower"
    if s == s.title():
        return "title"
    return "mixed"


def apply_style(s, style):
    if style == "upper":
        return s.upper()
    if style == "lower":
        return s.lower()
    if style == "title":
        return s.title()
    return s


def fk(method, *args, **kwargs):
    """Llama un método de Faker con un locale aleatorio (fallback a en_US)."""
    loc = random.choices(LOCALES, LOCALE_WEIGHTS)[0]
    f = fakers[loc]
    if not hasattr(f, method):
        f = fakers["en_US"]
    return getattr(f, method)(*args, **kwargs)


# ----------------------- generadores -----------------------
CATALOGO = [
    "ACTIVO", "INACTIVO", "PENDIENTE", "CANCELADO", "VIGENTE", "VENCIDO", "APROBADO",
    "RECHAZADO", "EN PROCESO", "FINALIZADO", "CARGO", "ABONO", "DEPOSITO", "RETIRO",
    "MXN", "USD", "EUR", "SUCURSAL", "APP MOVIL", "CAJERO AUTOMATICO", "BANCA EN LINEA",
    "MENSUAL", "QUINCENAL", "SEMANAL", "ANUAL", "SI", "NO", "S", "N", "TRUE", "FALSE",
    "0", "1", "Y", "NULL", "N/A", "NA", "SIN DATO", "NO APLICA", "PRODUCTO", "SERVICIO",
    "CORTE", "CARGA INICIAL", "CARGA INCREMENTAL", "PDF", "CSV", "XLSX", "ALTA", "BAJA",
    "MEDIA", "NORMAL", "ESPECIAL", "PARCIAL", "TOTAL", "NACIONAL", "INTERNACIONAL",
    "TARJETA DE DEBITO", "TARJETA DE CREDITO", "CUENTA DE AHORRO", "CUENTA CORRIENTE",
    "INVERSION A PLAZO", "SEGURO DE VIDA", "HIPOTECARIO", "AUTOMOTRIZ",
]

PRODUCTO_HEAD = [
    "Tarjeta", "Cuenta", "Seguro", "Credito", "Inversion", "Plan", "Paquete", "Servicio",
    "Fondo", "Cheque", "Prestamo", "Programa", "Membresia", "Cobertura",
]
PRODUCTO_MOD = [
    "Oro", "Platinum", "Premium", "Digital", "Plus", "Basica", "Clasica", "Total", "Flex",
    "Efectivo", "Empresarial", "Joven", "Select", "Express", "Integral", "Libre", "Nomina",
]

CONCEPTO_TEMPLATES = [
    "PAGO {w}", "PAGO {w} {n}", "COMPRA {w} {n}", "TRANSFERENCIA SPEI {n}", "COMISION {w}",
    "ABONO {w}", "RETIRO CAJERO {n}", "DEPOSITO {n}", "CARGO RECURRENTE {w}", "PAGO SERVICIO {w} {m}",
    "INTERES {w} {m}", "DEVOLUCION {n}", "ANUALIDAD {w}", "REF {n} {w}",
]


def g_catalogo():
    return random.choice(CATALOGO)


def g_producto():
    return "%s %s" % (random.choice(PRODUCTO_HEAD), random.choice(PRODUCTO_MOD))


def g_concepto():
    t = random.choice(CONCEPTO_TEMPLATES)
    return t.format(w=strip_accents(fk("word")), n=random.randint(1, 99999), m=random.randint(1, 12))


def g_numero():
    k = random.random()
    if k < 0.30:
        return str(random.randint(0, 10 ** random.randint(1, 9)))
    if k < 0.60:
        return "{:,.2f}".format(random.uniform(0, 10 ** random.randint(1, 7)))
    if k < 0.75:
        return "{:.2f}".format(random.uniform(0, 10 ** random.randint(1, 6)))
    if k < 0.85:
        return "{:.1f}%".format(random.uniform(0, 100))
    if k < 0.95:
        return "${:,.2f}".format(random.uniform(0, 10 ** random.randint(1, 7)))
    return "-{:.2f}".format(random.uniform(0, 10 ** random.randint(1, 6)))


def g_fecha():
    d = fk("date_time_between", start_date="-30y", end_date="now")
    fmt = random.choice([
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S", "%d/%m/%y", "%Y%m", "%H:%M:%S",
    ])
    return d.strftime(fmt)


def g_codigo():
    k = random.random()
    if k < 0.15:
        return str(uuid.UUID(int=random.getrandbits(128)))
    if k < 0.30:
        return hashlib.md5(str(random.random()).encode()).hexdigest()
    if k < 0.50:
        pat = random.choice(["??-####", "???###", "##??##??", "????-????-####", "SKU-######", "TX########"])
        return fk("bothify", text=pat, letters=string.ascii_uppercase)
    if k < 0.65:
        base = "_".join(strip_accents(fk("word")) for _ in range(random.randint(1, 3))).lower()
        return base + random.choice(["", "_v2", "_2024", "_tmp"])
    if k < 0.75:
        parts = [strip_accents(fk("word")).lower() for _ in range(random.randint(1, 3))]
        return "/" + "/".join(parts) + random.choice([".csv", ".parquet", ".txt"])
    if k < 0.85:
        return "v%d.%d.%d" % (random.randint(0, 9), random.randint(0, 20), random.randint(0, 99))
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=random.randint(6, 14)))


def g_regex_entities():
    """Valores de entidades que ya cubre la capa de regex (para el modelo son 'otro')."""
    opts = [
        lambda: fk("phone_number"),
        lambda: fk("email"),
        lambda: fk("ipv4"),
        lambda: fk("url"),
        lambda: fk("credit_card_number"),
        lambda: fk("iban"),
        lambda: fk("postcode"),
        lambda: str(fk("latitude")),
        lambda: "%s,%s" % (fk("latitude"), fk("longitude")),
        lambda: mx.curp(),
        lambda: mx.rfc(),
        lambda: "".join(random.choices(string.digits, k=18)),   # estilo CLABE
        lambda: "".join(random.choices(string.digits, k=11)),   # estilo NSS
        lambda: "".join(random.choices(string.digits, k=random.randint(10, 16))),
    ]
    return random.choice(opts)()


def g_texto():
    k = random.random()
    if k < 0.35:
        return fk("sentence", nb_words=random.randint(3, 10)).rstrip(".")
    if k < 0.55:
        return " ".join(fk("words", nb=random.randint(1, 4)))
    if k < 0.80:
        return fk("word")
    return fk("text", max_nb_chars=random.randint(40, 100)).replace("\n", " ")


# nombre: (peso, generador, aplicar_estilo_de_casing)
FAMILIES = {
    "catalogo": (0.08, g_catalogo, True),
    "producto": (0.08, g_producto, True),
    "concepto": (0.14, g_concepto, True),
    "numero":   (0.14, g_numero, False),
    "fecha":    (0.09, g_fecha, False),
    "codigo":   (0.14, g_codigo, False),
    "regex":    (0.15, g_regex_entities, False),
    "texto":    (0.18, g_texto, True),
}

# ----------------------- carga -----------------------
df_in = dataiku.Dataset(INPUT_DATASET).get_dataframe()
df_in = df_in[[VALUE_COL, TYPE_COL]].dropna()
df_in[VALUE_COL] = df_in[VALUE_COL].astype(str)

# idempotente: si ya había filas "otro", se descartan y se regeneran
df_pos = df_in[df_in[TYPE_COL] != OTRO_LABEL].copy()
class_sizes = df_pos[TYPE_COL].value_counts()
n_otro = int(OTRO_RATIO * class_sizes.mean())
print("Clases positivas:\n%s" % class_sizes.to_string())
print("Objetivo de filas 'otro': %d" % n_otro)

pos_norm = set(df_pos[VALUE_COL].map(norm))

# distribución de estilo y acentos de las clases positivas
style_dist = df_pos[VALUE_COL].map(style_of).dropna().value_counts(normalize=True)
style_names = style_dist.index.tolist()
style_weights = style_dist.values.tolist()
p_accent_pos = float(df_pos[VALUE_COL].map(has_accent).mean())
print("Estilos en positivas: %s | %% con acento: %.3f" % (style_dist.round(3).to_dict(), p_accent_pos))

# ----------------------- negativos reales (opcional) -----------------------
real_vals = []
for ds_name, col in REAL_NEGATIVE_SOURCES:
    r = dataiku.Dataset(ds_name).get_dataframe(columns=[col], limit=REAL_READ_LIMIT)[col]
    r = r.dropna().astype(str).str.strip()
    r = r[(r != "") & (r.str.len() <= MAX_LEN)].drop_duplicates()
    r = r[~r.map(norm).isin(pos_norm)]
    if len(r) > REAL_MAX_PER_COLUMN:
        r = r.sample(REAL_MAX_PER_COLUMN, random_state=SEED)
    real_vals.extend(r.tolist())
print("Valores reales añadidos a 'otro': %d" % len(real_vals))

# ----------------------- generación sintética -----------------------
n_synth = max(0, n_otro - len(real_vals))
fam_names = list(FAMILIES)
fam_w = [FAMILIES[k][0] for k in fam_names]

synth, seen = [], set()
attempts = 0
while len(synth) < n_synth and attempts < n_synth * 30:
    attempts += 1
    fam = random.choices(fam_names, fam_w)[0]
    _, gen, styled = FAMILIES[fam]
    try:
        v = gen()
    except Exception:
        continue
    if v is None:
        continue
    v = re.sub(r"\s+", " ", str(v)).strip()
    if not v or len(v) > MAX_LEN:
        continue
    if styled and style_names:
        v = apply_style(v, random.choices(style_names, style_weights)[0])
    if v in seen or norm(v) in pos_norm:
        continue
    seen.add(v)
    synth.append((v, fam))

# igualar proporción de acentos con la de las positivas
if synth:
    p_accent_otro = sum(has_accent(v) for v, _ in synth) / len(synth)
    if p_accent_otro > p_accent_pos > 0 or (p_accent_pos == 0 and p_accent_otro > 0):
        p_strip = 1.0 - (p_accent_pos / p_accent_otro)
        synth = [(strip_accents(v) if has_accent(v) and random.random() < p_strip else v, f)
                 for v, f in synth]
    elif p_accent_otro < p_accent_pos:
        print("AVISO: 'otro' tiene menos acentos (%.3f) que las positivas (%.3f)." % (p_accent_otro, p_accent_pos))

df_synth = pd.DataFrame(synth, columns=[VALUE_COL, "family"]).drop_duplicates(subset=[VALUE_COL])
df_synth = df_synth[~df_synth[VALUE_COL].map(norm).isin(pos_norm)]
print("Ejemplos por familia:\n%s" % df_synth.groupby("family").head(3).sort_values("family").to_string(index=False))

values_otro = real_vals + df_synth[VALUE_COL].tolist()
df_otro = pd.DataFrame({VALUE_COL: values_otro, TYPE_COL: OTRO_LABEL}).drop_duplicates(subset=[VALUE_COL])

# ----------------------- salida -----------------------
df_out = pd.concat([df_pos[[VALUE_COL, TYPE_COL]], df_otro], ignore_index=True)
df_out = df_out.sample(frac=1, random_state=SEED).reset_index(drop=True)


# chequeo de atajos: las estadísticas de formato de "otro" no deberían ser extremas
def _stats(s):
    s = s.astype(str)
    return pd.Series({
        "n": len(s),
        "len_media": s.str.len().mean(),
        "tokens_media": s.str.split().str.len().mean(),
        "pct_con_digitos": s.str.contains(r"\d").mean(),
        "pct_upper": s.map(lambda x: style_of(x) == "upper").mean(),
        "pct_acento": s.map(has_accent).mean(),
    })


print("Estadísticas por clase:\n%s" % df_out.groupby(TYPE_COL)[VALUE_COL].apply(_stats).unstack().round(3).to_string())

dataiku.Dataset(OUTPUT_DATASET).write_with_schema(df_out)
