/* PII v1 con estilo Banamex. Vanilla JS, sin dependencias.
   1. CONFIG  2. HELPERS  3. STATE  4. API  5. RENDER  6. INSPECT  7. BOOT */
"use strict";

const API = { process: "process_table" };

function apiUrl(path) {
  if (typeof getWebAppBackendUrl === "function") {
    try { return getWebAppBackendUrl(path); } catch { return path; }
  }
  return path;
}

const $ = (sel, root = document) => root.querySelector(sel);

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML;
}
const pctText = (p) => (Number(p || 0) * 100).toFixed(2) + "%";

const ALL = "__all";
const state = { columns: [], busy: false, filter: "", onlyPii: false, activeFile: ALL, entity: "" };

async function apiProcess(formData) {
  const res = await fetch(apiUrl(API.process), { method: "POST", body: formData });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.status !== "success") {
    throw new Error(data.message || ("HTTP " + res.status + " en " + API.process));
  }
  return data;
}

function getFiles() {
  const s = new Set(state.columns.map((c) => c.source_file || ""));
  return Array.from(s).filter(Boolean).sort((a, b) => a.localeCompare(b, "es"));
}
const isMulti = () => getFiles().length > 1;
const isIndexView = () => isMulti() && state.activeFile === ALL;

function getEntities() {
  const s = new Set();
  for (const c of state.columns) if (c.is_pii && c.entity) s.add(c.entity);
  return Array.from(s).sort((a, b) => a.localeCompare(b, "es"));
}

function topEntities(cols, n = 2) {
  const m = new Map();
  for (const c of cols) if (c.is_pii && c.entity) m.set(c.entity, (m.get(c.entity) || 0) + 1);
  return Array.from(m.entries()).sort((a, b) => b[1] - a[1]).slice(0, n);
}

function fileSummary(file) {
  const cols = state.columns.filter((c) => (c.source_file || "") === file);
  const pii = cols.filter((c) => c.is_pii).length;
  const pct = cols.length ? Math.round((pii / cols.length) * 100) : 0;
  return { file, total: cols.length, pii, pct, top: topEntities(cols) };
}

function visibleColumns() {
  const q = state.filter.trim().toLowerCase();
  return state.columns.filter((c) => {
    if (state.activeFile !== ALL && (c.source_file || "") !== state.activeFile) return false;
    if (state.onlyPii && !c.is_pii) return false;
    if (state.entity && (c.entity || "") !== state.entity) return false;
    if (!q) return true;
    return [c.source_file, c.name, c.entity].some((v) =>
      String(v ?? "").toLowerCase().includes(q));
  });
}

function renderMeta() {
  const total = state.columns.length;
  const pii = state.columns.filter((c) => c.is_pii).length;
  $("#resultsMeta").textContent = `${total} columnas · ${pii} PII`;
}

function renderKPIs() {
  const el = $("#kpiStrip");
  if (!isIndexView()) { el.hidden = true; el.textContent = ""; return; }
  const total = state.columns.length;
  const pii = state.columns.filter((c) => c.is_pii).length;
  const pct = total ? Math.round((pii / total) * 100) : 0;
  const files = getFiles().length;
  const top = topEntities(state.columns, 1)[0];
  el.hidden = false;
  el.innerHTML =
    `<div class="kpi"><span class="kpi-label">Columnas</span><span class="kpi-value">${total}</span></div>` +
    `<div class="kpi"><span class="kpi-label">PII</span><span class="kpi-value">${pii} · ${pct}%</span></div>` +
    `<div class="kpi"><span class="kpi-label">Archivos</span><span class="kpi-value">${files}</span></div>` +
    `<div class="kpi"><span class="kpi-label">Top entidad</span><span class="kpi-value">${escapeHtml(top ? `${top[0]} (${top[1]})` : "—")}</span></div>`;
}

function renderTabs() {
  const el = $("#fileTabs");
  if (!isMulti() && !(state.columns.length && getFiles().length)) { el.hidden = true; el.textContent = ""; return; }
  if (!isMulti()) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = "";
  const mkTab = (key, label, sub) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tab";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(state.activeFile === key));
    b.dataset.file = key;
    b.innerHTML = `<span>${escapeHtml(label)}</span> <span class="tab-badge">${escapeHtml(sub)}</span>`;
    return b;
  };
  const totalPii = state.columns.filter((c) => c.is_pii).length;
  el.append(mkTab(ALL, "Todos", `${state.columns.length}·${totalPii}`));
  for (const f of getFiles()) {
    const s = fileSummary(f);
    el.append(mkTab(f, f, `${s.total}·${s.pii}`));
  }
}

function renderIndex() {
  const el = $("#indexGrid");
  if (!isIndexView()) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = "";
  for (const f of getFiles()) {
    const s = fileSummary(f);
    const topTxt = s.top.length ? s.top.map(([e, n]) => `${e} (${n})`).join(", ") : "—";
    const card = document.createElement("button");
    card.type = "button";
    card.className = "file-card";
    card.dataset.file = f;
    card.setAttribute("aria-label", `Ver tabla de ${f}`);
    card.innerHTML =
      `<span class="file-card-name">${escapeHtml(f)}</span>` +
      `<span class="file-card-meta">${s.total} cols · ${s.pii} PII (${s.pct}%)</span>` +
      `<span class="prob-track" aria-hidden="true"><span class="prob-fill" style="width:${s.pct}%"></span></span>` +
      `<span class="file-card-top">Top: ${escapeHtml(topTxt)}</span>` +
      `<span class="file-card-cta">Ver tabla →</span>`;
    el.append(card);
  }
}

function renderFileHead() {
  const el = $("#fileViewHead");
  const showBack = isMulti() && state.activeFile !== ALL;
  if (!showBack) { el.hidden = true; el.textContent = ""; return; }
  const s = fileSummary(state.activeFile);
  const topTxt = s.top.length ? s.top.map(([e, n]) => `${e} (${n})`).join(", ") : "—";
  el.hidden = false;
  el.innerHTML =
    `<button type="button" class="link-back" data-file="${ALL}">← Todos</button>` +
    `<div><strong>${escapeHtml(s.file)}</strong>` +
    `<span class="file-card-meta"> · ${s.total} cols · ${s.pii} PII (${s.pct}%) · Top: ${escapeHtml(topTxt)}</span></div>` +
    `<span class="prob-track" aria-hidden="true"><span class="prob-fill" style="width:${s.pct}%"></span></span>`;
}

function renderEntityOptions() {
  const sel = $("#in-entity");
  const cur = state.entity;
  sel.textContent = "";
  const all = document.createElement("option");
  all.value = ""; all.textContent = "Todas";
  sel.append(all);
  for (const e of getEntities()) {
    const o = document.createElement("option");
    o.value = e; o.textContent = e;
    sel.append(o);
  }
  sel.value = cur && getEntities().includes(cur) ? cur : "";
  state.entity = sel.value;
}

function renderTable() {
  const wrap = $("#tableWrap");
  const tb = $("#piiListBody");
  const info = $("#pagingInfo");
  const btnFile = $("#btnExportFile");
  // En índice Todos no hay tabla, solo cards.
  if (isIndexView()) {
    wrap.hidden = true;
    btnFile.hidden = true;
    info.textContent = "Selecciona un archivo para ver su tabla completa.";
    return;
  }
  wrap.hidden = false;
  tb.textContent = "";
  const rows = visibleColumns();
  const inFile = isMulti() && state.activeFile !== ALL;
  btnFile.hidden = !inFile;
  if (inFile) btnFile.textContent = `Exportar ${state.activeFile}`;
  info.textContent = state.columns.length
    ? `Mostrando ${rows.length} de ${inFile ? fileSummary(state.activeFile).total : state.columns.length} columnas${state.activeFile !== ALL ? ` en ${state.activeFile}` : ""}.`
    : "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = state.columns.length
      ? "Ninguna columna coincide con el filtro."
      : "Nada inspeccionado todavía. Cuando lo haya, se numera aquí.";
    tr.append(td); tb.append(tr);
    return;
  }
  const showFile = state.activeFile === ALL;
  for (const col of rows) {
    const tr = document.createElement("tr");
    if (col.is_pii) tr.className = "pii-row";
    const ent = col.entity ?? "—";
    const entP = (col.entity_probability === null || col.entity_probability === undefined)
      ? "—" : pctText(col.entity_probability);
    tr.innerHTML =
      (showFile ? `<td>${escapeHtml(col.source_file || "")}</td>` : `<td class="muted">—</td>`) +
      `<td><strong>${escapeHtml(col.name)}</strong></td>` +
      `<td><span class="status-badge ${col.is_pii ? "danger" : "success"}">${col.is_pii ? "PII" : "NO PII"}</span></td>` +
      `<td><span class="prob-num">${pctText(col.pii_probability)}</span></td>` +
      `<td>${escapeHtml(ent)}</td><td>${escapeHtml(entP)}</td>`;
    // Oculta la columna Archivo cuando ya estás dentro de un tab de archivo.
    tb.append(tr);
  }
  // Oculta/muestra la columna Archivo según vista.
  const thFile = $("#piiTable thead th:first-child");
  if (thFile) thFile.style.display = showFile ? "" : "none";
  Array.from(tb.rows).forEach((r) => { if (r.cells[0]) r.cells[0].style.display = showFile ? "" : "none"; });
}

function renderAll() {
  renderMeta(); renderKPIs(); renderTabs(); renderIndex(); renderFileHead();
  renderEntityOptions(); renderTable();
}

function showAlert(msg, kind = "error") {
  const box = $("#uploadError");
  if (!msg) { box.hidden = true; return; }
  box.className = "alert " + (kind === "warn" ? "alert-warn" : "alert-error");
  $("#uploadError-text").textContent = msg;
  box.hidden = false;
  $("#field-file").classList.toggle("is-error", kind === "error");
}

function setBusy(b) {
  state.busy = b;
  $("#btnProcess").disabled = b;
  $("#btnProcess").textContent = b ? "Analizando…" : "Analizar PII";
  $("#btnGenerateCsv").disabled = b;
  const bf = $("#btnExportFile");
  if (bf) bf.disabled = b;
  $("#loadingSkeleton").hidden = !b;
  if (b) $("#upload-result").textContent = "Analizando columnas…";
}

function updateFileHint() {
  const input = $("#datasetFile"), hint = $("#fileHint");
  const n = input.files ? input.files.length : 0;
  if (!n) { hint.textContent = "Ningún archivo seleccionado"; hint.classList.remove("has-files"); return; }
  const names = Array.from(input.files).slice(0, 3).map((f) => f.name).join(", ");
  hint.textContent = `${n} archivo(s): ${names}${n > 3 ? ` +${n - 3} más` : ""}`;
  hint.classList.add("has-files");
}

function clearAll() {
  $("#pii-form").reset();
  updateFileHint();
  showAlert(null);
  $("#note-file").textContent = "Se analizan nombres de columna. Nada sale del servidor.";
  state.columns = []; state.filter = ""; state.onlyPii = false;
  state.activeFile = ALL; state.entity = "";
  $("#in-filter").value = ""; $("#in-only-pii").checked = false;
  $("#in-entity").value = "";
  $("#upload-result").textContent = "";
  $("#pagingInfo").textContent = "";
  renderAll();
  $("#datasetFile").focus();
}

async function onSubmit(e) {
  e.preventDefault();
  if (state.busy) return;
  showAlert(null);
  $("#upload-result").textContent = "";
  const input = $("#datasetFile");
  if (!input.files || !input.files.length) {
    showAlert("Por favor, selecciona uno o más archivos CSV o Excel.");
    input.focus();
    return;
  }
  const fd = new FormData();
  Array.from(input.files).forEach((f) => fd.append("file", f));
  setBusy(true);
  try {
    const data = await apiProcess(fd);
    state.columns = data.columns_analysis || [];
    state.activeFile = ALL; state.entity = "";
    $("#in-filter").value = ""; state.filter = "";
    $("#in-only-pii").checked = false; state.onlyPii = false;
    renderAll();
    const pii = state.columns.filter((c) => c.is_pii).length;
    const nFiles = getFiles().length;
    $("#upload-result").textContent = nFiles > 1
      ? `${nFiles} archivos · ${state.columns.length} columnas · ${pii} PII. Estás en Todos: elige una tarjeta.`
      : `${state.columns.length} columnas · ${pii} PII.`;
    if (data.warnings && data.warnings.length) showAlert(data.warnings.join(" | "), "warn");
  } catch (err) {
    showAlert(err.message || "Error desconocido al procesar las tablas.");
  } finally {
    setBusy(false);
  }
}

function toCsvRow(fields) {
  return fields.map((f) => {
    const v = String(f ?? "");
    return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  }).join(",");
}

function downloadCsv(filename, rows) {
  const header = ["Archivo", "Nombre de Columna", "Prediccion", "Probabilidad PII (%)", "Entidad", "Probabilidad Entidad (%)"];
  const body = rows.map((c) => [
    c.source_file || "", c.name, c.is_pii ? "PII" : "NO PII",
    (Number(c.pii_probability || 0) * 100).toFixed(2), c.entity ?? "",
    (c.entity_probability === null || c.entity_probability === undefined) ? "" : (Number(c.entity_probability) * 100).toFixed(2),
  ]);
  const blob = new Blob(["\ufeff" + [header, ...body].map(toCsvRow).join("\r\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.append(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

function handleCsv() {
  if (!state.columns.length) {
    showAlert("Primero analiza al menos una tabla antes de generar el CSV.");
    return;
  }
  downloadCsv("analisis_pii.csv", state.columns);
}

function handleExportFile() {
  if (!state.columns.length || state.activeFile === ALL) return;
  const rows = visibleColumns();
  if (!rows.length) {
    showAlert("Nada que exportar con los filtros actuales.", "warn");
    return;
  }
  downloadCsv(`analisis_pii_${state.activeFile}.csv`, rows);
}

function setActiveFile(f) {
  state.activeFile = f || ALL;
  renderAll();
}

function boot() {
  $("#pii-form").addEventListener("submit", onSubmit);
  $("#btn-clear").addEventListener("click", clearAll);
  $("#btnGenerateCsv").addEventListener("click", handleCsv);
  $("#btnExportFile").addEventListener("click", handleExportFile);
  $("#datasetFile").addEventListener("change", updateFileHint);
  const drop = $(".file-drop");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      $("#datasetFile").files = e.dataTransfer.files;
      updateFileHint();
    }
  });
  $("#in-filter").addEventListener("input", (e) => { state.filter = e.target.value; renderIndex(); renderFileHead(); renderTable(); });
  $("#in-only-pii").addEventListener("change", (e) => { state.onlyPii = e.target.checked; renderTable(); });
  $("#in-entity").addEventListener("change", (e) => { state.entity = e.target.value; renderTable(); });
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
  renderAll(); updateFileHint();
}

document.addEventListener("DOMContentLoaded", boot);
