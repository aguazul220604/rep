const API = {
  process: "process_table",
  inspect: "inspect_columns",
  metadata: "process_metadata",
  export: "export_xlsx",
  health: "health",
  deep: "analyze_column",
};

function apiUrl(path) {
  if (typeof getWebAppBackendUrl === "function") {
    try {
      return getWebAppBackendUrl(path);
    } catch {
      return path;
    }
  }
  return path;
}

const $ = (sel, root = document) => root.querySelector(sel);

function setChecked(sel, value) {
  const el = $(sel);
  if (el) el.checked = value;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML;
}
const pctText = (p) => (Number(p || 0) * 100).toFixed(2) + "%";

const EVIDENCE_LABELS = {
  name: "Nombre",
  content: "Contenido",
  both: "Nombre + contenido",
};
const isEmptyContent = (c) => Number(c.sample_size || 0) === 0;
function evidenceText(c) {
  return EVIDENCE_LABELS[c.evidence] || "—";
}

const CONFIDENCE_LABELS = { high: "Alta", medium: "Media", low: "Baja" };
function confidenceBand(col) {
  if (!col.is_pii) return null;
  const prob = Number(col.pii_probability || 0);
  const noContent = isEmptyContent(col);
  const both = col.evidence === "both";
  let level = "low";
  if (!noContent && both && prob >= 0.85 && !col.needs_review) {
    level = "high";
  } else if (!noContent && prob >= 0.6) {
    level = "medium";
  }
  const cls =
    level === "high" ? "danger" : level === "medium" ? "warn" : "muted";
  const notes = [];
  if (noContent) notes.push("sin contenido");
  if (col.needs_review) notes.push("requiere revisión");
  const title =
    "Confianza estimada combinando nombre y contenido " +
    "(Alta ≥85%, Media 60–85%, Baja <60% o sin contenido). " +
    "Es orientativa y no sustituye la revisión humana." +
    (notes.length ? ` ${notes.join("; ")}.` : "");
  return { level, label: CONFIDENCE_LABELS[level], cls, title };
}

function nameOnlyConfidenceBand(col) {
  if (!col.is_pii) return null;
  const prob = Number(col.pii_probability || 0);
  let level = "low";
  if (prob >= 0.85 && !col.needs_review) level = "high";
  else if (prob >= 0.6) level = "medium";
  const cls =
    level === "high" ? "danger" : level === "medium" ? "warn" : "muted";
  return {
    level,
    label: CONFIDENCE_LABELS[level],
    cls,
    title:
      "Confianza estimada a partir del nombre de la columna. " +
      "Es orientativa y no sustituye la revisión humana.",
  };
}

const ALL = "__all";
const state = {
  columns: [],
  busy: false,
  filter: "",
  onlyPii: false,
  onlyReview: false,
  onlyEmpty: false,
  showAudit: false,
  activeFile: ALL,
  entity: "",
  classification: "",
  maxUploadMb: null,
  maxReadRows: null,
  deepFile: "",
  deepColumn: "",
  deepBusy: false,
  deepResult: null,
  deepError: null,
  analysisMode: "columns",
  contentAnalysis: false,
  defaultContentAnalysis: false,
  metadataFiles: [],
  metadataColumns: {},
  metadataRows: [],
  metadataOrder: [],
  inspectBusy: false,
};

async function readJson(res) {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

function isMetadataMode() {
  return state.analysisMode === "metadata";
}

function currentRows() {
  return isMetadataMode() ? state.metadataRows : state.columns;
}

function applyModeVisibility() {
  const metadata = isMetadataMode();
  const content = Boolean(state.contentAnalysis) && !metadata;

  document.body.classList.toggle("mode-metadata", metadata);
  document.body.classList.toggle("content-on", content);
  document.body.classList.toggle("name-only", !content);

  const contentBox = $("#in-content-analysis");
  if (contentBox) {
    contentBox.checked = content;
    contentBox.disabled = metadata;
  }

  const deep = $("#article-deep");
  if (deep) deep.hidden = !content;

  const note = $("#note-file");
  if (note) {
    note.textContent = metadata
      ? "Se analiza únicamente el nombre de cada valor de la columna de metadata seleccionada."
      : content
        ? "Se analizan los nombres y el contenido de las columnas."
        : "Se analizan únicamente los nombres de las columnas.";
  }

  const modeNote = $("#analysis-mode-note");
  if (modeNote) {
    modeNote.textContent = metadata
      ? "Seleccione una columna por archivo; cada valor se evalúa como un campo independiente."
      : "Evalúa cada columna del archivo cargado usando solo su nombre.";
  }

  const contentNote = $("#content-analysis-note");
  if (contentNote) {
    contentNote.textContent = metadata
      ? "No aplica en modo metadata: solo se analizan nombres."
      : content
        ? "Activado: se analiza una muestra del contenido de cada columna."
        : "Desactivado: la evaluación usa únicamente los nombres de las columnas.";
  }

  const filterLabel = $("#filter-label");
  if (filterLabel) {
    filterLabel.textContent = metadata
      ? "Filtrar resultados"
      : "Filtrar por archivo";
  }
  const filterInput = $("#in-filter");
  if (filterInput) {
    filterInput.placeholder = metadata
      ? "Filtrar por elemento, entidad, dataset…"
      : "Filtrar por nombre de archivo…";
  }
  const filterNote = $("#filter-note");
  if (filterNote) {
    filterNote.textContent = metadata
      ? "Busca en los valores de metadata, el elemento analizado, la entidad o el dataset."
      : "Filtro por nombre de archivo. Si el campo se deja vacío, se muestran todos los archivos.";
  }

  const onlyEmpty = $("#in-only-empty");
  if (onlyEmpty) {
    const label = onlyEmpty.closest("label");
    if (label) label.hidden = !content;
  }

  const selectors = $("#metadataColumnSelectors");
  if (selectors && !metadata) selectors.hidden = true;

  const legend = $("#tableLegend");
  if (legend) {
    if (metadata) {
      legend.innerHTML =
        "Cada fila representa un valor de la columna de metadata seleccionada. " +
        "La predicción se basa únicamente en el nombre del elemento; " +
        "la probabilidad exacta y la fuente están disponibles con “Mostrar detalles de auditoría”.";
    } else if (!content) {
      legend.innerHTML =
        "La predicción se basa únicamente en el nombre de cada columna; " +
        "no se lee el contenido. Las probabilidades exactas están disponibles con " +
        "“Mostrar detalles de auditoría”.";
    } else {
      legend.innerHTML =
        'Las definiciones de cada columna se encuentran en ' +
        '<a href="#interpretabilidad">Interpretabilidad</a>, dentro de ' +
        '"Funcionalidad". La columna "Confianza PII" es una banda orientativa; ' +
        'para visualizar el porcentaje exacto, la evidencia y la muestra, ' +
        'habilite "Mostrar detalles de auditoría" en dicha sección.';
    }
  }
}

async function loadConfig() {
  try {
    const res = await fetch(apiUrl(API.health));
    const data = await readJson(res);
    const max = data.max_upload_mb;
    state.maxUploadMb = typeof max === "number" && max > 0 ? max : null;
    const rows = data.max_read_rows;
    state.maxReadRows = typeof rows === "number" && rows > 0 ? rows : null;
    const def = data.default_content_analysis;
    state.defaultContentAnalysis = def === 1 || def === true || def === "1";
    state.contentAnalysis = state.defaultContentAnalysis;
  } catch {
    state.maxUploadMb = null;
    state.maxReadRows = null;
  }
  applyModeVisibility();
}

async function apiProcess(formData) {
  const res = await fetch(apiUrl(API.process), {
    method: "POST",
    body: formData,
  });
  const data = await readJson(res);
  if (!res.ok || data.status !== "success") {
    const fallback =
      res.status === 413
        ? "El archivo es demasiado grande para el servidor o el proxy (HTTP 413)."
        : "HTTP " +
          res.status +
          (res.statusText ? " " + res.statusText : "") +
          " en " +
          API.process;
    throw new Error(data.message || fallback);
  }
  return data;
}

async function apiMetadata(formData) {
  const res = await fetch(apiUrl(API.metadata), {
    method: "POST",
    body: formData,
  });
  const data = await readJson(res);
  if (!res.ok || data.status !== "success") {
    const fallback =
      res.status === 413
        ? "El archivo es demasiado grande para el servidor o el proxy (HTTP 413)."
        : "HTTP " +
          res.status +
          (res.statusText ? " " + res.statusText : "") +
          " en " +
          API.metadata;
    throw new Error(data.message || fallback);
  }
  return data;
}

async function inspectMetadataFiles(files) {
  if (!files || !files.length) {
    state.metadataFiles = [];
    state.metadataColumns = {};
    renderMetadataSelectors();
    return;
  }
  const fd = new FormData();
  files.forEach((file) => fd.append("file", file));
  state.inspectBusy = true;
  renderMetadataSelectors();
  try {
    const res = await fetch(apiUrl(API.inspect), {
      method: "POST",
      body: fd,
    });
    const data = await readJson(res);
    if (!res.ok || data.status !== "success") {
      throw new Error(
        data.message ||
          "HTTP " + res.status + " en " + API.inspect,
      );
    }
    const previous = state.metadataColumns || {};
    const next = {};
    state.metadataFiles = Array.isArray(data.files) ? data.files : [];
    for (const entry of state.metadataFiles) {
      const prev = previous[entry.source_index];
      const suggested = entry.suggested_column || "";
      next[entry.source_index] =
        prev && entry.columns.includes(prev) ? prev : suggested;
    }
    state.metadataColumns = next;
  } finally {
    state.inspectBusy = false;
    renderMetadataSelectors();
  }
}

function getFiles() {
  const s = new Set(currentRows().map((c) => c.source_file || ""));
  return Array.from(s)
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b, "es"));
}
const isMulti = () => getFiles().length > 1;
const isIndexView = () => isMulti() && state.activeFile === ALL;

function getEntities() {
  const s = new Set();
  for (const c of currentRows()) if (c.is_pii && c.entity) s.add(c.entity);
  return Array.from(s).sort((a, b) => a.localeCompare(b, "es"));
}

function tableScopeFile() {
  if (state.activeFile !== ALL) return state.activeFile;
  const files = getFiles();
  if (files.length === 1) return files[0];
  return null;
}

function getEntitiesForScope() {
  const scope = tableScopeFile();
  const s = new Set();
  for (const c of currentRows()) {
    if (scope && (c.source_file || "") !== scope) continue;
    if (c.is_pii && c.entity) s.add(c.entity);
  }
  return Array.from(s).sort((a, b) => a.localeCompare(b, "es"));
}

function getClassificationsForScope() {
  const scope = tableScopeFile();
  const m = new Map();
  for (const c of currentRows()) {
    if (scope && (c.source_file || "") !== scope) continue;
    const key = c.information_classification;
    if (!key) continue;
    m.set(key, c.information_classification_label || key);
  }
  return Array.from(m.entries()).sort((a, b) =>
    String(a[1]).localeCompare(String(b[1]), "es"),
  );
}

function deepFileKey(col) {
  if (!col) return "";
  if (col.source_index !== null && col.source_index !== undefined) {
    return `idx:${col.source_index}`;
  }
  return `file:${col.source_file || ""}`;
}

function getDeepFileEntries() {
  const entries = new Map();
  for (const col of state.columns) {
    const key = deepFileKey(col);
    if (!key || key === "file:") continue;
    if (!entries.has(key)) {
      entries.set(key, {
        key,
        file: col.source_file || "archivo",
        index:
          col.source_index === null || col.source_index === undefined
            ? null
            : col.source_index,
      });
    }
  }
  return Array.from(entries.values()).sort((a, b) =>
    String(a.file).localeCompare(String(b.file), "es"),
  );
}

function getDeepColumns(fileKey) {
  return state.columns.filter((col) => deepFileKey(col) === fileKey);
}

function findDeepFile(entry) {
  const input = $("#datasetFile");
  const files = input && input.files ? Array.from(input.files) : [];
  if (entry.index !== null && entry.index !== undefined && files[entry.index]) {
    return files[entry.index];
  }
  return files.find((file) => file.name === entry.file) || null;
}

function showDeepError(message) {
  const box = $("#deepError");
  const text = $("#deepError-text");
  if (!box || !text) return;
  if (!message) {
    box.hidden = true;
    text.textContent = "—";
    return;
  }
  text.textContent = message;
  box.hidden = false;
}

function deepEmptyMessage(column) {
  return (
    `La columna “${column.name}” no contiene valores (está vacía o la totalidad de sus valores son nulos). ` +
    "El análisis profundo requiere al menos un dato para calcular la cobertura y la distribución de entidades."
  );
}

function renderDeepControls() {
  const tableSelect = $("#deep-table");
  const columnSelect = $("#deep-column");
  const button = $("#btn-deep-analyze");
  if (!tableSelect || !columnSelect || !button) return;

  const entries = getDeepFileEntries();
  const previousFile = state.deepFile;
  const previousColumn = state.deepColumn;

  tableSelect.innerHTML =
    '<option value="">Seleccione una tabla…</option>' +
    entries
      .map(
        (entry) =>
          `<option value="${escapeHtml(entry.key)}">${escapeHtml(entry.file)}</option>`,
      )
      .join("");

  let fileKey = state.deepFile;
  if (!entries.some((entry) => entry.key === fileKey)) {
    const active =
      state.activeFile !== ALL
        ? entries.find((entry) => entry.file === state.activeFile)
        : null;
    if (active) fileKey = active.key;
    else if (entries.length === 1) fileKey = entries[0].key;
    else fileKey = "";
  }
  state.deepFile = fileKey;
  tableSelect.value = fileKey;

  const columns = getDeepColumns(fileKey);
  if (previousFile && previousFile !== fileKey) {
    state.deepResult = null;
    state.deepError = null;
    showDeepError(null);
  }

  columnSelect.innerHTML =
    '<option value="">Seleccione una columna…</option>' +
    columns
      .map(
        (col) =>
          `<option value="${escapeHtml(col.name)}">${escapeHtml(col.name)}</option>`,
      )
      .join("");

  let columnName = state.deepColumn;
  if (!columns.some((col) => col.name === columnName)) columnName = "";
  state.deepColumn = columnName;
  columnSelect.value = columnName;

  if (previousColumn && previousColumn !== columnName) {
    state.deepResult = null;
    state.deepError = null;
    showDeepError(null);
  }

  tableSelect.disabled = state.deepBusy || entries.length === 0;
  columnSelect.disabled = state.deepBusy || !fileKey;
  button.disabled = state.deepBusy || !fileKey || !columnName;
  button.textContent = state.deepBusy
    ? "Analizando…"
    : "Analizar contenido completo";

  renderDeepResult();
}

function renderDeepResult() {
  const container = $("#deep-result");
  if (!container) return;

  if (state.deepBusy) {
    container.innerHTML =
      '<p class="field-note">Analizando la totalidad de la columna seleccionada…</p>';
    return;
  }

  if (!state.deepResult) {
    const hasColumns = state.columns.length > 0;
    container.innerHTML = `<p class="field-note">${
      hasColumns
        ? "Seleccione una tabla y una columna para ejecutar el análisis profundo."
        : "Cargue y analice al menos una tabla para habilitar el análisis profundo."
    }</p>`;
    return;
  }

  const result = state.deepResult;
  const countText = (value) =>
    Number(value || 0).toLocaleString("es-MX");
  const entities = (
    Array.isArray(result.entities) ? result.entities : []
  ).filter((entity) => Number(entity.distribution_rate || 0) > 0);
  const rows = entities
    .map((entity) => {
      const classification =
        entity.information_classification_label ||
        `Clasificación ${entity.classification ?? "—"}`;
      return (
        `<tr>` +
        `<td><strong>${escapeHtml(entity.entity)}</strong>${
          entity.used_validator
            ? ' <span class="status-badge success">Validador</span>'
            : ""
        }</td>` +
        `<td>${escapeHtml(classification)}</td>` +
        `<td>${pctText(entity.distribution_rate)}</td>` +
        `<td>${pctText(entity.coverage_rate)}</td>` +
        `</tr>`
      );
    })
    .join("");

  const warnings = [];
  if (result.truncated) {
    warnings.push(
      "Se alcanzó el tope de filas configurado; los porcentajes corresponden a las filas escaneadas.",
    );
  }
  if (Array.isArray(result.warnings)) warnings.push(...result.warnings);

  const main = result.main || null;
  const mainIsPii = Boolean(main && main.isPii);
  const mainEntity = main && main.entity ? escapeHtml(main.entity) : "—";
  const mainEvidence =
    main && main.evidence
      ? escapeHtml(EVIDENCE_LABELS[main.evidence] || main.evidence)
      : "—";
  const mainClassification =
    main && main.classification ? escapeHtml(main.classification) : "—";
  const deepIsZero = Number(result.pii_rate || 0) === 0;
  const mainBlock = main
    ? `<div class="deep-verdict">` +
      `<span class="deep-verdict-label">Veredicto principal (nombre + contenido)</span>` +
      `<div class="deep-verdict-body">` +
      `<span class="status-badge ${mainIsPii ? "danger" : "success"}">${mainIsPii ? "PII" : "NO PII"}</span>` +
      `<span>Entidad: <strong>${mainEntity}</strong></span>` +
      `<span>Fuente: ${mainEvidence}</span>` +
      `<span>Clasificación: ${mainClassification}</span>` +
      `</div>` +
      (mainIsPii && deepIsZero
        ? `<p class="field-note">El análisis profundo no encontró coincidencias de contenido. La clasificación principal se basó en el nombre de la columna${
            main.entity ? ` (entidad: ${mainEntity})` : ""
          }; este resultado no cambia el veredicto principal.</p>`
        : "") +
      `</div>`
    : "";

  const kpis = [
    ["Cobertura PII", pctText(result.pii_rate)],
    ["Celdas no vacías", countText(result.non_empty_values)],
    [
      "Sin PII / Otro",
      `${countText(result.non_pii_values)} (${pctText(result.non_pii_rate)})`,
    ],
    ["Filas escaneadas", countText(result.rows_scanned)],
    ["Valores únicos", countText(result.unique_values)],
    ["Vacías / nulas", countText(result.empty_values)],
  ]
    .map(
      ([label, value]) =>
        `<div class="deep-kpi"><span class="deep-kpi-label">${escapeHtml(label)}</span><span class="deep-kpi-value">${escapeHtml(value)}</span></div>`,
    )
    .join("");

  container.innerHTML =
    `<div class="deep-result-head">` +
    `<p class="dl-title">${escapeHtml(result.column)}</p>` +
    `<p class="dl-sub mono">${escapeHtml(result.source_file || "")}</p>` +
    `</div>` +
    `<div class="deep-kpis">${kpis}</div>` +
    mainBlock +
    (warnings.length
      ? `<div class="alert alert-warn deep-warning">${warnings
          .map((warning) => escapeHtml(warning))
          .join("<br />")}</div>`
      : "") +
    (entities.length
      ? `<div class="table-wrap"><table class="table deep-table" aria-label="Distribución de entidades PII">` +
        `<thead><tr><th>Entidad</th><th>Clasificación</th><th>% celdas PII</th><th>Cobertura</th></tr></thead>` +
        `<tbody>${rows}</tbody></table></div>`
      : '<p class="field-note">No se detectó PII por contenido en las filas escaneadas.</p>') +
    `<p class="field-note">La distribución suma 100% de las celdas PII. La cobertura se calcula sobre celdas no vacías; una celda puede coincidir con más de una entidad.</p>`;
}

function resetDeep() {
  state.deepFile = "";
  state.deepColumn = "";
  state.deepBusy = false;
  state.deepResult = null;
  state.deepError = null;
  showDeepError(null);
  renderDeepControls();
}

function handleDeepTableChange(event) {
  state.deepFile = event.target.value;
  state.deepColumn = "";
  state.deepResult = null;
  state.deepError = null;
  showDeepError(null);
  renderDeepControls();
}

function handleDeepColumnChange(event) {
  state.deepColumn = event.target.value;
  state.deepResult = null;
  state.deepError = null;
  showDeepError(null);
  renderDeepControls();
}

async function handleDeepAnalyze() {
  if (state.deepBusy) return;
  showDeepError(null);

  const entries = getDeepFileEntries();
  const entry = entries.find((item) => item.key === state.deepFile);
  const columns = getDeepColumns(state.deepFile);
  const column = columns.find((item) => item.name === state.deepColumn);

  if (!entry || !column) {
    showDeepError("Seleccione una tabla y una columna.");
    return;
  }

  if (isEmptyContent(column)) {
    state.deepResult = null;
    showDeepError(deepEmptyMessage(column));
    return;
  }

  const file = findDeepFile(entry);
  if (!file) {
    showDeepError(
      "El archivo seleccionado ya no está disponible. Vuelva a cargarlo.",
    );
    return;
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("column", column.name);
  formData.append("source_file", column.source_file || entry.file || file.name);
  if (entry.index !== null && entry.index !== undefined) {
    formData.append("source_index", String(entry.index));
  }

  state.deepBusy = true;
  state.deepResult = null;
  state.deepError = null;
  renderDeepControls();

  try {
    const response = await fetch(apiUrl(API.deep), {
      method: "POST",
      body: formData,
    });
    const data = await readJson(response);
    if (!response.ok || data.status !== "success") {
      throw new Error(
        data.message ||
          `HTTP ${response.status} en ${API.deep}`,
      );
    }
    state.deepResult = data.column_analysis || null;
    if (!state.deepResult) {
      throw new Error("La respuesta no incluyó resultados del análisis.");
    }
    state.deepResult.main = {
      isPii: Boolean(column.is_pii),
      entity: column.entity || "",
      evidence: column.evidence || "",
      probability: column.pii_probability,
      classification: column.information_classification_label || "",
    };
  } catch (error) {
    const raw = error && error.message ? error.message : "";
    if (isEmptyContent(column)) {
      state.deepError = deepEmptyMessage(column);
    } else if (raw === "Failed to fetch") {
      state.deepError =
        "No fue posible establecer conexión con el servidor para ejecutar el análisis profundo. Intente nuevamente.";
    } else {
      state.deepError =
        raw || "Se produjo un error no identificado en el análisis profundo.";
    }
    showDeepError(state.deepError);
  } finally {
    state.deepBusy = false;
    renderDeepControls();
  }
}

function topEntities(cols, n = 2) {
  const m = new Map();
  for (const c of cols)
    if (c.is_pii && c.entity) m.set(c.entity, (m.get(c.entity) || 0) + 1);
  return Array.from(m.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);
}

function fileSummary(file) {
  const rows = currentRows().filter((c) => (c.source_file || "") === file);
  const pii = rows.filter((c) => c.is_pii).length;
  const pct = rows.length ? Math.round((pii / rows.length) * 100) : 0;
  return { file, total: rows.length, pii, pct, top: topEntities(rows) };
}

function visibleColumns() {
  const q = state.filter.trim().toLowerCase();
  const scoped = tableScopeFile() !== null;
  return state.columns.filter((c) => {
    if (state.activeFile !== ALL && (c.source_file || "") !== state.activeFile)
      return false;
    if (state.onlyPii && !c.is_pii) return false;
    if (state.onlyReview && !c.needs_review) return false;
    if (state.onlyEmpty && Number(c.sample_size || 0) !== 0) return false;
    if (state.entity && (c.entity || "") !== state.entity) return false;
    if (
      state.classification &&
      (c.information_classification || "") !== state.classification
    )
      return false;
    if (!q || scoped) return true;
    return String(c.source_file ?? "")
      .toLowerCase()
      .includes(q);
  });
}

function visibleMetadataRows() {
  const q = state.filter.trim().toLowerCase();
  return state.metadataRows.filter((row) => {
    if (state.activeFile !== ALL && (row.source_file || "") !== state.activeFile)
      return false;
    if (state.onlyPii && !row.is_pii) return false;
    if (state.onlyReview && !row.needs_review) return false;
    if (state.entity && (row.entity || "") !== state.entity) return false;
    if (
      state.classification &&
      (row.information_classification || "") !== state.classification
    )
      return false;
    if (!q) return true;
    const haystack = Object.entries(row)
      .filter(([, value]) => typeof value === "string")
      .map(([, value]) => value)
      .join(" ")
      .toLowerCase();
    return haystack.includes(q);
  });
}

function metadataConfidenceBand(row) {
  if (!row.is_pii) return null;
  const prob = Number(row.pii_probability || 0);
  let level = "low";
  if (prob >= 0.85 && !row.needs_review) level = "high";
  else if (prob >= 0.6) level = "medium";
  const cls =
    level === "high" ? "danger" : level === "medium" ? "warn" : "muted";
  return {
    level,
    label: CONFIDENCE_LABELS[level],
    cls,
    title:
      "Confianza estimada a partir del nombre del elemento. " +
      "Es orientativa y no sustituye la revisión humana.",
  };
}

function renderMeta() {
  const rows = currentRows();
  const total = rows.length;
  const pii = rows.filter((c) => c.is_pii).length;
  const review = rows.filter((c) => c.needs_review).length;
  const pct = total ? Math.round((pii / total) * 100) : 0;
  const unit = isMetadataMode() ? "filas" : "columnas";
  $("#resultsMeta").textContent =
    `${total} ${unit} · ${pii} PII (${pct}%)` +
    (review ? ` · ${review} requieren revisión` : "");
}

function renderKPIs() {
  const el = $("#kpiStrip");
  const rows = currentRows();
  const metadata = isMetadataMode();

  if (metadata) {
    if (!rows.length) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    const total = rows.length;
    const pii = rows.filter((c) => c.is_pii).length;
    const review = rows.filter((c) => c.needs_review).length;
    const entities = new Set(
      rows.filter((c) => c.is_pii && c.entity).map((c) => c.entity),
    ).size;
    const pct = total ? Math.round((pii / total) * 100) : 0;
    const files = getFiles().length;
    const top = topEntities(rows, 1)[0];
    el.hidden = false;
    el.innerHTML =
      `<div class="kpi"><span class="kpi-label">Filas</span><span class="kpi-value">${total}</span></div>` +
      `<div class="kpi"><span class="kpi-label">PII</span><span class="kpi-value">${pii} (${pct}%)</span></div>` +
      `<div class="kpi"><span class="kpi-label">Entidades</span><span class="kpi-value">${entities}</span></div>` +
      `<div class="kpi"><span class="kpi-label">Requieren revisión</span><span class="kpi-value">${review}</span></div>` +
      `<div class="kpi"><span class="kpi-label">Archivos</span><span class="kpi-value">${files}</span></div>` +
      `<div class="kpi"><span class="kpi-label">Entidad principal</span><span class="kpi-value">${escapeHtml(top ? `${top[0]} (${top[1]})` : "—")}</span></div>`;
    return;
  }

  if (!isIndexView()) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const total = rows.length;
  const pii = rows.filter((c) => c.is_pii).length;
  const review = rows.filter((c) => c.needs_review).length;
  const empty = rows.filter(isEmptyContent).length;
  const critical = rows.filter((c) =>
    ["confidential_pii", "sensitive_pii", "restricted"].includes(
      c.information_classification,
    ),
  ).length;
  const pct = total ? Math.round((pii / total) * 100) : 0;
  const files = getFiles().length;
  const top = topEntities(rows, 1)[0];
  el.hidden = false;
  el.innerHTML =
    `<div class="kpi"><span class="kpi-label">Columnas</span><span class="kpi-value">${total}</span></div>` +
    `<div class="kpi"><span class="kpi-label">PII</span><span class="kpi-value">${pii} (${pct}%)</span></div>` +
    `<div class="kpi"><span class="kpi-label">PII crítico</span><span class="kpi-value">${critical}</span></div>` +
    `<div class="kpi"><span class="kpi-label">Requieren revisión</span><span class="kpi-value">${review}</span></div>` +
    (state.contentAnalysis
      ? `<div class="kpi"><span class="kpi-label">Sin contenido</span><span class="kpi-value">${empty}</span></div>`
      : "") +
    `<div class="kpi"><span class="kpi-label">Archivos</span><span class="kpi-value">${files}</span></div>` +
    `<div class="kpi"><span class="kpi-label">Entidad principal</span><span class="kpi-value">${escapeHtml(top ? `${top[0]} (${top[1]})` : "—")}</span></div>`;
}

function renderTabs() {
  const el = $("#fileTabs");
  const rows = currentRows();
  if (!isMulti() && !(rows.length && getFiles().length)) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  if (!isMulti()) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = "";
  const mkTab = (key, label, sub) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tab";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(state.activeFile === key));
    b.dataset.file = key;
    b.title = label;
    if (key !== ALL) {
      const q = state.filter.trim().toLowerCase();
      if (q && String(label).toLowerCase().includes(q))
        b.classList.add("is-match");
    }
    b.innerHTML = `<span class="tab-name">${escapeHtml(label)}</span> <span class="tab-badge">${escapeHtml(sub)}</span>`;
    return b;
  };
  const totalPii = rows.filter((c) => c.is_pii).length;
  el.append(mkTab(ALL, "Todos", `${rows.length}·${totalPii}`));
  for (const f of getFiles()) {
    const s = fileSummary(f);
    el.append(mkTab(f, f, `${s.total}·${s.pii}`));
  }
}

function renderIndex() {
  const el = $("#indexGrid");
  if (!isIndexView()) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = "";
  const q = state.filter.trim().toLowerCase();
  const files = getFiles().filter(
    (f) => !q || String(f).toLowerCase().includes(q),
  );
  if (!files.length) {
    el.innerHTML = `<p class="transfer-result mono">Ningún archivo coincide con los criterios de filtrado.</p>`;
    return;
  }
  for (const f of files) {
    const s = fileSummary(f);
    const topTxt = s.top.length
      ? s.top.map(([e, n]) => `${e} (${n})`).join(", ")
      : "—";
    const card = document.createElement("button");
    card.type = "button";
    card.className = "file-card";
    card.dataset.file = f;
    card.setAttribute("aria-label", `Consultar la tabla de ${f}`);
    const unit = isMetadataMode() ? "filas" : "columnas";
    card.innerHTML =
      `<span class="file-card-name">${escapeHtml(f)}</span>` +
      `<span class="file-card-meta">${s.total} ${unit} · ${s.pii} PII (${s.pct}%)</span>` +
      `<span class="prob-track" aria-hidden="true"><span class="prob-fill" style="width:${s.pct}%"></span></span>` +
      `<span class="file-card-top">Principales entidades: ${escapeHtml(topTxt)}</span>` +
      `<span class="file-card-cta">Consultar tabla →</span>`;
    el.append(card);
  }
}

function renderFileHead() {
  const el = $("#fileViewHead");
  const showBack = isMulti() && state.activeFile !== ALL;
  if (!showBack) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const s = fileSummary(state.activeFile);
  const topTxt = s.top.length
    ? s.top.map(([e, n]) => `${e} (${n})`).join(", ")
    : "—";
  const unit = isMetadataMode() ? "filas" : "columnas";
  el.hidden = false;
  el.innerHTML =
    `<button type="button" class="link-back" data-file="${ALL}">← Regresar a todos los archivos</button>` +
    `<div><strong>${escapeHtml(s.file)}</strong>` +
    `<span class="file-card-meta"> · ${s.total} ${unit} · ${s.pii} PII (${s.pct}%) · Principales entidades: ${escapeHtml(topTxt)}</span></div>` +
    `<span class="prob-track" aria-hidden="true"><span class="prob-fill" style="width:${s.pct}%"></span></span>`;
}

function renderGlobalFilter() {
  const row = $("#globalFilterRow");
  if (!row) return;
  row.hidden = !(isMulti() && currentRows().length > 0);
}

function renderFileFilters() {
  const row = $("#fileFilterRow");
  if (!row) return;
  row.hidden = isIndexView() || currentRows().length === 0;
}

function renderEntityOptions() {
  const sel = $("#in-entity");
  const cur = state.entity;
  const scoped = getEntitiesForScope();
  sel.textContent = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "Todas las entidades";
  sel.append(all);
  for (const e of scoped) {
    const o = document.createElement("option");
    o.value = e;
    o.textContent = e;
    sel.append(o);
  }
  sel.value = cur && scoped.includes(cur) ? cur : "";
  state.entity = sel.value;
}

function renderClassificationOptions() {
  const sel = $("#in-classification");
  if (!sel) return;
  const cur = state.classification;
  const scoped = getClassificationsForScope();
  sel.textContent = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "Todas las clasificaciones";
  sel.append(all);
  for (const [value, label] of scoped) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    sel.append(o);
  }
  const values = scoped.map(([value]) => value);
  sel.value = cur && values.includes(cur) ? cur : "";
  state.classification = sel.value;
}

function renderTable() {
  const wrap = $("#tableWrap");
  const tb = $("#piiListBody");
  const info = $("#pagingInfo");
  const selFile = $("#selExportFileFmt");
  const rowFile = $("#fileExportRow");
  const tableHead = $("#tableFileHead");
  const metadataWrap = $("#metadataTableWrap");
  document.body.classList.toggle("show-audit", state.showAudit);
  const auditBox = $("#in-show-audit");
  if (auditBox) auditBox.checked = state.showAudit;

  if (isMetadataMode()) {
    if (wrap) wrap.hidden = true;
    if (isIndexView()) {
      if (metadataWrap) metadataWrap.hidden = true;
      if (selFile) selFile.hidden = true;
      if (rowFile) rowFile.hidden = true;
      if (tableHead) {
        tableHead.hidden = true;
        tableHead.textContent = "";
      }
      info.textContent =
        "Es necesario seleccionar un archivo para consultar su tabla completa.";
      return;
    }
    if (metadataWrap) metadataWrap.hidden = false;
    if (rowFile) rowFile.hidden = false;
    const visible = visibleMetadataRows();
    const inFile = isMulti() && state.activeFile !== ALL;
    const total = inFile
      ? fileSummary(state.activeFile).total
      : state.metadataRows.length;
    info.textContent = state.metadataRows.length
      ? `Se muestran ${visible.length} de ${total} filas${inFile ? ` en ${state.activeFile}` : ""}.`
      : "";
    if (selFile) selFile.hidden = state.metadataRows.length === 0;
    const nameEl = $("#fileExportName");
    if (nameEl) {
      nameEl.textContent = state.metadataRows.length
        ? `Tabla: ${inFile ? state.activeFile : "metadata"} · ${visible.length} de ${total} filas visibles`
        : "";
    }
    if (tableHead) {
      tableHead.hidden = true;
      tableHead.textContent = "";
    }
    renderMetadataTable();
    return;
  }

  if (metadataWrap) metadataWrap.hidden = true;
  if (wrap) wrap.hidden = false;

  // En índice Todos no hay tabla, solo cards.
  if (isIndexView()) {
    wrap.hidden = true;
    if (selFile) selFile.hidden = true;
    if (rowFile) rowFile.hidden = true;
    if (tableHead) {
      tableHead.hidden = true;
      tableHead.textContent = "";
    }
    info.textContent =
      "Es necesario seleccionar un archivo para consultar su tabla completa.";
    return;
  }
  wrap.hidden = false;
  if (rowFile) rowFile.hidden = false;
  tb.textContent = "";
  const rows = visibleColumns();
  const inFile = isMulti() && state.activeFile !== ALL;
  const singleFile = !isMulti() && getFiles().length === 1;
  const showExport = state.columns.length > 0;
  const nameEl = $("#fileExportName");
  if (selFile) selFile.hidden = !showExport;
  if (showExport) {
    const label = inFile
      ? state.activeFile
      : singleFile
        ? getFiles()[0]
        : "esta vista";
    if (nameEl) {
      const total = inFile
        ? fileSummary(state.activeFile).total
        : state.columns.length;
      nameEl.textContent = `Tabla: ${label} · ${rows.length} de ${total} columnas visibles`;
    }
  } else if (nameEl) {
    nameEl.textContent = "";
  }
  info.textContent = state.columns.length
    ? `Se muestran ${rows.length} de ${inFile ? fileSummary(state.activeFile).total : state.columns.length} columnas${state.activeFile !== ALL ? ` en ${state.activeFile}` : ""}.`
    : "";
  if (tableHead) {
    const fileLabel = singleFile ? getFiles()[0] : "";
    tableHead.hidden = !fileLabel;
    tableHead.innerHTML = fileLabel
      ? `<span class="table-file-label">Archivo</span> ${escapeHtml(fileLabel)}`
      : "";
  }
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 9;
    td.textContent = state.columns.length
      ? "Ninguna columna coincide con los filtros aplicados"
      : "Sin información analizada";
    tr.append(td);
    tb.append(tr);
    return;
  }
  for (const col of rows) {
    const tr = document.createElement("tr");
    if (col.is_pii) tr.classList.add("pii-row");
    if (col.needs_review) tr.classList.add("needs-review");
    if (state.contentAnalysis && isEmptyContent(col))
      tr.classList.add("empty-content");
    const ent = col.entity ?? "—";
    const entP =
      col.entity_probability === null || col.entity_probability === undefined
        ? "—"
        : pctText(col.entity_probability);
    const reviewTitle = state.contentAnalysis
      ? "Confianza baja o ausencia de contenido; requiere revisión humana"
      : "Confianza baja o desacuerdo entre modelo y reglas; requiere revisión humana";
    const reviewCell = col.needs_review
      ? `<span class="status-badge warn" title="${reviewTitle}">Revisar</span>`
      : `<span class="muted">—</span>`;
    const sampleCell = !state.contentAnalysis
      ? `<span class="muted">—</span>`
      : isEmptyContent(col)
        ? `<span class="status-badge muted" title="Sin filas: la clasificación se basó únicamente en el nombre">Sin contenido</span>`
        : `<span class="prob-num">${Number(col.sample_size || 0)}</span>`;
    const band = state.contentAnalysis
      ? confidenceBand(col)
      : nameOnlyConfidenceBand(col);
    const probCell = state.showAudit
      ? `<span class="prob-num">${pctText(col.pii_probability)}</span>`
      : band
        ? `<span class="status-badge ${band.cls}" title="${escapeHtml(band.title)}">${band.label}</span>`
        : `<span class="muted">—</span>`;
    const clsKey = String(col.information_classification || "").replace(
      /[^a-z_]/gi,
      "",
    );
    const clsLabel = col.information_classification_label || "Sin clasificación";
    const clsBadge = clsKey
      ? `<span class="status-badge info-class-${clsKey}">${escapeHtml(clsLabel)}</span>`
      : `<span class="status-badge muted">Sin clasificación</span>`;
    const classificationCell =
      clsBadge +
      (col.classification_review
        ? ` <span class="status-badge warn is-audit-only" title="Clasificación sujeta a revisión">Revisar</span>`
        : "");
    tr.innerHTML =
      `<td><strong>${escapeHtml(col.name)}</strong></td>` +
      `<td><span class="status-badge ${col.is_pii ? "danger" : "success"}">${col.is_pii ? "PII" : "NO PII"}</span></td>` +
      `<td class="col-audit">${escapeHtml(ent)}</td>` +
      `<td>${classificationCell}</td>` +
      `<td>${reviewCell}</td>` +
      `<td>${probCell}</td>` +
      `<td class="col-audit">${escapeHtml(entP)}</td>` +
      `<td class="col-audit" title="Fuente de la decisión: nombre, contenido o ambos">${escapeHtml(evidenceText(col))}</td>` +
      `<td class="col-audit content-only">${sampleCell}</td>`;
    tb.append(tr);
  }
}

function metadataColumnNames() {
  if (state.metadataOrder && state.metadataOrder.length) {
    return state.metadataOrder;
  }
  const seen = new Set();
  for (const row of state.metadataRows) {
    for (const key of Object.keys(row)) {
      if (
        [
          "source_file",
          "source_index",
          "row_index",
          "element",
          "evaluable",
          "analysis_mode",
          "content_analysis",
          "model_version",
          "classification_version",
          "is_pii",
          "pii_probability",
          "entity",
          "entity_probability",
          "needs_review",
          "evidence",
          "source",
          "score_binary",
          "regex_entity",
          "entity_top1",
          "information_classification",
          "information_classification_group",
          "information_classification_label",
          "classification_review",
          "classification_reason",
        ].includes(key)
      ) {
        continue;
      }
      seen.add(key);
    }
  }
  return Array.from(seen);
}

function renderMetadataTable() {
  const head = $("#metadataTableHead");
  const body = $("#metadataTableBody");
  if (!head || !body) return;
  head.textContent = "";
  body.textContent = "";

  const metaCols = metadataColumnNames();
  const audit = Boolean(state.showAudit);
  const headerLabels = [
    "Archivo",
    ...metaCols,
    "Elemento",
    "PII",
    "Entidad",
    "Clasificación",
    "Confianza",
    "Revisión",
  ];
  if (audit) {
    headerLabels.push(
      "Prob. PII",
      "Prob. Entidad",
      "Evidencia",
      "Fuente",
      "Score nombre",
      "Entidad regex",
      "Entidad top1",
    );
  }

  const headerRow = document.createElement("tr");
  for (const label of headerLabels) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    headerRow.append(th);
  }
  head.append(headerRow);

  const rows = visibleMetadataRows();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = headerLabels.length;
    td.textContent = state.metadataRows.length
      ? "Ninguna fila coincide con los filtros aplicados"
      : "Sin información analizada";
    tr.append(td);
    body.append(tr);
    return;
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    if (row.is_pii) tr.classList.add("pii-row");
    if (row.needs_review) tr.classList.add("needs-review");

    const fileTd = document.createElement("td");
    fileTd.textContent = row.source_file || "";
    tr.append(fileTd);

    for (const column of metaCols) {
      const td = document.createElement("td");
      td.textContent = row[column] ?? "";
      tr.append(td);
    }

    const elementTd = document.createElement("td");
    if (row.evaluable === false || !row.element) {
      elementTd.innerHTML =
        '<span class="status-badge muted metadata-element-empty">Sin elemento</span>';
    } else {
      elementTd.innerHTML = `<strong>${escapeHtml(row.element)}</strong>`;
    }
    tr.append(elementTd);

    const piiTd = document.createElement("td");
    piiTd.innerHTML = `<span class="status-badge ${
      row.is_pii ? "danger" : "success"
    }">${row.is_pii ? "PII" : "NO PII"}</span>`;
    tr.append(piiTd);

    const entityTd = document.createElement("td");
    entityTd.textContent = row.entity || "—";
    tr.append(entityTd);

    const clsKey = String(row.information_classification || "").replace(
      /[^a-z_]/gi,
      "",
    );
    const clsLabel = row.information_classification_label || "Sin clasificación";
    const clsTd = document.createElement("td");
    clsTd.innerHTML = clsKey
      ? `<span class="status-badge info-class-${clsKey}">${escapeHtml(clsLabel)}</span>`
      : '<span class="status-badge muted">Sin clasificación</span>';
    tr.append(clsTd);

    const band = metadataConfidenceBand(row);
    const bandTd = document.createElement("td");
    bandTd.innerHTML = band
      ? `<span class="status-badge ${band.cls}" title="${escapeHtml(band.title)}">${band.label}</span>`
      : '<span class="muted">—</span>';
    tr.append(bandTd);

    const reviewTd = document.createElement("td");
    reviewTd.innerHTML = row.needs_review
      ? '<span class="status-badge warn">Revisar</span>'
      : '<span class="muted">—</span>';
    tr.append(reviewTd);

    if (audit) {
      const probPiiTd = document.createElement("td");
      probPiiTd.textContent = pctText(row.pii_probability);
      tr.append(probPiiTd);

      const probEntTd = document.createElement("td");
      probEntTd.textContent =
        row.entity_probability === null ||
        row.entity_probability === undefined
          ? "—"
          : pctText(row.entity_probability);
      tr.append(probEntTd);

      const evidenceTd = document.createElement("td");
      evidenceTd.textContent =
        row.evidence === "name"
          ? "Nombre"
          : row.evidence === "sin_elemento"
            ? "Sin elemento"
            : row.evidence || "—";
      tr.append(evidenceTd);

      const sourceTd = document.createElement("td");
      sourceTd.textContent = row.source || "—";
      tr.append(sourceTd);

      const scoreTd = document.createElement("td");
      scoreTd.textContent = pctText(row.score_binary);
      tr.append(scoreTd);

      const regexTd = document.createElement("td");
      regexTd.textContent = row.regex_entity || "—";
      tr.append(regexTd);

      const top1Td = document.createElement("td");
      top1Td.textContent = row.entity_top1 || "—";
      tr.append(top1Td);
    }

    body.append(tr);
  }
}

function renderMetadataSelectors() {
  const box = $("#metadataColumnSelectors");
  if (!box) return;
  box.textContent = "";

  if (!isMetadataMode() || !state.metadataFiles.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;

  for (const entry of state.metadataFiles) {
    const row = document.createElement("div");
    row.className = "metadata-column-row";

    const label = document.createElement("label");
    label.className = "field-label";
    label.textContent = `Columna de metadata · ${entry.source_file}`;

    const select = document.createElement("select");
    select.className = "select mono";
    select.dataset.index = String(entry.source_index);

    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Seleccione una columna…";
    select.append(blank);

    for (const column of entry.columns || []) {
      const option = document.createElement("option");
      option.value = column;
      option.textContent = column;
      select.append(option);
    }

    select.value = state.metadataColumns[entry.source_index] || "";
    select.disabled = state.inspectBusy;
    select.addEventListener("change", (event) => {
      state.metadataColumns[entry.source_index] = event.target.value;
    });

    row.append(label, select);
    box.append(row);
  }

  if (state.inspectBusy) {
    const note = document.createElement("p");
    note.className = "field-note";
    note.textContent = "Leyendo las columnas de los archivos seleccionados…";
    box.append(note);
  }
}

function renderReport() {
  const art = $("#article-report");
  if (!art) return;
  const rows = currentRows();
  art.hidden = !(isMulti() && rows.length > 0);
  if (art.hidden) return;
  const total = rows.length;
  const pii = rows.filter((c) => c.is_pii).length;
  const files = getFiles().length;
  const unit = isMetadataMode() ? "filas" : "columnas";
  const sum = $("#reportSummary");
  if (sum)
    sum.textContent = `${files} tablas · ${total} ${unit} · ${pii} PII → reporte_pii_global (CSV o Excel)`;
}

function renderAll() {
  applyModeVisibility();
  renderMetadataSelectors();
  renderMeta();
  renderKPIs();
  renderTabs();
  renderIndex();
  renderFileHead();
  renderGlobalFilter();
  renderFileFilters();
  renderEntityOptions();
  renderClassificationOptions();
  renderTable();
  renderReport();
  try {
    renderDeepControls();
  } catch (error) {
    console.error("No se pudo actualizar el análisis profundo", error);
  }
}

function showAlert(msg, kind = "error") {
  const box = $("#uploadError");
  if (!msg) {
    box.hidden = true;
    return;
  }
  box.className = "alert " + (kind === "warn" ? "alert-warn" : "alert-error");
  $("#uploadError-text").textContent = msg;
  box.hidden = false;
  $("#field-file").classList.toggle("is-error", kind === "error");
}

function setBusy(b) {
  state.busy = b;
  $("#btnProcess").disabled = b;
  $("#btnProcess").textContent = b ? "Analizando…" : "Analizar PII";
  const modeSelect = $("#in-analysis-mode");
  if (modeSelect) modeSelect.disabled = b;
  const contentBox = $("#in-content-analysis");
  if (contentBox) contentBox.disabled = b || isMetadataMode();
  const sf = $("#selExportFileFmt");
  if (sf) sf.disabled = b;
  const sr = $("#selReportFmt");
  if (sr) sr.disabled = b;
  const overlay = $("#loadingOverlay");
  if (overlay) overlay.hidden = !b;
  document.body.classList.toggle("is-busy", b);
  if (b) $("#upload-result").textContent = "Analizando columnas…";
}

function fmtSize(n) {
  const v = Number(n || 0);
  if (v < 1024) return `${v} B`;
  if (v < 1048576) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / 1048576).toFixed(1)} MB`;
}

function updateFileHint() {
  const input = $("#datasetFile"),
    hint = $("#fileHint"),
    list = $("#fileList");
  const files = input.files ? Array.from(input.files) : [];
  if (!files.length) {
    hint.textContent = "Ningún archivo seleccionado";
    hint.classList.remove("has-files");
    if (list) list.textContent = "";
    return;
  }
  hint.textContent =
    files.length === 1
      ? "1 archivo seleccionado"
      : `${files.length} archivos seleccionados`;
  hint.classList.add("has-files");
  if (!list) return;
  list.textContent = "";
  files.forEach((f, i) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "file-list-name";
    name.textContent = `${f.name} · ${fmtSize(f.size)}`;
    name.title = f.name;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "file-remove";
    rm.dataset.idx = String(i);
    rm.setAttribute("aria-label", `Eliminar ${f.name}`);
    rm.textContent = "×";
    li.append(name, rm);
    list.append(li);
  });
}

function removeUploadFile(idx) {
  const input = $("#datasetFile");
  const files = input.files ? Array.from(input.files) : [];
  if (idx < 0 || idx >= files.length) return;
  const dt = new DataTransfer();
  files.forEach((f, i) => {
    if (i !== idx) dt.items.add(f);
  });
  input.files = dt.files;
  updateFileHint();
  if (isMetadataMode()) {
    inspectMetadataFiles(Array.from(input.files)).catch((error) => {
      showAlert(
        error.message || "No fue posible leer las columnas de los archivos.",
      );
    });
  } else {
    state.metadataFiles = [];
    state.metadataColumns = {};
    renderMetadataSelectors();
  }
}

function clearAll() {
  $("#pii-form").reset();
  updateFileHint();
  showAlert(null);
  state.columns = [];
  state.metadataRows = [];
  state.metadataOrder = [];
  state.metadataFiles = [];
  state.metadataColumns = {};
  state.inspectBusy = false;
  state.analysisMode = "columns";
  state.contentAnalysis = state.defaultContentAnalysis;
  state.filter = "";
  state.onlyPii = false;
  state.onlyReview = false;
  state.onlyEmpty = false;
  state.showAudit = false;
  state.activeFile = ALL;
  state.entity = "";
  state.classification = "";
  resetDeep();
  $("#in-filter").value = "";
  const modeSelect = $("#in-analysis-mode");
  if (modeSelect) modeSelect.value = "columns";
  setChecked("#in-only-pii", false);
  setChecked("#in-only-review", false);
  setChecked("#in-only-empty", false);
  setChecked("#in-show-audit", false);
  $("#in-entity").value = "";
  $("#in-classification").value = "";
  const sf = $("#selExportFileFmt");
  if (sf) sf.value = "";
  const sr = $("#selReportFmt");
  if (sr) sr.value = "";
  $("#upload-result").textContent = "";
  $("#pagingInfo").textContent = "";
  renderAll();
  $("#datasetFile").focus();
}

function resetRunState() {
  state.activeFile = ALL;
  state.entity = "";
  state.classification = "";
  resetDeep();
  $("#in-filter").value = "";
  state.filter = "";
  setChecked("#in-only-pii", false);
  state.onlyPii = false;
  setChecked("#in-only-review", false);
  state.onlyReview = false;
  setChecked("#in-only-empty", false);
  state.onlyEmpty = false;
}

async function onSubmitMetadata(files) {
  setBusy(true);
  try {
    await inspectMetadataFiles(files);
    const mapping = {};
    for (let index = 0; index < files.length; index += 1) {
      const column =
        state.metadataColumns[index] || state.metadataColumns[String(index)];
      if (!column) {
        throw new Error(
          `Seleccione la columna de metadata para ${files[index].name}.`,
        );
      }
      mapping[String(index)] = column;
    }

    const fd = new FormData();
    files.forEach((file) => fd.append("file", file));
    fd.append("metadata_columns", JSON.stringify(mapping));

    const data = await apiMetadata(fd);
    state.metadataRows = data.results || [];
    state.metadataOrder = data.metadata_columns || [];
    state.metadataFiles = data.files || state.metadataFiles;
    state.columns = [];
    resetRunState();
    renderAll();

    const pii = state.metadataRows.filter((row) => row.is_pii).length;
    const nFiles = getFiles().length;
    $("#upload-result").textContent =
      nFiles > 1
        ? `${nFiles} archivos · ${state.metadataRows.length} filas · ${pii} PII. Seleccione una tarjeta para consultar el detalle.`
        : `${state.metadataRows.length} filas · ${pii} PII.`;

    if (data.warnings && data.warnings.length) {
      showAlert(data.warnings.join(" | "), "warn");
    }
  } catch (err) {
    $("#upload-result").textContent = "";
    showAlert(
      err.message ||
        "Se produjo un error no identificado al procesar la metadata.",
    );
  } finally {
    setBusy(false);
  }
}

async function onSubmit(e) {
  e.preventDefault();
  if (state.busy) return;
  showAlert(null);
  $("#upload-result").textContent = "";
  const input = $("#datasetFile");
  if (!input.files || !input.files.length) {
    showAlert("Es necesario seleccionar al menos un archivo CSV o Excel.");
    input.focus();
    return;
  }
  const files = Array.from(input.files);
  const totalBytes = files.reduce((acc, f) => acc + (f.size || 0), 0);
  if (state.maxUploadMb && totalBytes > state.maxUploadMb * 1024 * 1024) {
    showAlert(
      `El tamaño total (${fmtSize(totalBytes)}) supera el límite de ${state.maxUploadMb} MB.`,
    );
    return;
  }

  if (isMetadataMode()) {
    await onSubmitMetadata(files);
    return;
  }

  const fd = new FormData();
  files.forEach((f) => fd.append("file", f));
  fd.append("content_analysis", state.contentAnalysis ? "1" : "0");
  setBusy(true);
  try {
    const data = await apiProcess(fd);
    state.columns = data.columns_analysis || [];
    state.metadataRows = [];
    state.metadataOrder = [];
    resetRunState();
    renderAll();
    const pii = state.columns.filter((c) => c.is_pii).length;
    const nEmpty = state.contentAnalysis
      ? state.columns.filter(isEmptyContent).length
      : 0;
    const nFiles = getFiles().length;
    $("#upload-result").textContent =
      nFiles > 1
        ? `${nFiles} archivos · ${state.columns.length} columnas · ${pii} PII. Seleccione una tarjeta para consultar el detalle.`
        : `${state.columns.length} columnas · ${pii} PII.`;
    const msgs = [];
    if (data.warnings && data.warnings.length) msgs.push(...data.warnings);
    if (nEmpty)
      msgs.push(
        `${nEmpty} columnas no presentan contenido; su clasificación se basó únicamente en el nombre y requiere revisión.`,
      );
    if (msgs.length) showAlert(msgs.join(" | "), "warn");
  } catch (err) {
    $("#upload-result").textContent = "";
    showAlert(
      err.message || "Se produjo un error no identificado al procesar los archivos.",
    );
  } finally {
    setBusy(false);
  }
}

function toCsvRow(fields) {
  return fields
    .map((f) => {
      const v = String(f ?? "");
      return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
    })
    .join(",");
}

const EXPORT_HEADER_DEFAULT = [
  "Archivo",
  "Nombre de Columna",
  "Prediccion",
  "Clasificación",
  "Requiere revision",
];

const EXPORT_HEADER_AUDIT = [
  "Archivo",
  "Nombre de Columna",
  "Prediccion",
  "Probabilidad PII (%)",
  "Evidencia",
  "Requiere revision",
  "Entidad",
  "Probabilidad Entidad (%)",
  "Muestra",
  "Clasificación",
  "Grupo",
  "Revisión clasificación",
];

function exportHeader() {
  return state.showAudit ? EXPORT_HEADER_AUDIT : EXPORT_HEADER_DEFAULT;
}

function buildExportRows(cols) {
  return cols.map((c) => {
    const base = [
      c.source_file || "",
      c.name,
      c.is_pii ? "PII" : "NO PII",
      c.information_classification_label || "Sin clasificación",
      c.needs_review ? "Si" : "No",
    ];
    if (!state.showAudit) return base;
    return [
      c.source_file || "",
      c.name,
      c.is_pii ? "PII" : "NO PII",
      (Number(c.pii_probability || 0) * 100).toFixed(2),
      evidenceText(c) === "—" ? "" : evidenceText(c),
      c.needs_review ? "Si" : "No",
      c.entity ?? "",
      c.entity_probability === null || c.entity_probability === undefined
        ? ""
        : (Number(c.entity_probability) * 100).toFixed(2),
      Number(c.sample_size || 0),
      c.information_classification_label || "Sin clasificación",
      c.information_classification_group || "",
      c.classification_review ? "Si" : "No",
    ];
  });
}

function triggerDownload(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
}

function metadataExportHeader() {
  const columns = metadataColumnNames();
  const header = [
    "Archivo",
    ...columns,
    "Elemento",
    "Prediccion",
    "Entidad",
    "Clasificación",
    "Requiere revision",
  ];
  if (!state.showAudit) return header;
  return [
    ...header,
    "Probabilidad PII (%)",
    "Probabilidad Entidad (%)",
    "Evidencia",
    "Fuente",
    "Score nombre (%)",
    "Entidad regex",
    "Entidad top1",
    "Evaluable",
  ];
}

function buildMetadataExportRows(rows) {
  const columns = metadataColumnNames();
  return rows.map((row) => {
    const base = [
      row.source_file || "",
      ...columns.map((column) => row[column] ?? ""),
      row.element ?? "",
      row.is_pii ? "PII" : "NO PII",
      row.entity ?? "",
      row.information_classification_label || "Sin clasificación",
      row.needs_review ? "Si" : "No",
    ];
    if (!state.showAudit) return base;
    return [
      ...base,
      (Number(row.pii_probability || 0) * 100).toFixed(2),
      row.entity_probability === null || row.entity_probability === undefined
        ? ""
        : (Number(row.entity_probability) * 100).toFixed(2),
      row.evidence === "name"
        ? "Nombre"
        : row.evidence === "sin_elemento"
          ? "Sin elemento"
          : row.evidence || "",
      row.source || "",
      (Number(row.score_binary || 0) * 100).toFixed(2),
      row.regex_entity ?? "",
      row.entity_top1 ?? "",
      row.evaluable === false ? "No" : "Si",
    ];
  });
}

function exportPayload(rows) {
  if (isMetadataMode()) {
    return {
      header: metadataExportHeader(),
      rows: buildMetadataExportRows(rows),
    };
  }
  return { header: exportHeader(), rows: buildExportRows(rows) };
}

function downloadCsvWith(filename, header, rows) {
  const blob = new Blob(
    [
      "\ufeff" +
        [header, ...rows].map(toCsvRow).join("\r\n"),
    ],
    { type: "text/csv;charset=utf-8;" },
  );
  const url = URL.createObjectURL(blob);
  triggerDownload(url, filename);
  URL.revokeObjectURL(url);
}

async function downloadXlsxWith(filename, header, rows) {
  const res = await fetch(apiUrl(API.export), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, header, rows }),
  });
  if (!res.ok) {
    let msg = "";
    try {
      const d = await res.json();
      msg = d.message || "";
    } catch {}
    throw new Error(msg || "HTTP " + res.status + " en " + API.export);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  triggerDownload(url, filename);
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

async function downloadWithFormat(base, fmt, rows) {
  const payload = exportPayload(rows);
  if (fmt === "xlsx") {
    try {
      await downloadXlsxWith(`${base}.xlsx`, payload.header, payload.rows);
    } catch (err) {
      showAlert(
        (err.message || "No fue posible generar el archivo XLSX.") +
          " Se descargará el formato CSV.",
        "warn",
      );
      downloadCsvWith(`${base}.csv`, payload.header, payload.rows);
    }
  } else {
    downloadCsvWith(`${base}.csv`, payload.header, payload.rows);
  }
}

function safeBase(name) {
  return (
    String(name || "archivo")
      .replace(/\.(csv|xlsx|xls)$/i, "")
      .replace(/[\\/:*?"<>|]/g, "_")
      .slice(0, 80) || "archivo"
  );
}

function handleExportFile(fmt = "csv") {
  if (!currentRows().length) {
    showAlert("Es necesario analizar al menos una tabla antes de exportar.");
    return;
  }
  if (isIndexView()) return;
  const rows = isMetadataMode() ? visibleMetadataRows() : visibleColumns();
  if (!rows.length) {
    showAlert("No hay resultados que exportar con los filtros aplicados.", "warn");
    return;
  }
  const target =
    isMulti() && state.activeFile !== ALL
      ? state.activeFile
      : getFiles()[0] || "vista";
  downloadWithFormat(`analisis_pii_${safeBase(target)}`, fmt, rows);
}

function handleGenerateReport(fmt = "csv") {
  const rows = currentRows();
  if (!rows.length) {
    showAlert(
      "Es necesario analizar al menos una tabla antes de generar el reporte.",
    );
    return;
  }
  downloadWithFormat("reporte_pii_global", fmt, rows);
}

function resetFileFilters() {
  state.entity = "";
  state.classification = "";
  state.onlyPii = false;
  state.onlyReview = false;
  state.onlyEmpty = false;
  const se = $("#in-entity");
  if (se) se.value = "";
  const sc = $("#in-classification");
  if (sc) sc.value = "";
  setChecked("#in-only-pii", false);
  setChecked("#in-only-review", false);
  setChecked("#in-only-empty", false);
}

function setActiveFile(f) {
  state.activeFile = f || ALL;
  resetFileFilters();
  renderAll();
}

function boot() {
  $("#pii-form").addEventListener("submit", onSubmit);
  $("#btn-clear").addEventListener("click", clearAll);
  $("#datasetFile").addEventListener("change", () => {
    updateFileHint();
    const input = $("#datasetFile");
    const files = input.files ? Array.from(input.files) : [];
    if (isMetadataMode()) {
      inspectMetadataFiles(files).catch((error) => {
        showAlert(
          error.message || "No fue posible leer las columnas de los archivos.",
        );
      });
    } else {
      state.metadataFiles = [];
      state.metadataColumns = {};
      renderMetadataSelectors();
    }
  });

  const modeSelect = $("#in-analysis-mode");
  if (modeSelect) {
    modeSelect.value = state.analysisMode;
    modeSelect.addEventListener("change", (event) => {
      state.analysisMode = event.target.value;
      state.columns = [];
      state.metadataRows = [];
      state.metadataOrder = [];
      state.metadataFiles = [];
      state.metadataColumns = {};
      state.activeFile = ALL;
      resetFileFilters();
      resetDeep();
      applyModeVisibility();
      renderAll();
      const input = $("#datasetFile");
      const files = input && input.files ? Array.from(input.files) : [];
      if (isMetadataMode() && files.length) {
        inspectMetadataFiles(files).catch((error) => {
          showAlert(
            error.message || "No fue posible leer las columnas de los archivos.",
          );
        });
      }
    });
  }

  const contentBox = $("#in-content-analysis");
  if (contentBox) {
    contentBox.addEventListener("change", (event) => {
      if (isMetadataMode()) {
        event.target.checked = false;
        return;
      }
      state.contentAnalysis = event.target.checked;
      if (!state.contentAnalysis) {
        state.onlyEmpty = false;
        setChecked("#in-only-empty", false);
      }
      applyModeVisibility();
      renderAll();
    });
  }
  const fl = $("#fileList");
  if (fl)
    fl.addEventListener("click", (e) => {
      const b = e.target.closest("[data-idx]");
      if (b) removeUploadFile(Number(b.dataset.idx));
    });
  const drop = $(".file-drop");
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("dragover");
    }),
  );
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove("dragover");
    }),
  );
  drop.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      $("#datasetFile").files = e.dataTransfer.files;
      updateFileHint();
      const files = Array.from(e.dataTransfer.files);
      if (isMetadataMode()) {
        inspectMetadataFiles(files).catch((error) => {
          showAlert(
            error.message ||
              "No fue posible leer las columnas de los archivos.",
          );
        });
      } else {
        state.metadataFiles = [];
        state.metadataColumns = {};
        renderMetadataSelectors();
      }
    }
  });
  $("#in-filter").addEventListener("input", (e) => {
    state.filter = e.target.value;
    renderTabs();
    renderIndex();
    renderFileHead();
    renderTable();
  });
  const bindChange = (sel, fn) => {
    const el = $(sel);
    if (el) el.addEventListener("change", fn);
  };
  bindChange("#in-only-pii", (e) => {
    state.onlyPii = e.target.checked;
    renderTable();
  });
  bindChange("#in-only-review", (e) => {
    state.onlyReview = e.target.checked;
    renderTable();
  });
  bindChange("#in-only-empty", (e) => {
    state.onlyEmpty = e.target.checked;
    renderTable();
  });
  bindChange("#in-show-audit", (e) => {
    state.showAudit = e.target.checked;
    if (!state.showAudit) {
      state.entity = "";
      const se = $("#in-entity");
      if (se) se.value = "";
    }
    renderTable();
  });
  bindChange("#in-entity", (e) => {
    state.entity = e.target.value;
    renderTable();
  });
  bindChange("#in-classification", (e) => {
    state.classification = e.target.value;
    renderTable();
  });
  bindChange("#deep-table", handleDeepTableChange);
  bindChange("#deep-column", handleDeepColumnChange);
  const deepButton = $("#btn-deep-analyze");
  if (deepButton) deepButton.addEventListener("click", handleDeepAnalyze);
  const bindDownload = (sel, handler) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("change", () => {
      const fmt = el.value;
      el.value = "";
      if (fmt) handler(fmt);
    });
  };
  bindDownload("#selExportFileFmt", handleExportFile);
  bindDownload("#selReportFmt", handleGenerateReport);
  $("#fileTabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-file]");
    if (b) setActiveFile(b.dataset.file);
  });
  $("#indexGrid").addEventListener("click", (e) => {
    const b = e.target.closest("[data-file]");
    if (b) setActiveFile(b.dataset.file);
  });
  $("#fileViewHead").addEventListener("click", (e) => {
    const b = e.target.closest("[data-file]");
    if (b) setActiveFile(b.dataset.file);
  });
  const howto = $("#howtoDetails");
  if (howto) {
    const openHowto = () => {
      howto.open = true;
    };
    document
      .querySelectorAll('a[href="#interpretabilidad"]')
      .forEach((a) => a.addEventListener("click", openHowto));
    if (location.hash === "#interpretabilidad") openHowto();
    window.addEventListener("hashchange", () => {
      if (location.hash === "#interpretabilidad") openHowto();
    });
  }
  renderAll();
  updateFileHint();
  loadConfig();
}

document.addEventListener("DOMContentLoaded", boot);

