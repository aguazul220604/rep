# -*- coding: utf-8 -*-
"""
Inferencia sobre nombres de columna (Sección 8 de la documentación):
  1. Modelo binario de nombre (`model_binary_v1`) como compuerta de PII.
  2. Reglas determinísticas de `regex.txt` (`_NAME_REGEX_RULES`) como fast path
     para entidades de alta precisión y como respaldo.
  3. Modelo multiclase de entidad (`model_entity_v1`) solo para las columnas que
     el binario marca como PII y que no resolvió el fast path.

Optimizaciones de resolución de nombres:
  - El modelo de entidad no corre en columnas que el binario descarta.
  - El fast path regex evita el modelo de entidad en nombres inequívocos.
  - Las predicciones se cachean por nombre normalizado (LRU configurable).
"""
from collections import OrderedDict
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from pii_engine.core.schema import NO_ENTITY, OTRO_PII, NamePrediction
from pii_engine.recognizers.context import normalize_column_name

# ---------------------------------------------------------------------------
# Reglas ordenadas de `regex.txt` (40 entidades) como respaldo y refuerzo
# ---------------------------------------------------------------------------
_NAME_REGEX_RULES: List[Tuple[str, re.Pattern]] = [
    ("fecha_vencimiento", re.compile(r"(expry|exp dt|venc|fec vig)")),
    ("correo", re.compile(r"(mail|correo)")),
    ("curp", re.compile(r"(curp)")),
    ("rfc", re.compile(r"(rfc|rf [0-9] d.gitos|fiscal)")),
    ("nss", re.compile(r"(nss|seg cli|cod seguridad imss)")),
    ("ssn", re.compile(r"(social[ _.-]?security|(^|[^a-z])(ssn)([^a-z]|$))")),
    ("clave_elector", re.compile(r"((clave|cve|clv)[ _.-]?elector)")),
    ("cic", re.compile(r"((^|[^a-z])(cic|cic[ _.-]?(ine|ife))([^a-z]|$))")),
    ("ocr", re.compile(r"((^|[^a-z])(ocr|ocr[ _.-]?(ine|ife))([^a-z]|$))")),
    ("pasaporte", re.compile(r"(pasaporte|passport)")),
    ("licencia_conducir", re.compile(r"(licencia[ _.-]?(de[ _.-]?)?(cond|manejo)|driver[ _.-]?licen[sc]e|(^|[^a-z])(lic[ _.-]?(cond[a-z]*|manejo))([^a-z]|$))")),
    ("soeid", re.compile(r"((^|[^a-z])(soeid|soe[ _.-]?id)([^a-z]|$))")),
    ("geid", re.compile(r"((^|[^a-z])(geid|ge[ _.-]?id)([^a-z]|$))")),
    ("lugar_nacimiento", re.compile(r"((lugar|pais|ciudad|entidad|edo|estado|municipio|localidad)[ _.-]?(de[ _.-]?)?nac(im|ido)|birth[ _.-]?place|place[ _.-]?of[ _.-]?birth|lugar[ _.-]?nac)")),
    ("fecha_nacimiento", re.compile(r"(nacim|brthdt|fecnac|birth dt|birth date)")),
    ("nacionalidad", re.compile(r"(nacionalidad|nationality|citizenship|ciudadania|(^|[^a-z])(nacion)([^a-z]|$))")),
    ("sexo", re.compile(r"(sexo|gndr|genero)")),
    ("estado_civil", re.compile(r"(estado civil|edo civil|est civil)")),
    ("apellido", re.compile(r"(apellido|ap materno|ap paterno|a materno|a paterno|apepat|apemat)")),
    ("cvv", re.compile(r"(vc security code|cod(igo)?[ _.-]?(de[ _.-]?)?seguridad[ _.-]?tarj|card[ _.-]?(security|verification)[ _.-]?code|(^|[^a-z])(cvv|cvc|cvv2|cvc2|csc)([^a-z]|$))")),
    ("pin_nip", re.compile(r"((^|[^a-z])(nip|pin)([^a-z]|$))")),
    ("tarjeta", re.compile(r"(card|crd|plastico|nucc|tarjeta|(^|[^a-z])(pan)([^a-z]|$))")),
    ("nomina", re.compile(r"^(?!.*(nombre|name|(^|[^a-z])nom([^a-z]|$))).*(nomina|numnom|numemp|empleado|vendnomi)")),
    ("coordenadas", re.compile(r"(latitud|latitude|longitud|longitude|coordenad|geoloc|geohash|ubicacion[ _.-]?gps|(^|[^a-z])(lat|lon|lng|geo|gps|utm)([^a-z]|$))")),
    ("codigo_postal", re.compile(r"(cpl|cod postalcte|cod postalnegocio|cod postal|codigo postal|postal code|num codigopostal|(^|[^a-z])(cp|zip)([^a-z]|$))")),
    ("direccion", re.compile(r"^(?!.*(civil|riesgo)).*(addr|calle|street|domicil|direc|direccion|avenida|manzana|apartado[ _.-]?postal|po[ _.-]?box|colonia|colony|nomcol|asentamiento|fraccionamiento|neighborhood|barrio|municip|delegacion|alcaldia|localidad|poblacion|nompob|pobnegocio|locality|ciudadcte|ciudaddom|edo[ _.-]?(dom|cte|res|sol)|estadocte|estadodom|cveedo|cve[ _.-]?edo|entidad[ _.-]?(res|sol|fed)|entidadfed|ent[ _.-]?fed|nom[ _.-]?entidadfedcte|nom[ _.-]?estado|province|provincia|pais|country|cntry|cnrty|paisnegocio|vc issuer domicile|vc domicile|(^|[^a-z])(dir|dom|domic|blvd|boulevard|mza|lote|apdo|(num|numero|no|nro)[ _.-]?(ext|int)(erior)?|col|deleg|mpio|mun|town|loc|ciudad|city|cdad|estado|edo|state|estate|estat|ste[ _.-]?bus|ctry)([^a-z]|$))")),
    ("telefono", re.compile(r"^(?!.*(extension|extn|ext[0-9]|(^|[^a-z])ext([^a-z]|$))).*(telefon|phone|fax|numtel|celular|whatsapp|telcel|mobile|numcel|celnum|landline|fixed[ _.-]?line|cvet|(^|[^a-z])(tel|tfno|tlf|ph(one)?|ph[ _.-]?no|cel|cell|movil|lada)([^a-z]|$))")),
    ("saldo", re.compile(r"(saldo|sdo|ingreso|bal|balance|capital|curr bal|tot prin|impcasa|limite|lmt|capvig)")),
    ("credito", re.compile(r"^(?!.*credencial).*(cred|linea|line id|linnum|loan)")),
    ("empresa", re.compile(r"(empresa|nom negafiliado|razon social|razon|empleador|compania|nom recvcor|nom oribnk|nom recvbnk|nom intmed|nom dealer)")),
    ("nombre", re.compile(r"(nombre|name|nm txt|nom ordenante|nomcte|beneficiario|nom usuario|nom ben|nom aval|nom oper|nomcli|nom emisor|nom tercer|nom deudor|represen|(^|[^a-z])(nm|nom)([^a-z]|$))")),
    ("cliente", re.compile(r"(clien|clnt|cust|ctem|ctenum|gfcid|user ref|num cte)")),
    ("contrato", re.compile(r"^(?!.*creactecto).*(contrat|contract|num contrtraspaso|num contras|num contorig|num prefcontorig|(^|[^a-z])(ctos?)([^a-z]|$))")),
    ("iban", re.compile(r"((^|[^a-z])(iban)([^a-z]|$))")),
    ("clabe", re.compile(r"(cuentaclabe|num cuenta clabe dest|clabe interbanca|id clabe dest|(^|[^a-z])(clabe)([^a-z]|$))")),
    ("cuenta", re.compile(r"^(?!.*actacons).*(acct|account|acnt|cuenta|cta|reln|num inversion e|num inversion|inversion)")),
    ("biometrico", re.compile(r"(huella|biometr)")),
    ("puesto", re.compile(r"(manager|ejecutivo|ocupacion|puesto)")),
    ("ip", re.compile(r"(ip isp|ip maquina|ip pc solctd|(^|[^a-z])(ip|ipv4|ipv6)([^a-z]|$))")),
    (OTRO_PII, re.compile(r"(credencial[ _.-]?(ine|ife|elector|(para[ _.-]?)?votar)|idmex|voter[ _.-]?id|(^|[^a-z])(ine|ife|elector|id[ _.-]?mex)([^a-z]|$)|corres destario fi bic|ent intermediaria bic|rec beneficiari institution|seccion sol|seccion res|des remesa|firmae operalfa|dato particular|(^|[^a-z])(edad|age)([^a-z]|$)|num identif|num id|dispositivo|medioacceso|tok req|sirh|numdepend|dependientes|ptrmadre|indicadorextranjero|num depend|numautos|car dlr|num autos|num rollover|cnt ptsmaxsbtasa|actacons|creactecto|num digverificador|num negafiliado|vc issuer code|cm cte pais riesgo|cve|dleq|delq|extn|ext[0-9]|extension oficina|(^|[^a-z])ext([^a-z]|$))")),
]

# Campos que se guardan en la caché de predicciones por nombre normalizado.
_CACHE_FIELDS = (
    "is_pii_name",
    "score_name",
    "expected_entity",
    "prob_entity_name",
    "entity_top1",
    "abstained",
    "source",
)


def match_rule_entity_from_normalized(norm: str) -> Optional[str]:
    """Evalúa las reglas determinísticas sobre un nombre ya normalizado."""
    if not norm:
        return None
    for entity, pattern in _NAME_REGEX_RULES:
        if pattern.search(norm):
            return entity
    return None


def match_rule_entity(column_name: str) -> Optional[str]:
    """Evalúa las reglas determinísticas de `regex.txt` sobre el nombre normalizado."""
    return match_rule_entity_from_normalized(normalize_column_name(column_name))


def _apply_temperature(proba: np.ndarray, temperature: float) -> np.ndarray:
    logp = np.log(np.clip(np.asarray(proba, dtype=float), 1e-9, 1.0)) / float(temperature)
    logp -= logp.max(axis=1, keepdims=True)
    q = np.exp(logp)
    return q / q.sum(axis=1, keepdims=True)


_LEGACY_ENTITY_CLASS = "HybridEntityClassifier"
_LEGACY_ENTITY_SYMBOLS = {"HybridEntityClassifier": "HybridEntityClassifier", "_softmax_rows": "_softmax_rows"}
_JOBLIB_LEGACY_LOCK = threading.Lock()


def _is_legacy_entity_error(exc: BaseException) -> bool:
    if _LEGACY_ENTITY_CLASS in str(exc):
        return True
    return isinstance(exc, ModuleNotFoundError)


def _load_joblib_bundle(path: Path) -> Any:
    import joblib

    try:
        return joblib.load(path)
    except (AttributeError, ModuleNotFoundError) as exc:
        if not _is_legacy_entity_error(exc):
            raise

        try:
            import pii_entity_model
        except Exception:
            raise exc

        from joblib import numpy_pickle

        with _JOBLIB_LEGACY_LOCK:
            original_find_class = numpy_pickle.NumpyUnpickler.find_class

            def _find_class(unpickler, module, name):
                target = _LEGACY_ENTITY_SYMBOLS.get(name)
                if target is not None:
                    return getattr(pii_entity_model, target)
                return original_find_class(unpickler, module, name)

            numpy_pickle.NumpyUnpickler.find_class = _find_class
            try:
                return joblib.load(path)
            finally:
                numpy_pickle.NumpyUnpickler.find_class = original_find_class


class NameModelPredictor:
    """
    Gestiona la inferencia de los modelos de nombre (`model_binary_v1` y `model_entity_v1`).

    Flujo:
      1. Binario para todas las columnas (barato y vectorizado).
      2. Reglas de nombre para todas las columnas (fast path y respaldo).
      3. Modelo de entidad solo para columnas PII que no resolvió el fast path.
      4. Caché por nombre normalizado para evitar repetir trabajo entre tablas.

    Si no se proporcionan los bundles `model.joblib`, opera de forma determinística
    con el motor de reglas de `regex.txt` para permitir uso local inmediato.
    """

    def __init__(
        self,
        binary_bundle: Optional[Union[str, Path, Dict[str, Any]]] = None,
        entity_bundle: Optional[Union[str, Path, Dict[str, Any]]] = None,
        binary_threshold: float = 0.409,
        entity_min_confidence: float = 0.45,
        use_rules_fallback: bool = True,
        name_resolution_cfg: Optional[Dict[str, Any]] = None,
    ):
        self.binary_bundle = self._load_bundle(binary_bundle)
        self.entity_bundle = self._load_bundle(entity_bundle)
        self.binary_threshold = float(binary_threshold)
        self.entity_min_confidence = float(entity_min_confidence)
        self.use_rules_fallback = bool(use_rules_fallback)

        cfg = name_resolution_cfg or {}
        self.binary_gate_enabled = bool(cfg.get("binary_gate_enabled", True))
        self.fast_path_enabled = bool(cfg.get("fast_path_enabled", True))
        self.fast_path_min_confidence = float(cfg.get("fast_path_min_confidence", 0.85))
        self.fast_path_entities: Dict[str, float] = {
            str(entity): float(confidence)
            for entity, confidence in (cfg.get("fast_path_entities") or {}).items()
        }
        self.cache_size = int(cfg.get("cache_size", 4096))
        self._cache: "OrderedDict[str, Tuple[Any, ...]]" = OrderedDict()
        self._cache_lock = threading.Lock()

    @staticmethod
    def _load_bundle(source: Optional[Union[str, Path, Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        if source is None:
            return None
        if isinstance(source, dict):
            return source
        p = Path(source)
        if p.is_dir():
            p = p / "model.joblib"
        if not p.exists():
            return None
        loaded = _load_joblib_bundle(p)
        if isinstance(loaded, dict):
            return loaded
        return {"model": loaded}

    @property
    def model_version_tag(self) -> str:
        bin_tag = "bin:joblib" if self.binary_bundle is not None else "bin:rules"
        ent_tag = "ent:joblib" if self.entity_bundle is not None else "ent:rules"
        return f"{bin_tag},{ent_tag}"

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def _cache_get(self, norm_name: str) -> Optional[Tuple[Any, ...]]:
        if self.cache_size <= 0:
            return None
        with self._cache_lock:
            if norm_name in self._cache:
                self._cache.move_to_end(norm_name)
                return self._cache[norm_name]
        return None

    def _cache_put(self, norm_name: str, prediction: NamePrediction) -> None:
        if self.cache_size <= 0:
            return
        with self._cache_lock:
            self._cache[norm_name] = tuple(
                getattr(prediction, field) for field in _CACHE_FIELDS
            )
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def _from_cache(self, column: str, norm_name: str, cached: Tuple[Any, ...]) -> NamePrediction:
        return NamePrediction(
            column=column,
            normalized_name=norm_name,
            **dict(zip(_CACHE_FIELDS, cached)),
        )

    def _is_fast_path(self, entity: Optional[str]) -> bool:
        return bool(
            self.fast_path_enabled
            and entity
            and entity in self.fast_path_entities
            and self.fast_path_entities[entity] >= self.fast_path_min_confidence
        )

    def predict(self, columns: List[str]) -> Dict[str, NamePrediction]:
        if not columns:
            return {}

        norm_names = [normalize_column_name(c) for c in columns]
        results: Dict[str, NamePrediction] = {}
        pending: List[int] = []

        for i, column in enumerate(columns):
            cached = self._cache_get(norm_names[i])
            if cached is None:
                pending.append(i)
            else:
                results[column] = self._from_cache(column, norm_names[i], cached)

        if not pending:
            return results

        raw_names = [columns[i] for i in pending]
        norms = [norm_names[i] for i in pending]
        n = len(pending)

        # ------------------------------------------------------------------
        # 1. Modelo binario (vectorizado)
        # ------------------------------------------------------------------
        bin_scores = np.zeros(n, dtype=float)
        bin_thresh = self.binary_threshold
        if self.binary_bundle is not None:
            model = self.binary_bundle["model"]
            bin_thresh = float(self.binary_bundle.get("threshold", self.binary_threshold))
            probs = np.asarray(model.predict_proba(np.asarray(raw_names, dtype=object)), dtype=float)
            bin_scores = probs[:, 1] if probs.ndim == 2 else probs

        # ------------------------------------------------------------------
        # 2. Reglas de nombre (fast path y respaldo)
        # ------------------------------------------------------------------
        rules = (
            [match_rule_entity_from_normalized(norm) for norm in norms]
            if self.use_rules_fallback
            else [None] * n
        )

        if self.binary_bundle is None:
            is_pii = [rule is not None for rule in rules]
        else:
            is_pii = [bool(score >= bin_thresh) for score in bin_scores]

        # ------------------------------------------------------------------
        # 3. Modelo de entidad solo para columnas PII no resueltas por regex
        # ------------------------------------------------------------------
        ent_pred: List[Optional[str]] = [None] * n
        ent_top1: List[Optional[str]] = [None] * n
        ent_prob = np.zeros(n, dtype=float)
        ent_abstained = [True] * n

        need_entity: List[int] = []
        for i in range(n):
            if self.binary_gate_enabled and not is_pii[i]:
                continue
            if self._is_fast_path(rules[i]):
                continue
            if self.entity_bundle is not None:
                need_entity.append(i)

        if need_entity and self.entity_bundle is not None:
            ent_model = self.entity_bundle["model"]
            classes = np.asarray(
                self.entity_bundle.get("classes", getattr(ent_model, "classes_", []))
            )
            sub_names = np.asarray([raw_names[i] for i in need_entity], dtype=object)
            raw = np.asarray(ent_model.predict_proba(sub_names), dtype=float)
            P = np.zeros((len(sub_names), len(classes)), dtype=float)
            if hasattr(ent_model, "classes_"):
                P[:, np.searchsorted(classes, ent_model.classes_)] = raw
            else:
                P = raw
            temperature = float(self.entity_bundle.get("temperature", 1.0))
            P = _apply_temperature(P, temperature)
            min_conf = float(self.entity_bundle.get("min_confidence", self.entity_min_confidence))

            for row, i in enumerate(need_entity):
                top_idx = int(np.argmax(P[row]))
                class_raw = str(classes[top_idx])
                class_norm = "estado_civil" if class_raw == "estado civil" else class_raw
                prob_value = float(P[row, top_idx])
                ent_top1[i] = class_norm
                ent_prob[i] = prob_value
                if prob_value >= min_conf and class_norm != NO_ENTITY:
                    ent_pred[i] = class_norm
                    ent_abstained[i] = False

        # ------------------------------------------------------------------
        # 4. Combinar y cachear
        # ------------------------------------------------------------------
        for i in range(n):
            rule_entity = rules[i]
            expected_entity = ent_pred[i]
            prob_entity = float(ent_prob[i])
            top1_entity = ent_top1[i]
            abstained = ent_abstained[i]
            source = "model"

            if self._is_fast_path(rule_entity) and is_pii[i]:
                expected_entity = rule_entity
                top1_entity = rule_entity
                prob_entity = self.fast_path_entities[rule_entity]
                abstained = False
                source = "rules"
            elif expected_entity is None and rule_entity is not None:
                expected_entity = rule_entity
                top1_entity = top1_entity or rule_entity
                prob_entity = max(prob_entity, 0.85 if self.entity_bundle is None else 0.65)
                abstained = False
                source = "rules" if self.entity_bundle is None else "both"

            if self.binary_bundle is None:
                rule_pii = rule_entity is not None
                rescued = not self.binary_gate_enabled and expected_entity is not None
                score_name = 0.88 if rule_pii else (0.75 if rescued else 0.08)
                is_pii_value = rule_pii or rescued
            else:
                score_name = float(bin_scores[i])
                is_pii_value = bool(is_pii[i])
                if not self.binary_gate_enabled and expected_entity is not None and not is_pii_value:
                    score_name = max(score_name, 0.55)
                    is_pii_value = True

            prediction = NamePrediction(
                column=columns[pending[i]],
                normalized_name=norms[i],
                is_pii_name=is_pii_value,
                score_name=score_name,
                expected_entity=expected_entity if is_pii_value else None,
                prob_entity_name=prob_entity if is_pii_value else 0.0,
                entity_top1=top1_entity,
                abstained=abstained,
                source=source if is_pii_value else ("rules" if self.binary_bundle is None else "model"),
            )
            results[columns[pending[i]]] = prediction
            self._cache_put(norms[i], prediction)

        return results
