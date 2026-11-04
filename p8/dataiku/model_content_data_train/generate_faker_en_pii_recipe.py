# -*- coding: utf-8 -*-
"""
Python recipe (Dataiku): genera datos PII sinteticos en ingles con Faker.

Entrada : ninguna.
Salida  : dataset con columnas value, type y las clases direccion, nombre,
          apellido y empresa. Debe conectarse como una rama nueva al Stack
          que consolida el dataset de entrenamiento.

La clase "otro" se mantiene en su receta independiente. Las cuotas son
configurables y la semilla fija hace que la generacion sea reproducible.
"""
import random
import re
import unicodedata

import pandas as pd
import dataiku
from faker import Faker

# ======================= CONFIG =======================
OUTPUT_DATASET = "training_dataset_faker_en_pii"
VALUE_COL, TYPE_COL = "value", "type"

CLASS_QUOTAS = {
    "direccion": 25000,
    "nombre": 25000,
    "apellido": 25000,
    "empresa": 50000,
}
STYLE_NAMES = ["title", "lower", "upper"]
STYLE_WEIGHTS = [0.70, 0.20, 0.10]

LOCALES = ["en_US", "en_GB"]
LOCALE_WEIGHTS = [0.80, 0.20]
SEED = 42
MAX_LEN = 120
MAX_ATTEMPTS_PER_ROW = 50
PROGRESS_EVERY = 5000
# ======================================================

random.seed(SEED)

fakers = {}
for i, locale in enumerate(LOCALES):
    faker = Faker(locale)
    faker.seed_instance(SEED + i)
    fakers[locale] = faker


# ----------------------- helpers -----------------------
def strip_accents(value):
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(value))
        if not unicodedata.combining(char)
    )


def norm(value):
    value = strip_accents(value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def apply_style(value, style):
    if style == "upper":
        return value.upper()
    if style == "lower":
        return value.lower()
    if style == "title":
        return value.title()
    return value


def fk(method):
    """Llama un metodo de Faker con uno de los locales ingleses configurados."""
    locale = random.choices(LOCALES, weights=LOCALE_WEIGHTS, k=1)[0]
    return getattr(fakers[locale], method)()


def clean_value(value):
    return re.sub(r"\s+", " ", str(value)).strip()


# ----------------------- generadores -----------------------
def g_direccion():
    # La mayoria son direcciones completas; tambien se incluyen calles solas.
    method = "address" if random.random() < 0.85 else "street_address"
    value = fk(method)
    if method == "address":
        value = re.sub(r"\s*\r?\n\s*", ", ", value)
    return value


def g_nombre():
    """Nombres completos, no nombres de pila aislados."""
    k = random.random()
    if k < 0.65:
        return fk("name")
    if k < 0.85:
        return "%s %s" % (fk("first_name"), fk("last_name"))
    if k < 0.95:
        return "%s %s %s" % (
            fk("first_name"), fk("first_name"), fk("last_name")
        )
    return "%s %s-%s" % (
        fk("first_name"), fk("last_name"), fk("last_name")
    )


def g_apellido():
    """Incluye apellidos simples y compuestos con guion."""
    if random.random() < 0.60:
        return fk("last_name")
    return "%s-%s" % (fk("last_name"), fk("last_name"))


COMPANY_SUFFIX_RE = re.compile(
    r"(?:\b(?:inc|incorporated|llc|ltd|limited|plc|llp|group|holdings|"
    r"company|co|corp|corporation)\.?|\band sons)\s*$",
    re.IGNORECASE,
)


def g_empresa():
    """Evita empresas formadas solo por apellidos, indistinguibles de personas."""
    value = clean_value(fk("company"))
    if not COMPANY_SUFFIX_RE.search(value):
        value = "%s %s" % (value, fk("company_suffix"))
    return value


GENERATORS = {
    "direccion": g_direccion,
    "nombre": g_nombre,
    "apellido": g_apellido,
    "empresa": g_empresa,
}


def generate_class(label, quota, seen_norm, style_names, style_weights):
    rows = []
    attempts = 0
    max_attempts = max(1, quota * MAX_ATTEMPTS_PER_ROW)

    while len(rows) < quota and attempts < max_attempts:
        attempts += 1
        value = clean_value(GENERATORS[label]())
        if not value or len(value) > MAX_LEN:
            continue
        if style_names:
            style = random.choices(style_names, weights=style_weights, k=1)[0]
            value = apply_style(value, style)
        value_norm = norm(value)
        if not value_norm or value_norm in seen_norm:
            continue

        seen_norm.add(value_norm)
        rows.append((value, label))
        if PROGRESS_EVERY and len(rows) % PROGRESS_EVERY == 0:
            print("%s: %d/%d" % (label, len(rows), quota))

    if len(rows) != quota:
        raise RuntimeError(
            "No se pudo completar la cuota de '%s': %d/%d tras %d intentos. "
            "Revisa duplicados, MAX_LEN o la variedad del generador."
            % (label, len(rows), quota, attempts)
        )
    return rows


def validate_output(df_out):
    if list(df_out.columns) != [VALUE_COL, TYPE_COL]:
        raise ValueError("La salida debe contener unicamente value, type.")
    if df_out.isna().any().any():
        raise ValueError("La salida contiene valores nulos.")
    if (df_out[VALUE_COL].str.strip() == "").any():
        raise ValueError("La salida contiene valores vacios.")
    if not set(df_out[TYPE_COL]).issubset(GENERATORS):
        raise ValueError("La salida contiene etiquetas no permitidas.")
    normalized = df_out[VALUE_COL].map(norm)
    if normalized.duplicated().any():
        raise ValueError("La salida contiene valores normalizados duplicados.")
    actual_counts = df_out[TYPE_COL].value_counts().to_dict()
    expected_counts = {k: v for k, v in CLASS_QUOTAS.items() if v > 0}
    if actual_counts != expected_counts:
        raise ValueError(
            "Cuotas incorrectas. Esperado: %s; obtenido: %s"
            % (expected_counts, actual_counts)
        )


def main():
    if len(LOCALES) != len(LOCALE_WEIGHTS) or not LOCALES:
        raise ValueError("LOCALES y LOCALE_WEIGHTS deben tener la misma longitud.")
    if set(CLASS_QUOTAS) != set(GENERATORS):
        raise ValueError("CLASS_QUOTAS debe configurar las cuatro clases PII.")
    if any(not isinstance(q, int) or q < 0 for q in CLASS_QUOTAS.values()):
        raise ValueError("Todas las cuotas deben ser enteros no negativos.")
    if len(STYLE_NAMES) != len(STYLE_WEIGHTS) or not STYLE_NAMES:
        raise ValueError("STYLE_NAMES y STYLE_WEIGHTS deben tener la misma longitud.")
    if any(weight < 0 for weight in STYLE_WEIGHTS) or sum(STYLE_WEIGHTS) <= 0:
        raise ValueError("STYLE_WEIGHTS debe contener pesos no negativos y alguno positivo.")

    # Deduplicacion global entre todos los valores sinteticos generados.
    seen_norm = set()

    rows = []
    for label, quota in CLASS_QUOTAS.items():
        print("Generando %s: %d filas" % (label, quota))
        rows.extend(generate_class(
            label, quota, seen_norm, STYLE_NAMES, STYLE_WEIGHTS
        ))

    df_out = pd.DataFrame(rows, columns=[VALUE_COL, TYPE_COL])
    df_out = df_out.sample(frac=1, random_state=SEED).reset_index(drop=True)
    validate_output(df_out)

    print("Filas generadas por clase:\n%s" % df_out[TYPE_COL].value_counts().to_string())
    print(
        "Ejemplos por clase:\n%s"
        % df_out.groupby(TYPE_COL).head(3).sort_values(TYPE_COL).to_string(index=False)
    )
    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(df_out)


main()
