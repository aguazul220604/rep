# -*- coding: utf-8 -*-
"""
Python recipe (Dataiku): genera dataset sintetico de las 40 entidades PII.

Entrada : ninguna.
Salida  : dataset `entity_samples_50` con una columna por entidad (40 columnas,
          en el orden de `entities.yaml`) y N_ROWS filas de datos sinteticos
          (N_ROWS se define en el CONFIG de esta receta; valido de 1 a
          1,000,000). Todos los valores son validos por construccion (digitos
          verificadores CURP/RFC/CLABE/NSS/Luhn/IBAN, estructuras INE/SAT/
          SEPOMEX y catalogos oficiales del motor).

Uso en Dataiku:
  1. Crear receta Python sin datasets de entrada.
  2. Crear y conectar el dataset de salida `entity_samples_50`.
  3. Pegar este script y ejecutarlo.

Si `pii_engine` esta importable en el entorno de la receta, cada columna se
valida contra los patterns + validators + catalogos reales de `entities.yaml`
antes de escribir. Si Faker no esta disponible, se usan listas embebidas.
"""
import base64
import datetime as dt
import os
import random
import re
import unicodedata
from pathlib import Path

import pandas as pd

import dataiku

try:
    from faker import Faker
except ImportError:
    Faker = None

try:
    import yaml
    from pii_engine.recognizers.context import normalize_text_value
    from pii_engine.recognizers.registry import DEFAULT_CONFIG_DIR
    from pii_engine.recognizers.validators import get_validator
    ENGINE_AVAILABLE = True
except ImportError:
    ENGINE_AVAILABLE = False

# ======================= CONFIG =======================
OUTPUT_DATASET = "entity_samples_50"
N_ROWS = 50          # editable en la receta; rango valido 1..1,000,000
SEED = 42

# Escalas: por encima de estos umbrales se relajan unicidad y validacion
# para mantener runtime y RAM acotados (1M filas ~8 min / ~3.2 GB).
MAX_ROWS = 1_000_000
UNIQUE_UP_TO = 50_000         # hasta aqui se exigen valores unicos por columna
VALIDATE_FULL_UP_TO = 100_000  # hasta aqui validacion completa del motor
VALIDATE_SAMPLE_SIZE = 20_000  # muestra por columna por encima del umbral

ENTITIES = [
    "curp", "rfc", "clave_elector", "clabe", "iban", "tarjeta", "correo", "ip",
    "coordenadas", "ssn", "nss", "telefono", "codigo_postal", "fecha_nacimiento",
    "fecha_vencimiento", "pasaporte", "licencia_conducir", "soeid", "geid",
    "cic", "ocr", "cuenta", "cliente", "contrato", "nomina", "cvv", "pin_nip",
    "saldo", "credito", "biometrico", "lugar_nacimiento", "nacionalidad",
    "sexo", "estado_civil", "puesto", "nombre", "apellido", "direccion",
    "empresa", "otro_pii",
]

CATALOG_ENTITIES = [
    "lugar_nacimiento", "nacionalidad", "sexo", "estado_civil", "puesto",
]

CONTENT_ENTITIES = ["nombre", "apellido", "direccion", "empresa"]

MAX_DUP_STREAK = 300
# ========================================================

random.seed(SEED)

_FAKER = None
if Faker is not None:
    Faker.seed(SEED)
    _FAKER = Faker("es_MX")
    _FAKER.seed_instance(SEED)

# ---------------------------------------------------------------------------
# Catalogos de respaldo (subconjuntos reales de config/catalogs/*.txt) para
# cuando ni pii_engine ni la ruta del repositorio estan disponibles.
# ---------------------------------------------------------------------------
FALLBACK_CATALOGS = {
    "sexo": [
        "h", "m", "f", "hombre", "mujer", "masculino", "femenino", "male",
        "female", "masc", "fem", "no binario", "indistinto", "no especificado",
        "x",
    ],
    "estado_civil": [
        "soltero", "soltera", "soltero(a)", "casado", "casada", "casado(a)",
        "viudo", "viuda", "viudo(a)", "divorciado", "divorciada",
        "divorciado(a)", "union libre", "concubinato", "separado", "separada",
        "sociedad conyugal", "separacion de bienes", "single", "married",
        "divorced", "widowed", "cohabiting", "s", "c", "v", "d", "u", "ul",
    ],
    "nacionalidad": [
        "mexicana", "mexicano", "mexico", "mx", "mex", "estadounidense",
        "estados unidos", "usa", "us", "canadiense", "canada", "ca",
        "espanola", "espanol", "espana", "es", "colombiana", "colombiano",
        "colombia", "co", "argentina", "argentino", "ar", "chilena", "chileno",
        "chile", "cl", "peruana", "peruano", "peru", "pe", "brasilena",
        "brasileno", "brasil", "br", "venezolana", "venezolano", "venezuela",
        "ve", "guatemalteca", "guatemalteco", "guatemala", "gt", "cubana",
        "cubano", "cuba", "cu", "ecuatoriana", "ecuatoriano", "ecuador", "ec",
        "uruguaya", "uruguayo", "uruguay", "uy", "costarricense", "costa rica",
        "cr", "panamena", "panameno", "panama", "pa",
    ],
    "puesto": [
        "director", "directora", "director general", "subdirector",
        "subdirectora", "gerente", "subgerente", "ejecutivo", "ejecutiva",
        "ejecutivo de cuenta", "ejecutivo bancario", "ejecutivo sr",
        "ejecutivo jr", "analista", "analista sr", "analista jr",
        "coordinador", "coordinadora", "supervisor", "supervisora", "jefe",
        "jefa", "lider de proyecto", "especialista", "consultor", "consultora",
        "asesor", "asesora", "contador", "contadora", "abogado", "abogada",
        "ingeniero", "ingeniera", "arquitecto", "arquitecta", "medico",
        "doctora", "doctor", "profesor", "profesora", "docente",
        "administrador", "administradora", "auxiliar", "asistente",
        "secretaria", "secretario", "recepcionista", "cajero",
    ],
    "lugar_nacimiento": [
        "aguascalientes", "baja california", "baja california sur", "campeche",
        "chiapas", "chihuahua", "ciudad de mexico", "coahuila",
        "coahuila de zaragoza", "colima", "durango", "guanajuato", "guerrero",
        "hidalgo", "jalisco", "mexico", "estado de mexico", "michoacan",
        "michoacan de ocampo", "morelos", "nayarit", "nuevo leon", "oaxaca",
        "puebla", "queretaro", "quintana roo", "san luis potosi", "sinaloa",
        "sonora", "tabasco", "tamaulipas", "tlaxcala", "veracruz", "yucatan",
        "zacatecas", "acapulco", "cancun", "celaya", "ciudad juarez",
        "cuernavaca", "culiacan", "guadalajara", "hermosillo", "leon",
        "merida", "mexicali", "monterrey", "morelia", "pachuca", "queretaro",
    ],
}

# Listas de respaldo para Grupo D cuando no hay Faker.
FIRST_NAMES_MX = [
    "Maria", "Jose", "Juan", "Guadalupe", "Carlos", "Rosa", "Francisco",
    "Josefa", "Antonio", "Ramiro", "Alejandra", "Fernanda", "Diego",
    "Valeria", "Miguel", "Sofia", "Ricardo", "Patricia", "Eduardo", "Carmen",
    "Jorge", "Lucia", "Manuel", "Isabel", "Pedro", "Elena", "Rafael", "Marta",
    "Andres", "Claudia", "Luis", "Gabriela", "Roberto", "Adriana", "Sergio",
    "Beatriz", "Hector", "Silvia", "Ernesto", "Veronica", "Arturo", "Laura",
    "Pablo", "Monica", "Ignacio", "Diana", "Raul", "Teresa", "Jaime", "Norma",
]

LAST_NAMES_MX = [
    "Garcia", "Hernandez", "Lopez", "Martinez", "Rodriguez", "Gonzalez",
    "Perez", "Sanchez", "Ramirez", "Torres", "Flores", "Rivera", "Gomez",
    "Diaz", "Cruz", "Morales", "Reyes", "Ortiz", "Gutierrez", "Chavez",
    "Ramos", "Aguilar", "Vargas", "Romero", "Mendoza", "Herrera", "Medina",
    "Aguirre", "Silva", "Juarez", "Rios", "Cortes", "Guerrero", "Soto",
    "Navarro", "Jimenez", "Sandoval", "Cabrera", "Delgado", "Castillo",
]

STREETS_MX = [
    "Av. Insurgentes", "Calle Juarez", "Av. Reforma", "Calle Hidalgo",
    "Av. Universidad", "Calle Morelos", "Av. Revolucion", "Calle Zaragoza",
    "Av. Constituyentes", "Calle Independencia", "Calle Matamoros",
    "Av. Lopez Mateos", "Calle Allende", "Av. Tolosa", "Calle Madero",
    "Av. Patriotismo", "Calle Donceles", "Av. Revolucion", "Calle Cordoba",
    "Av. Cuauhtemoc",
]

COLONIAS_MX = [
    "Centro", "Roma Norte", "Condesa", "Narvarte", "Del Valle", "Polanco",
    "Coyoacan", "Santa Fe", "San Angel", "Villa de Cortes", "Obrera",
    "Doctores", "Juarez", "Nativitas", "Portales",
]

CIUDADES_MX = [
    "Ciudad de Mexico", "Guadalajara", "Monterrey", "Puebla", "Queretaro",
    "Merida", "Cancun", "Tijuana", "Leon", "Morelia", "Saltillo", "Toluca",
    "Aguascalientes", "Culiacan", "Hermosillo",
]

COMPANIES_MX = [
    "Grupo Sigma", "Comercial Delta", "Industrias del Norte",
    "Tiendas del Sol", "Servicios Integrales del Sur",
    "Grupo Constructor Azteca", "Distribuidora del Centro",
    "Alimentos Rivera", "Transportes Mescalero",
    "Consultores Asociados", "Papeleria Universal",
    "Refaccionaria del Golfo", "Seguros del Bajio",
    "Textiles Monteblanco", "Hospital Santa Fe",
]

EMAIL_DOMAINS = [
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com.mx", "live.com",
    "citibanamex.com.mx", "bbva.com.mx", "profeco.gob.mx",
]

BANK_CODES_CLABE = [
    "002", "012", "014", "016", "019", "021", "022", "058", "062", "072",
    "127", "128", "155", "166", "601", "614", "638", "648", "656", "679",
]

CURP_STATES = (
    "AS", "BC", "BS", "CC", "CL", "CM", "CS", "CH", "DF", "DG", "GT",
    "GR", "HG", "JC", "MC", "MN", "MS", "NT", "NL", "OC", "PL", "QT",
    "QR", "SP", "SL", "SR", "TC", "TS", "TL", "VZ", "YN", "ZS", "NE",
)

INE_STATE_CODES = ["%02d" % i for i in range(1, 33)] + ["87", "88"]

_OTRO_PII_AGE_MIN = 18
_OTRO_PII_AGE_MAX = 99

_CURP_CHARS = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
_CURP_DICT = {ch: idx for idx, ch in enumerate(_CURP_CHARS)}

_RFC_CHARS = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ"
_RFC_DICT = {ch: idx for idx, ch in enumerate(_RFC_CHARS)}

_CONSONANTS = "BCDFGHJKLMNPQRSTVWXYZ"
_VOWELS = "AEIOUX"
_CLABE_WEIGHTS = (3, 7, 1)
_NON_DIGIT = re.compile(r"\D+")


# ----------------------- utilidades -----------------------
def _rand_digits(n):
    return "".join(random.choice("0123456789") for _ in range(n))


def _random_date(year_from, year_to):
    y = random.randint(year_from, year_to)
    while True:
        try:
            return dt.date(y, random.randint(1, 12), random.randint(1, 31))
        except ValueError:
            continue


def _fill(generator, n):
    if n > UNIQUE_UP_TO:
        # Escala grande: sin control de unicidad (ahorra tiempo y RAM;
        # a partir de ~50k filas varias columnas no alcanzan a ser unicas).
        return [generator() for _ in range(n)]
    out = []
    seen = set()
    dup_streak = 0
    while len(out) < n:
        value = generator()
        if value in seen:
            dup_streak += 1
            if dup_streak <= MAX_DUP_STREAK:
                continue
        else:
            dup_streak = 0
            seen.add(value)
        out.append(value)
    return out


# ----------------------- digitos verificadores -----------------------
def _curp_check(base17):
    total = sum(_CURP_DICT[ch] * (18 - i) for i, ch in enumerate(base17))
    return base17 + str((10 - (total % 10)) % 10)


def _rfc_check_char(base12):
    total = sum(_RFC_DICT[ch] * (13 - i) for i, ch in enumerate(base12))
    rem = total % 11
    if rem == 0:
        return "0"
    if rem == 10:
        return "A"
    if rem == 1:
        return "0"
    return str(11 - rem)


def _clabe_check(base17):
    total = sum((int(base17[i]) * _CLABE_WEIGHTS[i % 3]) % 10 for i in range(17))
    return base17 + str((10 - (total % 10)) % 10)


def _nss_check(base10):
    total = 0
    for i, ch in enumerate(base10):
        prod = int(ch) * (2 if i % 2 == 1 else 1)
        if prod >= 10:
            prod = (prod // 10) + (prod % 10)
        total += prod
    return base10 + str((10 - (total % 10)) % 10)


def _luhn_check_digit(body):
    total = 0
    for i, ch in enumerate(reversed(body)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - (total % 10)) % 10)


def _iban_complete(country, bban):
    rearranged = bban + country + "00"
    numeric = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged
    )
    check = 98 - (int(numeric) % 97)
    return "%s%02d%s" % (country, check, bban)


# ----------------------- generadores Grupo A -----------------------
def g_curp():
    date = _random_date(1950, 2005)
    base17 = (
        random.choice(_CONSONANTS)
        + random.choice(_VOWELS)
        + random.choice(_CONSONANTS)
        + random.choice(_CONSONANTS)
        + date.strftime("%y%m%d")
        + random.choice("HMX")
        + random.choice(CURP_STATES)
        + "".join(random.choice(_CONSONANTS) for _ in range(3))
        + random.choice(_CONSONANTS + "0123456789")
    )
    return _curp_check(base17)


def g_rfc():
    date = _random_date(1940, 2000)
    prefix = "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))
    homoclave = "".join(
        random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(2)
    )
    base12 = prefix + date.strftime("%y%m%d") + homoclave
    return base12 + _rfc_check_char(base12)


def g_clave_elector():
    date = _random_date(1955, 2005)
    return (
        "".join(random.choice(_CONSONANTS) for _ in range(6))
        + date.strftime("%y%m%d")
        + random.choice(INE_STATE_CODES)
        + random.choice("HMX")
        + _rand_digits(3)
    )


def g_clabe():
    bank = random.choice(BANK_CODES_CLABE)
    return _clabe_check(bank + _rand_digits(14))


def g_iban():
    country, bban_len = random.choice(
        [("ES", 20), ("GB", 18), ("DE", 18), ("FR", 25), ("IT", 23),
         ("PT", 21), ("BE", 12)]
    )
    bban = _rand_digits(bban_len)
    return _iban_complete(country, bban)


def g_tarjeta():
    prefix, length = random.choice(
        [("4", 16), ("5", 16), ("6", 16), ("34", 15), ("37", 15)]
    )
    body = prefix + _rand_digits(length - 1 - len(prefix))
    digits = body + _luhn_check_digit(body)
    style = random.random()
    if style < 0.60:
        return digits
    if length == 16:
        sep = "-" if style < 0.80 else " "
        return sep.join(digits[i:i + 4] for i in range(0, 16, 4))
    sep = "-" if style < 0.80 else " "
    return digits[:4] + sep + digits[4:10] + sep + digits[10:]


def g_correo():
    def _ascii(value):
        return "".join(
            ch for ch in unicodedata.normalize("NFKD", value)
            if not unicodedata.combining(ch)
        )

    first = _ascii(random.choice(FIRST_NAMES_MX)).lower()
    last = _ascii(random.choice(LAST_NAMES_MX)).lower()
    local = "%s.%s" % (first, last)
    if random.random() < 0.5:
        local += str(random.randint(1, 999))
    return "%s@%s" % (local, random.choice(EMAIL_DOMAINS))


def g_ip():
    if random.random() < 0.70:
        return ".".join(str(random.randint(1, 254)) for _ in range(4))
    groups = [random.randint(0, 0xFFFF) for _ in range(8)]
    groups[0] = random.randint(1, 0xFFFF)
    return ":".join("%x" % g for g in groups)


def g_coordenadas():
    lat = random.uniform(14.5, 32.8)
    lon = random.uniform(-118.4, -86.7)
    return "%.6f, %.6f" % (lat, lon)


def g_ssn():
    while True:
        area = random.randint(1, 899)
        if area == 666:
            continue
        group = random.randint(1, 99)
        serial = random.randint(1, 9999)
        value = "%03d-%02d-%04d" % (area, group, serial)
        digits = _NON_DIGIT.sub("", value)
        if len(set(digits)) > 1:
            return value


def g_nss():
    while True:
        first = random.randint(1, 9)
        value = _nss_check(str(first) + _rand_digits(9))
        if len(set(value)) > 1:
            return value


# ----------------------- generadores Grupo B -----------------------
def g_telefono():
    lada2 = random.choice(["55", "81", "33", "66", "44", "99", "22", "77"])
    lada3 = random.choice(["656", "222", "442", "998", "744", "341"])
    while True:
        style = random.randint(0, 3)
        p1, p2 = _rand_digits(4), _rand_digits(4)
        if style == 0:
            value = "+52 %s %s %s" % (random.choice([lada2, lada3]), p1, p2)
        elif style == 1:
            value = "%s %s %s" % (lada2, p1, p2)
        elif style == 2:
            value = "(%s) %s-%s" % (lada2, p1, p2)
        else:
            value = "+52 %s %s-%s" % (lada3, p1, p2)
        digits = _NON_DIGIT.sub("", value)
        if len(digits) in (12, 13) and digits.startswith("52"):
            core = digits[-10:]
        else:
            core = digits
        if (
            len(core) == 10
            and core[0] in "23456789"
            and len(set(core)) > 2
            and core not in ("1234567890", "0123456789", "9876543210",
                             "0987654321")
        ):
            return value


def g_codigo_postal():
    return "%05d" % random.randint(1000, 99998)


def g_fecha_nacimiento():
    today = dt.date.today()
    start = today - dt.timedelta(days=int(114 * 365.25))
    end = today - dt.timedelta(days=365)
    span = (end - start).days
    date = start + dt.timedelta(days=random.randint(0, span))
    style = random.random()
    if style < 0.60:
        return date.strftime("%Y-%m-%d")
    if style < 0.85:
        return date.strftime("%d/%m/%Y")
    return date.strftime("%Y%m%d")


def g_fecha_vencimiento():
    hi = 2045
    lo = min(dt.date.today().year + 1, hi - 1)
    year = random.randint(lo, hi)
    month = random.randint(1, 12)
    if random.random() < 0.55:
        return "%02d/%02d" % (month, year % 100)
    if random.random() < 0.70:
        return "%02d/%04d" % (month, year)
    return "%04d-%02d-%02d" % (
        year, month, random.randint(1, 28)
    )


def g_pasaporte():
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if random.random() < 0.5:
        return random.choice(letters) + _rand_digits(random.randint(7, 9))
    return (
        random.choice(letters) + random.choice(letters)
        + _rand_digits(random.randint(7, 9))
    )


def g_licencia_conducir():
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "%s%s%s-%s" % (
        random.choice(letters), _rand_digits(7), _rand_digits(1), _rand_digits(5)
    )


def g_soeid():
    letters = "abcdefghijklmnopqrstuvwxyz"
    return (
        random.choice(letters) + random.choice(letters) + _rand_digits(5)
    )


def g_geid():
    return str(random.randint(1000000000, 9999999999))


def g_cic():
    return str(random.randint(100000000, 999999999))


def g_ocr():
    if random.random() < 0.5:
        return str(random.randint(100000000000, 999999999999))
    return str(random.randint(1000000000000, 9999999999999))


def g_cuenta():
    n = random.randint(6, 16)
    first = random.choice("123456789")
    return first + _rand_digits(n - 1)


def g_cliente():
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    style = random.randint(0, 2)
    if style == 0:
        return "CTE%s" % _rand_digits(random.randint(6, 10))
    if style == 1:
        return "%s%s" % (random.choice(letters), _rand_digits(random.randint(7, 11)))
    return "%s-%s" % (
        random.choice(["CL", "CT", "USR"]), _rand_digits(random.randint(5, 10))
    )


def g_contrato():
    year = random.randint(2018, 2026)
    style = random.randint(0, 1)
    if style == 0:
        return "CTO-%d-%s" % (year, _rand_digits(6))
    return "%s-%s" % (random.choice(["CTO", "CNT", "CTA"]), _rand_digits(10))


def g_nomina():
    style = random.randint(0, 1)
    if style == 0:
        return "EMP%s" % _rand_digits(random.randint(6, 8))
    return "NOM-%s" % _rand_digits(random.randint(5, 8))


def g_cvv():
    return _rand_digits(3) if random.random() < 0.5 else _rand_digits(4)


def g_pin_nip():
    return _rand_digits(random.randint(4, 6))


def g_saldo():
    value = random.uniform(500.0, 250000.0)
    sign = "-" if random.random() < 0.20 else ""
    return "%s%.2f" % (sign, value)


def g_credito():
    style = random.randint(0, 1)
    if style == 0:
        return "LNE%s" % _rand_digits(random.randint(6, 10))
    return "%s-%s" % (
        random.choice(["CRED", "LNE", "LOAN"]), _rand_digits(random.randint(5, 9))
    )


def g_biometrico():
    raw = bytes(random.getrandbits(8) for _ in range(32))
    return base64.b64encode(raw).decode("ascii")


def g_lugar_nacimiento():
    return random.choice(_get_catalog("lugar_nacimiento"))


# ----------------------- generadores Grupo C -----------------------
def g_nacionalidad():
    return random.choice(_get_catalog("nacionalidad"))


def g_sexo():
    return random.choice(_get_catalog("sexo"))


def g_estado_civil():
    return random.choice(_get_catalog("estado_civil"))


def g_puesto():
    return random.choice(_get_catalog("puesto"))


# ----------------------- generadores Grupo D -----------------------
def g_nombre():
    if _FAKER is not None:
        k = random.random()
        if k < 0.55:
            return _FAKER.name()
        if k < 0.80:
            return "%s %s" % (_FAKER.first_name(), _FAKER.last_name())
        return "%s %s %s" % (
            _FAKER.first_name(), _FAKER.first_name(), _FAKER.last_name()
        )
    first = random.choice(FIRST_NAMES_MX)
    last = random.choice(LAST_NAMES_MX)
    k = random.random()
    if k < 0.60:
        return "%s %s" % (first, last)
    if k < 0.85:
        return "%s %s %s" % (
            first, random.choice(FIRST_NAMES_MX), last
        )
    return "%s %s-%s" % (
        first, last, random.choice(LAST_NAMES_MX)
    )


def g_apellido():
    if _FAKER is not None:
        if random.random() < 0.60:
            return _FAKER.last_name()
        return "%s-%s" % (_FAKER.last_name(), _FAKER.last_name())
    last = random.choice(LAST_NAMES_MX)
    if random.random() < 0.40:
        return "%s-%s" % (last, random.choice(LAST_NAMES_MX))
    return last


def g_direccion():
    if _FAKER is not None:
        value = _FAKER.street_address()
        if random.random() < 0.60:
            value += ", %s" % _FAKER.city()
        return re.sub(r"\s*\r?\n\s*", ", ", value)
    street = random.choice(STREETS_MX)
    num = random.randint(1, 2500)
    colonia = random.choice(COLONIAS_MX)
    ciudad = random.choice(CIUDADES_MX)
    value = "%s %d, Col. %s, %s" % (street, num, colonia, ciudad)
    if random.random() < 0.40:
        value += ", CP %s" % "%05d" % random.randint(1000, 99998)
    return value


def g_empresa():
    if _FAKER is not None:
        value = _FAKER.company()
        if random.random() < 0.30:
            value += " %s" % _FAKER.company_suffix()
        return value
    value = random.choice(COMPANIES_MX)
    if random.random() < 0.50:
        value += random.choice([
            " S.A. de C.V.", " S.C.", " S. de R.L.", " S.A.B.",
        ])
    if random.random() < 0.30:
        value += " %d" % random.randint(1, 999)
    return value


# ----------------------- generadores Grupo E -----------------------
def g_otro_pii():
    return "edad %d" % random.randint(_OTRO_PII_AGE_MIN, _OTRO_PII_AGE_MAX)


GENERATORS = {
    "curp": g_curp,
    "rfc": g_rfc,
    "clave_elector": g_clave_elector,
    "clabe": g_clabe,
    "iban": g_iban,
    "tarjeta": g_tarjeta,
    "correo": g_correo,
    "ip": g_ip,
    "coordenadas": g_coordenadas,
    "ssn": g_ssn,
    "nss": g_nss,
    "telefono": g_telefono,
    "codigo_postal": g_codigo_postal,
    "fecha_nacimiento": g_fecha_nacimiento,
    "fecha_vencimiento": g_fecha_vencimiento,
    "pasaporte": g_pasaporte,
    "licencia_conducir": g_licencia_conducir,
    "soeid": g_soeid,
    "geid": g_geid,
    "cic": g_cic,
    "ocr": g_ocr,
    "cuenta": g_cuenta,
    "cliente": g_cliente,
    "contrato": g_contrato,
    "nomina": g_nomina,
    "cvv": g_cvv,
    "pin_nip": g_pin_nip,
    "saldo": g_saldo,
    "credito": g_credito,
    "biometrico": g_biometrico,
    "lugar_nacimiento": g_lugar_nacimiento,
    "nacionalidad": g_nacionalidad,
    "sexo": g_sexo,
    "estado_civil": g_estado_civil,
    "puesto": g_puesto,
    "nombre": g_nombre,
    "apellido": g_apellido,
    "direccion": g_direccion,
    "empresa": g_empresa,
    "otro_pii": g_otro_pii,
}


# ----------------------- carga de catalogos -----------------------
_CATALOG_CACHE = {}
CATALOG_SOURCES = {}


def _catalog_dir_candidates():
    candidates = []
    if ENGINE_AVAILABLE:
        candidates.append(DEFAULT_CONFIG_DIR / "catalogs")
    try:
        candidates.append(
            Path(__file__).resolve().parent.parent / "config" / "catalogs"
        )
    except NameError:
        pass
    project_dir = os.environ.get("DKU_FLOW_PROJECT_DIR")
    if project_dir:
        base = Path(project_dir)
        candidates.append(base / "lib" / "pii_engine" / "config" / "catalogs")
        candidates.append(base / "pii_engine" / "config" / "catalogs")
    return candidates


def _get_catalog(name):
    if name in _CATALOG_CACHE:
        return _CATALOG_CACHE[name]
    for catalog_dir in _catalog_dir_candidates():
        path = catalog_dir / ("%s.txt" % name)
        try:
            if not path.exists():
                continue
            values = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except (OSError, UnicodeDecodeError):
            continue
        if values:
            _CATALOG_CACHE[name] = values
            CATALOG_SOURCES[name] = str(path)
            return values
    _CATALOG_CACHE[name] = list(FALLBACK_CATALOGS[name])
    CATALOG_SOURCES[name] = "embebido (fallback)"
    return _CATALOG_CACHE[name]


# ----------------------- validacion -----------------------
def _validate_with_engine(df):
    config_path = DEFAULT_CONFIG_DIR / "entities.yaml"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entities_cfg = cfg["entities"]

    n_rows = len(df)
    sample_mode = n_rows > VALIDATE_FULL_UP_TO
    if sample_mode:
        print(
            "Validacion motor: muestra de %d valores por columna (N_ROWS=%d)"
            % (VALIDATE_SAMPLE_SIZE, n_rows)
        )
        rng = random.Random(SEED)
    else:
        print("Validacion motor: completa (%d valores por columna)" % n_rows)

    for column in ENTITIES:
        spec = entities_cfg[column]
        strategy = str(spec.get("strategy", "regex")).lower()
        values = df[column].astype(str).tolist()
        if sample_mode:
            values = rng.sample(values, VALIDATE_SAMPLE_SIZE)

        if strategy == "regex":
            patterns = spec.get("patterns") or []
            if patterns:
                combined = "(?i)(?:" + "|".join(
                    "(?:%s)" % p for p in patterns
                ) + ")"
                rx = re.compile(combined)
                for value in values:
                    if not rx.search(value):
                        raise RuntimeError(
                            "[%s] valor sin match de patron: %r"
                            % (column, value)
                        )
            validator = get_validator(spec.get("validator"))
            if validator is not None:
                for value in values:
                    if not validator(value):
                        raise RuntimeError(
                            "[%s] validador rechaza: %r" % (column, value)
                        )
        elif strategy == "dictionary":
            catalog_path = DEFAULT_CONFIG_DIR / spec["catalog"]
            catalog = set()
            for line in catalog_path.read_text(encoding="utf-8").splitlines():
                clean = line.strip()
                if clean and not clean.startswith("#"):
                    normalized = normalize_text_value(clean)
                    if normalized:
                        catalog.add(normalized)
            for value in values:
                if normalize_text_value(value) not in catalog:
                    raise RuntimeError(
                        "[%s] valor fuera de catalogo: %r" % (column, value)
                    )
        else:
            for value in values:
                if not str(value).strip():
                    raise RuntimeError("[%s] valor vacio" % column)


def validate_output(df):
    if list(df.columns) != ENTITIES:
        raise ValueError(
            "Columnas incorrectas: se esperaban %d entidades en el orden "
            "de entities.yaml." % len(ENTITIES)
        )
    if df.shape != (N_ROWS, len(ENTITIES)):
        raise ValueError(
            "Forma incorrecta: se esperaba (%d, %d), se obtuvo %s"
            % (N_ROWS, len(ENTITIES), df.shape)
        )
    if df.isna().any().any():
        raise ValueError("La salida contiene valores nulos.")
    for column in ENTITIES:
        if (df[column].astype(str).str.strip() == "").any():
            raise ValueError("La columna '%s' contiene valores vacios." % column)

    if not ENGINE_AVAILABLE:
        print(
            "AVISO: pii_engine no esta importable en este entorno; "
            "validacion limitada a forma y valores no vacios."
        )
        return

    _validate_with_engine(df)


# ----------------------- main -----------------------
def main():
    if not isinstance(N_ROWS, int) or isinstance(N_ROWS, bool):
        raise ValueError(
            "N_ROWS debe ser un entero; se obtuvo %r." % (N_ROWS,)
        )
    if not (1 <= N_ROWS <= MAX_ROWS):
        raise ValueError(
            "N_ROWS debe estar entre 1 y %d; se obtuvo %d."
            % (MAX_ROWS, N_ROWS)
        )
    if len(ENTITIES) != 40:
        raise ValueError("Se esperaban 40 entidades, hay %d." % len(ENTITIES))
    if set(GENERATORS) != set(ENTITIES):
        missing = set(ENTITIES) - set(GENERATORS)
        extra = set(GENERATORS) - set(ENTITIES)
        raise ValueError(
            "GENERATORS desalineado. Faltan: %s; sobran: %s"
            % (sorted(missing), sorted(extra))
        )

    print(
        "Generando %d columnas x %d filas (seed=%d, faker=%s, engine=%s)"
        % (len(ENTITIES), N_ROWS, SEED, _FAKER is not None, ENGINE_AVAILABLE)
    )

    data = {}
    for entity in ENTITIES:
        values = _fill(GENERATORS[entity], N_ROWS)
        data[entity] = values
        print("  %-18s -> %r" % (entity, values[0]))

    df = pd.DataFrame(data, columns=ENTITIES).astype(str)
    validate_output(df)

    for name in sorted(CATALOG_SOURCES):
        print("catalogo %-18s -> %s" % (name, CATALOG_SOURCES[name]))

    print("Forma final: %s" % (df.shape,))
    print("Primeras 2 filas:\n%s" % df.head(2).to_string(index=False))

    dataiku.Dataset(OUTPUT_DATASET).write_with_schema(df)
    print("Dataset '%s' escrito correctamente." % OUTPUT_DATASET)


main()
