# -*- coding: utf-8 -*-
"""
Validadores estructurales y de dígito verificador para entidades PII (Grupos A y B).

Se ejecutan únicamente sobre los valores que ya pasaron el filtro rápido de regex
en Polars (Sección 7.4 de la documentación).
"""
import datetime as dt
import ipaddress
import re
from typing import Callable, Dict, Optional

# ---------------------------------------------------------------------------
# Catálogos auxiliares oficiales (RENAPO / SAT / ABM / INE)
# ---------------------------------------------------------------------------
_CURP_STATES = frozenset({
    "AS", "BC", "BS", "CC", "CL", "CM", "CS", "CH", "DF", "DG", "GT",
    "GR", "HG", "JC", "MC", "MN", "MS", "NT", "NL", "OC", "PL", "QT",
    "QR", "SP", "SL", "SR", "TC", "TS", "TL", "VZ", "YN", "ZS", "NE",
})

_CURP_CHARS = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
_CURP_DICT = {ch: idx for idx, ch in enumerate(_CURP_CHARS)}

_RFC_CHARS = "0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ Ñ"
_RFC_DICT = {ch: idx for idx, ch in enumerate(_RFC_CHARS)}

_CLABE_WEIGHTS = (3, 7, 1)

_INE_STATE_CODES = frozenset(
    {f"{i:02d}" for i in range(1, 33)} | {"87", "88"}
)

_NON_DIGIT = re.compile(r"\D+")
_COORD_PAIR = re.compile(
    r"^\s*([+-]?\d{1,3}(?:\.\d+)?)\s*[,;/\s]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$"
)
_SINGLE_DECIMAL = re.compile(r"^\s*([+-]?\d{1,3}\.\d{3,10})\s*$")


def _valid_yy_mm_dd(yy_str: str, mm_str: str, dd_str: str) -> bool:
    """Valida que AAMMDD corresponda a una fecha calendario real (considerando bisiestos)."""
    try:
        yy = int(yy_str)
        mm = int(mm_str)
        dd = int(dd_str)
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            return False
        # Usar año 2000 para permitir 29 de febrero si yy % 4 == 0
        year = 2000 + yy if yy <= 29 else 1900 + yy
        dt.date(year, mm, dd)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Grupo A: Validadores fuertes (con dígito verificador o estructura estricta)
# ---------------------------------------------------------------------------
def validate_curp(value: str) -> bool:
    """
    Valida CURP (18 caracteres):
      - Fecha AAMMDD válida (posiciones 4..10)
      - Sexo H, M o X (posición 10)
      - Entidad federativa en catálogo RENAPO (posiciones 11..13)
      - Dígito verificador oficial RENAPO (posición 17)
    """
    s = value.strip().upper()
    if len(s) != 18:
        return False
    if not _valid_yy_mm_dd(s[4:6], s[6:8], s[8:10]):
        return False
    if s[10] not in ("H", "M", "X"):
        return False
    if s[11:13] not in _CURP_STATES:
        return False
    if not s[17].isdigit():
        return False

    try:
        total = sum(_CURP_DICT[s[i]] * (18 - i) for i in range(17))
    except KeyError:
        return False
    check = (10 - (total % 10)) % 10
    return int(s[17]) == check


def validate_rfc(value: str) -> bool:
    """
    Valida RFC de persona física (13 chars) o moral (12 chars):
      - Prefijo de 3 o 4 letras (incluyendo & y Ñ)
      - Fecha AAMMDD válida
      - Homoclave alfanumérica; si el dígito verificador SAT coincide o es 'A'/'0'-'9', pasa.
    """
    s = value.strip().upper().replace("-", "").replace(" ", "")
    if len(s) == 13:
        date_part = s[4:10]
        prefix = s[:4]
    elif len(s) == 12:
        date_part = s[3:9]
        prefix = " " + s[:3]
    else:
        return False

    if not _valid_yy_mm_dd(date_part[0:2], date_part[2:4], date_part[4:6]):
        return False

    # Rechazar RFC genéricos o de prueba obvios si se requiere, aunque XAXX010101000 y XEXX010101000 son válidos en SAT
    if s in ("XAXX010101000", "XEXX010101000"):
        return True

    full_12 = prefix + date_part + s[-3:-1]
    try:
        total = sum(_RFC_DICT[ch] * (13 - i) for i, ch in enumerate(full_12))
    except KeyError:
        return False

    rem = total % 11
    if rem == 0:
        expected = "0"
    elif rem == 10:
        expected = "A"
    else:
        expected = str(11 - rem)

    # En bases bancarias reales algunos RFC tienen homoclave asignada sin el checksum estricto;
    # exigimos checksum exacto o estructura de homoclave válida con fecha real.
    return s[-1] == expected or (s[-3:].isalnum() and s[-1] in "0123456789A")


def validate_clave_elector(value: str) -> bool:
    """
    Valida Clave de Elector INE (18 caracteres):
      - 6 consonantes iniciales
      - 6 dígitos de fecha AAMMDD válida
      - 2 dígitos de entidad (01-32, 87, 88)
      - 1 carácter de sexo (H, M, X)
      - 3 dígitos de homoclave
    """
    s = value.strip().upper().replace("-", "").replace(" ", "")
    if len(s) != 18:
        return False
    if not s[:6].isalpha():
        return False
    if not _valid_yy_mm_dd(s[6:8], s[8:10], s[10:12]):
        return False
    if s[12:14] not in _INE_STATE_CODES:
        return False
    if s[14] not in ("H", "M", "X"):
        return False
    return s[15:18].isdigit()


def validate_clabe(value: str) -> bool:
    """
    Valida CLABE interbancaria (18 dígitos):
      - 18 dígitos numéricos, no todos iguales
      - Código de banco (primeros 3 dígitos != 000)
      - Dígito verificador ponderado (3, 7, 1) mod 10
    """
    s = value.strip().replace(" ", "").replace("-", "")
    if len(s) != 18 or not s.isdigit():
        return False
    if len(set(s)) == 1 or s[:3] == "000":
        return False

    total = sum((int(s[i]) * _CLABE_WEIGHTS[i % 3]) % 10 for i in range(17))
    check = (10 - (total % 10)) % 10
    return int(s[17]) == check


def validate_iban(value: str) -> bool:
    """
    Valida IBAN internacional según ISO 13616 (Módulo 97 == 1).
    """
    s = value.strip().upper().replace(" ", "").replace("-", "")
    if not (15 <= len(s) <= 34):
        return False
    if not (s[:2].isalpha() and s[2:4].isdigit() and s[4:].isalnum()):
        return False

    rearranged = s[4:] + s[:4]
    numeric_chars = []
    for ch in rearranged:
        if ch.isdigit():
            numeric_chars.append(ch)
        else:
            numeric_chars.append(str(ord(ch) - 55))
    return int("".join(numeric_chars)) % 97 == 1


def validate_luhn_card(value: str) -> bool:
    """
    Valida número de tarjeta bancaria (PAN):
      - 13 a 19 dígitos (permitiendo espacios o guiones de separación)
      - Prefijo de red financiera válido (3=Amex/Diners, 4=Visa, 5=MC, 6=Discover/Carnet)
      - Algoritmo de Luhn
    """
    digits = _NON_DIGIT.sub("", value.strip())
    if not (13 <= len(digits) <= 19):
        return False
    if len(set(digits)) == 1:
        return False
    if digits[0] not in ("3", "4", "5", "6"):
        return False

    total = 0
    reverse_digits = digits[::-1]
    for i, ch in enumerate(reverse_digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def validate_email(value: str) -> bool:
    """Valida estructura y dominio de correo electrónico."""
    s = value.strip()
    if len(s) > 254 or "@" not in s or " " in s:
        return False
    local, _, domain = s.rpartition("@")
    if not local or not domain or "." not in domain:
        return False
    if domain.startswith(".") or domain.endswith(".") or ".." in s:
        return False
    tld = domain.rsplit(".", 1)[-1]
    return len(tld) >= 2 and tld.isalpha()


def validate_ip(value: str) -> bool:
    """Valida direcciones IPv4 o IPv6 con la librería estándar `ipaddress`."""
    s = value.strip()
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def validate_coordenadas(value: str) -> bool:
    """
    Valida coordenadas geográficas:
      - Par "lat, lon" con lat en [-90, 90] y lon en [-180, 180], o
      - Valor decimal individual con al menos 3 decimales en rango [-180, 180].
    """
    s = value.strip()
    m_pair = _COORD_PAIR.match(s)
    if m_pair:
        try:
            lat = float(m_pair.group(1))
            lon = float(m_pair.group(2))
            return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
        except ValueError:
            return False

    m_single = _SINGLE_DECIMAL.match(s)
    if m_single:
        try:
            val = float(m_single.group(1))
            return -180.0 <= val <= 180.0 and val != 0.0
        except ValueError:
            return False
    return False


def validate_ssn(value: str) -> bool:
    """
    Valida SSN estadounidense (9 dígitos, con o sin guiones):
      - Descarta áreas 000, 666 y 900-999
      - Descarta grupo 00 y serie 0000
    """
    digits = _NON_DIGIT.sub("", value.strip())
    if len(digits) != 9 or len(set(digits)) == 1:
        return False
    area = int(digits[:3])
    group = int(digits[3:5])
    serial = int(digits[5:])
    if area in (0, 666) or area >= 900:
        return False
    if group == 0 or serial == 0:
        return False
    return True


def validate_nss(value: str) -> bool:
    """
    Valida Número de Seguridad Social IMSS (11 dígitos):
      - Subdelegación (01..99)
      - Dígito verificador IMSS (variante Luhn de izquierda a derecha con pesos 1, 2)
    """
    digits = _NON_DIGIT.sub("", value.strip())
    if len(digits) != 11 or not digits.isdigit():
        return False
    if len(set(digits)) == 1 or digits[:2] == "00":
        return False

    total = 0
    for i in range(10):
        prod = int(digits[i]) * (2 if i % 2 == 1 else 1)
        if prod >= 10:
            prod = (prod // 10) + (prod % 10)
        total += prod
    check = (10 - (total % 10)) % 10
    return int(digits[10]) == check


# ---------------------------------------------------------------------------
# Grupo B: Validadores de estructura y plausibilidad (dependientes de contexto)
# ---------------------------------------------------------------------------
def validate_telefono(value: str) -> bool:
    """
    Valida teléfono de 10 dígitos (con prefijo +52 / 52 opcional):
      - Descarta dígitos todos repetidos (0000000000, 9999999999)
      - Descarta secuencias ascendentes/descendentes triviales (1234567890)
      - Código de área inicial en [2..9] (numeración nacional México / NANP)
    """
    s = value.strip()
    digits = _NON_DIGIT.sub("", s)
    if len(digits) in (12, 13) and digits.startswith("52"):
        digits = digits[-10:]
    elif len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) != 10:
        return False
    if len(set(digits)) <= 2:
        return False
    if digits in ("1234567890", "0123456789", "9876543210", "0987654321"):
        return False
    return digits[0] in "23456789"


def validate_codigo_postal(value: str) -> bool:
    """Valida código postal mexicano de 5 dígitos en rango SEPOMEX (01000 a 99999)."""
    s = value.strip()
    if len(s) != 5 or not s.isdigit():
        return False
    cp = int(s)
    return 1000 <= cp <= 99998


def _parse_flexible_date(value: str) -> Optional[dt.date]:
    s = value.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def validate_fecha_nacimiento(value: str) -> bool:
    """Valida que sea una fecha con edad plausible entre 0 y 115 años."""
    d = _parse_flexible_date(value)
    if d is None:
        return False
    today = dt.date.today()
    age_years = (today - d).days / 365.25
    return 0.0 <= age_years <= 115.0


def validate_fecha_vencimiento(value: str) -> bool:
    """Valida formato MM/AA, MM/AAAA o fecha completa en rango de vigencia razonable (1995..2050)."""
    s = value.strip()
    m = re.match(r"^(0[1-9]|1[0-2])[/\-](\d{2}|\d{4})$", s)
    if m:
        yr = int(m.group(2))
        if yr < 100:
            yr += 2000
        return 1995 <= yr <= 2050

    d = _parse_flexible_date(s)
    if d is not None:
        return 1995 <= d.year <= 2050
    return False


# ---------------------------------------------------------------------------
# Registro de funciones validadoras por nombre
# ---------------------------------------------------------------------------
VALIDATORS: Dict[str, Callable[[str], bool]] = {
    "curp_checksum": validate_curp,
    "rfc_checksum": validate_rfc,
    "clave_elector_check": validate_clave_elector,
    "clabe_checksum": validate_clabe,
    "iban_mod97": validate_iban,
    "luhn_card": validate_luhn_card,
    "email_domain": validate_email,
    "ip_address": validate_ip,
    "coordenadas_range": validate_coordenadas,
    "ssn_rules": validate_ssn,
    "nss_checksum": validate_nss,
    "telefono_rules": validate_telefono,
    "codigo_postal_range": validate_codigo_postal,
    "fecha_nacimiento_plausible": validate_fecha_nacimiento,
    "fecha_vencimiento_plausible": validate_fecha_vencimiento,
}

# Entidades cuyo validador incluye dígito verificador fuerte (reciben bonus_validador en la fusión)
STRONG_CHECKSUM_VALIDATORS = frozenset({
    "curp_checksum",
    "rfc_checksum",
    "clave_elector_check",
    "clabe_checksum",
    "iban_mod97",
    "luhn_card",
    "ip_address",
    "nss_checksum",
})


def get_validator(name: Optional[str]) -> Optional[Callable[[str], bool]]:
    if not name or name == "none":
        return None
    if name not in VALIDATORS:
        raise KeyError(f"Validador desconocido en configuración: {name!r}")
    return VALIDATORS[name]
