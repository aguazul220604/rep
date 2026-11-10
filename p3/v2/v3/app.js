const API = {
  scan: "/scan-bundles",
  historical: "/get-historical",
  authorize: "/authorize-cleanup",
  globalReport: "/global-report",
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

async function fetchWithTimeout(url, options = {}, timeoutMs = 90000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function isAbortError(error) {
  return Boolean(error && (error.name === "AbortError" || error.code === 20));
}

/* =====================================================================
   HELPERS
   ===================================================================== */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function escHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escAttr(value) {
  return escHtml(value).replace(/'/g, "&#39;");
}

const STATUS_LABELS = { Conservado: "Conservar", Descartado: "Eliminar" };
const statusLabel = (estado) =>
  STATUS_LABELS[estado] || STATUS_LABELS.Conservado;

function showError(message, allowRetry = false) {
  const box = $("#app-alert");
  const txt = $("#app-alert-text");
  const retry = $("#app-alert-retry");
  if (!box || !txt) {
    alert(message);
    return;
  }
  txt.textContent = message;
  box.classList.remove("alert-warn");
  box.classList.add("alert-error");
  box.hidden = false;
  if (retry) retry.hidden = !allowRetry;
}

function showWarning(message) {
  const box = $("#app-alert");
  const txt = $("#app-alert-text");
  const retry = $("#app-alert-retry");
  if (!box || !txt) {
    console.warn(message);
    return;
  }
  txt.textContent = message;
  box.classList.remove("alert-error");
  box.classList.add("alert-warn");
  box.hidden = false;
  if (retry) retry.hidden = true;
}

function hideError() {
  const box = $("#app-alert");
  const retry = $("#app-alert-retry");
  if (box) box.hidden = true;
  if (retry) retry.hidden = true;
}

function confirmAction(options = {}) {
  const message = options.message || "¿Deseas continuar?";
  const modal = $("#confirm-modal");
  if (!modal) return Promise.resolve(window.confirm(message));

  const title = $("#confirm-modal-title");
  const text = $("#confirm-modal-text");
  const confirmBtn = modal.querySelector("[data-modal-confirm]");
  const cancelBtn = modal.querySelector("button[data-modal-cancel]");
  const cancelEls = modal.querySelectorAll("[data-modal-cancel]");
  const previouslyFocused = document.activeElement;

  if (title) title.textContent = options.title || "Confirmar acción";
  if (text) text.textContent = message;
  if (confirmBtn) confirmBtn.textContent = options.confirmLabel || "Confirmar";

  modal.hidden = false;
  document.body.classList.add("confirm-open");

  return new Promise((resolve) => {
    function finish(result) {
      modal.hidden = true;
      document.body.classList.remove("confirm-open");
      modal.removeEventListener("keydown", onKeyDown);
      confirmBtn?.removeEventListener("click", onConfirm);
      cancelEls.forEach((el) => el.removeEventListener("click", onCancel));
      if (previouslyFocused && typeof previouslyFocused.focus === "function") {
        previouslyFocused.focus();
      }
      resolve(result);
    }
    function onConfirm() {
      finish(true);
    }
    function onCancel() {
      finish(false);
    }
    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    }
    confirmBtn?.addEventListener("click", onConfirm);
    cancelEls.forEach((el) => el.addEventListener("click", onCancel));
    modal.addEventListener("keydown", onKeyDown);
    if (cancelBtn) cancelBtn.focus();
    else if (confirmBtn) confirmBtn.focus();
  });
}
window.confirmAction = confirmAction;

function tickClock() {
  const node = $("#topbar-clock");
  if (!node) return;
  try {
    const d = new Date();
    const fmt = new Intl.DateTimeFormat("es-MX", {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    node.textContent = fmt.format(d);
  } catch {
    node.textContent = new Date().toLocaleString();
  }
}

/* =====================================================================
   STATE
   ===================================================================== */
window.AppState = {
  periodoEjecucion: "Sep 2026",
  criterioAntiguedad: "> 6 meses",
  scanBundles: [],
  historicBundles: [],
  server: {
    hostname: "—",
    environment: "—",
    flow: "NEW",
    known: true,
    last_scan: "",
  },
  recentBundles: [],
  recentSort: "project",
  currentSection: "CLEANUP",
  isLoading: false,
  busy: false,
  preloaderText: "Leyendo inventario y dataset histórico…",

  async init() {
    this.isLoading = true;
    this.preloaderText = "Leyendo inventario y dataset histórico…";
    this.renderPreloader();
    await this.fetchScanBundles();
  },

  renderPreloader() {
    const node = document.getElementById("preloader");
    if (!node) return;
    const visible = Boolean(this.isLoading);
    if (visible) {
      const txt = document.getElementById("preloader-text");
      if (txt) txt.textContent = this.preloaderText;
    }
    node.hidden = !visible;
  },

  getBundleSize(b) {
    const v = b.size ?? b.size_mb;
    return parseFloat(v) || 0;
  },

  getUnionBundles() {
    const map = new Map();
    for (const b of this.scanBundles || []) {
      if (b && b.s3_path) map.set(b.s3_path, b);
    }
    for (const b of this.historicBundles || []) {
      if (b && b.s3_path && !map.has(b.s3_path)) map.set(b.s3_path, b);
    }
    return [...map.values()];
  },

  getCutoffQuery() {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const raw = params.get("cutoff_days");
      if (raw !== null && raw !== "" && !Number.isNaN(Number(raw))) {
        return `?cutoff_days=${encodeURIComponent(raw)}`;
      }
    } catch (_) {}
    return "";
  },

  async fetchScanBundles() {
    this.isLoading = true;
    this.preloaderText = "Leyendo inventario y dataset histórico…";
    hideError();
    this.renderPreloader();
    this.renderCurrentView();
    renderRecentSection();
    try {
      const url = apiUrl(API.scan + this.getCutoffQuery());
      const response = await fetchWithTimeout(url, {}, 90000);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.status === "success") {
        this.scanBundles = data.bundles || [];
        this.recentBundles = data.recent_bundles || [];
        if (data.server) this.server = data.server;
        this.periodoEjecucion = data.periodo_ejecucion || this.periodoEjecucion;
        this.criterioAntiguedad =
          data.criterio_antiguedad || this.criterioAntiguedad;
        if (Array.isArray(data.warnings) && data.warnings.length > 0) {
          showWarning(data.warnings.join(" · "));
        }
        try {
          console.log(
            `[scan-bundles] host=${this.server.hostname} env=${this.server.environment} flow=${this.server.flow} total=${this.scanBundles.length} cutoff_days=${data.cutoff_days ?? "n/a"}`,
          );
        } catch (_) {}
      } else {
        showError(
          "Error al obtener versionamiento: respuesta inválida del backend",
        );
      }
    } catch (error) {
      console.error("Error al obtener /scan-bundles:", error);
      if (isAbortError(error)) {
        showError(
          "El escaneo de S3 tardó más de 90 segundos. Vuelve a intentarlo.",
          true,
        );
      } else {
        showError(
          "No se pudo cargar el versionamiento (S3). Se debe revisar la conexión",
          true,
        );
      }
    } finally {
      this.isLoading = false;
      this.renderPreloader();
      this.renderMetrics();
      this.renderCurrentView();
      renderRecentSection();
    }
  },

  async fetchHistoricBundles() {
    this.isLoading = true;
    this.preloaderText = "Leyendo histórico de bundles…";
    hideError();
    this.renderPreloader();
    this.renderCurrentView();
    try {
      const url = apiUrl(API.historical);
      const response = await fetchWithTimeout(url, {}, 60000);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.status === "success") {
        this.historicBundles = data.bundles || [];
        if (data.server) this.server = { ...this.server, ...data.server };
      } else {
        showError("Error al obtener histórico: respuesta inválida del backend");
      }
    } catch (error) {
      console.error("Error al obtener /get-historical:", error);
      if (isAbortError(error)) {
        showError(
          "El histórico tardó más de 60 segundos en responder. Vuelve a intentarlo.",
          true,
        );
      } else {
        showError(
          "No se pudo cargar el histórico. Revise la conexión e intente de nuevo.",
          true,
        );
      }
    } finally {
      this.isLoading = false;
      this.renderPreloader();
      this.renderMetrics();
      this.renderCurrentView();
    }
  },

  async switchSection(section) {
    this.currentSection = section;
    syncHash();
    if (section === "HISTORIC" && this.historicBundles.length === 0) {
      await this.fetchHistoricBundles();
    } else {
      this.renderMetrics();
      this.renderCurrentView();
    }
  },

  get activeBundles() {
    return this.currentSection === "CLEANUP"
      ? this.scanBundles
      : this.historicBundles;
  },

  // MÉTRICAS GLOBALES
  getMetrics() {
    const all = this.getUnionBundles();
    const totalMB = all.reduce((acc, b) => acc + this.getBundleSize(b), 0);
    const espacioALiberar = all
      .filter((b) => b.estado === "Descartado")
      .reduce((acc, b) => acc + this.getBundleSize(b), 0);
    const espacioResultante = Math.max(0, totalMB - espacioALiberar);
    return {
      total: Number(totalMB).toFixed(2),
      aLiberar: Number(espacioALiberar).toFixed(2),
      resultante: Number(espacioResultante).toFixed(2),
    };
  },

  toggleStatus(s3_path) {
    if (!s3_path) return;
    const normalizePath = (path) => (path || "").trim().replace(/\/+$/, "");
    const targetPath = normalizePath(s3_path);
    const scanItem = (this.scanBundles || []).find(
      (b) => normalizePath(b.s3_path) === targetPath,
    );
    const historicItem = (this.historicBundles || []).find(
      (b) => normalizePath(b.s3_path) === targetPath,
    );
    const currentStatus = (scanItem || historicItem)?.estado || "Conservado";
    const newStatus =
      currentStatus === "Conservado" ? "Descartado" : "Conservado";
    if (scanItem) scanItem.estado = newStatus;
    if (historicItem) historicItem.estado = newStatus;
    this.renderMetrics();
    this.renderCurrentView();
  },

  setAllActive(estado) {
    if (estado !== "Conservado" && estado !== "Descartado") return;
    const items = this.activeBundles || [];
    if (items.length === 0) return;
    for (const b of items) b.estado = estado;
    this.renderMetrics();
    this.renderCurrentView();
  },
};

/* =====================================================================
   RENDER
   ===================================================================== */
window.AppState.renderMetrics = function () {
  const m = this.getMetrics();
  const set = (id, v) => {
    const n = document.getElementById(id);
    if (n) n.textContent = v;
  };
  set("stat-total", `${m.total} MB`);
  set("stat-liberar", `${m.aLiberar} MB`);
  set("stat-resultante", `${m.resultante} MB`);
  set("stat-periodo", `Periodo de ejecución: ${this.periodoEjecucion}`);
  set(
    "stat-criterio",
    `Antigüedad de versionamientos: ${this.criterioAntiguedad}`,
  );
  set("stat-periodo-top", this.periodoEjecucion);
  set("stat-criterio-top", this.criterioAntiguedad);
  set("meta-asof", `as of ${this.periodoEjecucion}`);
  set("guide-criterio", this.criterioAntiguedad);
};

window.AppState.renderCurrentView = function () {
  const host = document.getElementById("main-content-container");
  if (!host) return;
  if (this.isLoading) {
    host.innerHTML = `<article class="slip" aria-label="Cargando">
      <header class="slip-head"><h2 class="slip-title">Cargando información</h2><p class="slip-ref">S3 · histórico</p></header>
      <div class="skeleton is-tall"></div><div class="skeleton"></div><div class="skeleton is-short"></div>
      <footer class="slip-foot"><span>Leyendo inventario y dataset histórico</span><span>…</span></footer>
    </article>`;
    return;
  }
  const bundles = this.activeBundles;
  if (this.currentSection === "HISTORIC") {
    host.innerHTML = renderHistorico(bundles);
  } else {
    host.innerHTML = renderVersionamiento(bundles);
  }
  syncSelectAll();
};

function syncSelectAll() {
  const check = document.querySelector(
    "#main-content-container .select-all-check",
  );
  if (!check) return;
  const items = window.AppState.activeBundles || [];
  if (items.length === 0) {
    check.disabled = true;
    check.checked = false;
    check.indeterminate = false;
    check.setAttribute("aria-label", "No hay bundles para seleccionar");
    check.title = "No hay bundles para seleccionar";
    return;
  }
  const descartados = items.filter(
    (b) => (b.estado || "Conservado") === "Descartado",
  ).length;
  const allDesc = descartados === items.length;
  check.disabled = false;
  check.checked = allDesc;
  check.indeterminate = descartados > 0 && !allDesc;
  const label = allDesc
    ? "Conservar todos"
    : "Eliminar todos";
  check.setAttribute("aria-label", label);
  check.title = label;
}

function renderContextHeader() {
  const s = window.AppState.server || {};
  return `<div class="context-bar">
    <span class="context-item"><span class="context-label">Ambiente</span><strong>${escHtml(s.environment || "—")}</strong></span>
    <span class="context-sep" aria-hidden="true">·</span>
    <span class="context-item"><span class="context-label">Servidor</span><strong>${escHtml(s.hostname || "—")}</strong></span>
  </div>`;
}

function statusBtnHtml(b) {
  const estado = b.estado || "Conservado";
  const cls = estado.toLowerCase();
  const icon = estado === "Descartado" ? "✖" : "✔";
  const label = statusLabel(estado);
  return `<button class="status-toggle-btn ${cls}" type="button" data-s3-path="${escAttr(b.s3_path)}" aria-label="${escAttr(label)} ${escAttr(b.filename || b.s3_path)}"><span class="icon" aria-hidden="true">${icon}</span><span>${escHtml(label)}</span></button>`;
}

function emptyBlock(text) {
  return `<div class="state-block"><p class="state-word">Sin registros</p><p class="state-sub">${escHtml(text)}</p></div>`;
}

function slipOpen(title, ref) {
  return `<article class="slip"><header class="slip-head"><h2 class="slip-title">${escHtml(title)}</h2><p class="slip-ref">${escHtml(ref)}</p></header>`;
}
function slipClose(left, right) {
  return `<footer class="slip-foot"><span>${escHtml(left)}</span><span>${escHtml(right)}</span></footer></article>`;
}

function formatBundleDate(value) {
  const text = String(value ?? "").trim();
  if (!text) return "—";
  if (["nan", "nat", "none", "null"].includes(text.toLowerCase())) return "—";
  return text;
}

function formatBundleSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size <= 0) return "—";
  return `${size.toFixed(2)} MB`;
}

function renderProjectTable(items, emptyMessage, ariaLabel, options = {}) {
  const list = Array.isArray(items) ? items : [];
  const showCleanupPeriod = Boolean(options.showCleanupPeriod);
  const columnCount = showCleanupPeriod ? 6 : 5;
  let rows = "";
  if (list.length === 0) {
    rows = `<tr><td colspan="${columnCount}">${escHtml(emptyMessage)}</td></tr>`;
  } else {
    // Agrupa por proyecto conservando el orden de llegada para que cada
    // proyecto ocupe una sola celda (rowspan) con sus versiones debajo.
    const groups = new Map();
    for (const b of list) {
      const key = b.proyecto || "—";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(b);
    }
    rows = [...groups.entries()]
      .map(([proyecto, versions]) =>
        versions
          .map((b, idx) => {
            const projectCell =
              idx === 0
                ? `<td class="project-cell" rowspan="${versions.length}">${escHtml(proyecto)}</td>`
                : "";
            const rowClass = idx === 0 ? ' class="project-start"' : "";
            const cleanupCell = showCleanupPeriod
              ? `<td class="cell-date">${escHtml(formatBundleDate(b.fecha_periodo_limpieza))}</td>`
              : "";
            return `<tr${rowClass}>${projectCell}<td title="${escAttr(b.s3_path)}">${escHtml(b.filename || b.s3_path)}</td><td class="cell-date">${escHtml(formatBundleDate(b.fecha_creacion_s3))}</td><td class="cell-size">${escHtml(formatBundleSize(b.size ?? b.size_mb))}</td>${cleanupCell}<td>${statusBtnHtml(b)}</td></tr>`;
          })
          .join(""),
      )
      .join("");
  }
  const cleanupHeader = showCleanupPeriod
    ? '<th scope="col">Periodo de limpieza</th>'
    : "";
  const allDescartado =
    list.length > 0 &&
    list.every((b) => (b.estado || "Conservado") === "Descartado");
  const selectAllLabel = allDescartado
    ? "Conservar todos"
    : "Eliminar todos";
  return `<div class="table-wrap"><table class="table" aria-label="${escAttr(ariaLabel)}">
      <thead><tr><th scope="col">Proyecto</th><th scope="col">Versiones</th><th scope="col">Fecha creación S3</th><th scope="col">Peso</th>${cleanupHeader}<th scope="col" class="status-col-head"><label class="select-all"><input type="checkbox" class="select-all-check"${allDescartado ? " checked" : ""}${list.length === 0 ? " disabled" : ""} aria-label="${escAttr(selectAllLabel)}" title="${escAttr(selectAllLabel)}"><span class="visually-hidden">Seleccionar todos</span></label><span class="status-col-label">Acción al autorizar</span></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function renderVersionamiento(bundles) {
  const items = Array.isArray(bundles) ? bundles : [];
  return `${slipOpen("Versionamiento de proyectos", "versionamiento")}
    ${renderContextHeader()}
    <div class="view-title-row">
      <div class="controls-group"><span class="field-label" style="margin:0">Agrupado por proyecto · excluye histórico</span></div>
      <div class="controls-group"><button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('HISTORIC')">Consultar histórico</button></div>
    </div>
    ${renderProjectTable(items, "No hay registros de versionamiento", "Versionamiento")}
    ${slipClose(`${items.length} bundles · la versión más reciente se conserva (default)`, "BN-01")}`;
}

function renderHistorico(bundles) {
  const items = Array.isArray(bundles) ? bundles : [];
  return `${slipOpen("Histórico de proyectos", "histórico")}
    ${renderContextHeader()}
    <div class="view-title-row">
      <div class="controls-group"><span class="field-label" style="margin:0">Dataset histórico</span></div>
      <div class="controls-group"><button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('CLEANUP')">Consultar versionamiento</button></div>
    </div>
    ${renderProjectTable(items, "No hay registros históricos", "Histórico", { showCleanupPeriod: true })}
    ${slipClose(`${items.length} registros en histórico`, "BN-02")}`;
}

function formatRemaining(value) {
  const dias = Number(value);
  if (!Number.isFinite(dias)) return "—";
  if (dias <= 0) return "Hoy";
  if (dias === 1) return "1 día";
  return `${dias} días`;
}

function renderRecentSection() {
  const app = window.AppState;
  const contextEl = $("#recent-context");
  const countEl = $("#recent-count");
  const contentEl = $("#recent-content");
  const sortBtn = $("#recent-sort-btn");
  const server = app.server || {};

  if (contextEl) {
    const inventoryAsOf = server.last_scan
      ? ` · Inventario: ${server.last_scan}`
      : "";
    contextEl.textContent = `Ambiente: ${server.environment || "—"} · Servidor: ${server.hostname || "—"}${inventoryAsOf}`;
  }
  if (sortBtn) {
    sortBtn.textContent =
      app.recentSort === "proximity"
        ? "Ordenar por proyecto"
        : "Ordenar por proximidad";
  }
  if (countEl) {
    if (app.isLoading) countEl.textContent = "Cargando…";
    else countEl.textContent = `${(app.recentBundles || []).length} bundles`;
  }
  if (!contentEl) return;

  if (app.isLoading) {
    contentEl.innerHTML = `<div class="skeleton is-tall"></div><div class="skeleton"></div><div class="skeleton is-short"></div>`;
    return;
  }

  const items = app.recentBundles || [];
  if (items.length === 0) {
    contentEl.innerHTML = `<div class="state-block"><p class="state-word">Sin registros</p><p class="state-sub">No hay bundles en la ventana de 6 meses.</p></div>`;
    return;
  }
  contentEl.innerHTML = renderRecentTable(items, app.recentSort);
}

function renderRecentTable(items, sortMode) {
  const groups = new Map();
  for (const b of items) {
    const key = b.proyecto || "—";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(b);
  }

  const groupList = [...groups.entries()].map(([proyecto, versions]) => {
    const sorted = [...versions].sort((a, b) => {
      const da = Number(a.dias_para_limpieza);
      const db = Number(b.dias_para_limpieza);
      const na = Number.isFinite(da) ? da : Number.MAX_SAFE_INTEGER;
      const nb = Number.isFinite(db) ? db : Number.MAX_SAFE_INTEGER;
      if (na !== nb) return na - nb;
      return String(a.fecha_creacion_s3 || "").localeCompare(
        String(b.fecha_creacion_s3 || ""),
      );
    });
    const minDias = sorted.reduce((min, item) => {
      const n = Number(item.dias_para_limpieza);
      return Number.isFinite(n) ? Math.min(min, n) : min;
    }, Number.MAX_SAFE_INTEGER);
    return { proyecto, versions: sorted, minDias };
  });

  if (sortMode === "proximity") {
    groupList.sort(
      (a, b) => a.minDias - b.minDias || a.proyecto.localeCompare(b.proyecto),
    );
  } else {
    groupList.sort((a, b) => a.proyecto.localeCompare(b.proyecto));
  }

  const rows = groupList
    .map(({ proyecto, versions }) =>
      versions
        .map((b, idx) => {
          const projectCell =
            idx === 0
              ? `<td class="project-cell" rowspan="${versions.length}">${escHtml(proyecto)}</td>`
              : "";
          const rowClass = idx === 0 ? ' class="project-start"' : "";
          return `<tr${rowClass}>${projectCell}<td title="${escAttr(b.s3_path)}">${escHtml(b.filename || b.s3_path)}</td><td>${escHtml(b.fecha_creacion_s3 || "—")}</td><td>${escHtml(formatRemaining(b.dias_para_limpieza))}</td></tr>`;
        })
        .join(""),
    )
    .join("");

  return `<div class="table-wrap"><table class="table" aria-label="Bundles próximos al periodo de limpieza">
      <thead><tr><th scope="col">Proyecto</th><th scope="col">Versión</th><th scope="col">Fecha</th><th scope="col">Faltan</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

/* =====================================================================
   ACTIONS
   ===================================================================== */
function setBusy(busy) {
  window.AppState.busy = busy;
  for (const id of ["btn-authorize", "btn-global"]) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    btn.disabled = busy;
    btn.setAttribute("aria-busy", String(busy));
  }
  const auth = $("#btn-authorize");
  if (auth) auth.textContent = busy ? "Autorizando…" : "Autorizar";
}

function printStamp(text) {
  const slot = $("#stamp-slot");
  if (!slot) return;
  slot.textContent = "";
  const stamp = el("div", "stamp is-landing");
  stamp.append(el("span", "stamp-word", "Certified"));
  stamp.append(el("span", "stamp-rule"));
  stamp.append(el("span", "stamp-meta", text || "Banamex"));
  slot.append(stamp);
}

window.triggerAuthorize = async function () {
  if (window.AppState.busy) return;
  hideError();

  const payload = window.AppState.getUnionBundles();
  if (payload.length === 0) {
    showError("No hay bundles para autorizar");
    return;
  }
  const toDelete = payload.filter((b) => b.estado === "Descartado").length;
  const confirmed = await window.confirmAction({
    title: "Autorizar eliminación en S3",
    message: `¿Está seguro de autorizar la eliminación en S3? Se eliminarán ${toDelete} bundle(s) marcados para eliminar y se actualizará el histórico.`,
    confirmLabel: "Autorizar",
    cancelLabel: "Cancelar",
  });
  if (!confirmed) return;

  try {
    setBusy(true);
    const url = apiUrl(API.authorize);
    const response = await fetchWithTimeout(
      url,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bundles: payload }),
      },
      180000,
    );
    if (!response.ok) {
      let msg = `HTTP ${response.status}`;
      try {
        const err = await response.json();
        if (err && err.message) msg = err.message;
      } catch (_) {}
      showError(`Error al procesar la autorización: ${msg}`);
      return;
    }
    const blob = await response.blob();
    const downloadUrl = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = downloadUrl;
    a.download = `Reporte_Limpieza_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    $("#action-result").textContent =
      `Limpieza ejecutada · ${payload.length} bundles · CSV descargado`;
    printStamp(`Banamex · ${window.AppState.periodoEjecucion}`);
    window.AppState.historicBundles = [];
    await window.AppState.fetchScanBundles();
  } catch (error) {
    console.error("Error en authorize:", error);
    showError("Error al procesar la autorización");
  } finally {
    setBusy(false);
  }
};

window.triggerGlobalReport = async function () {
  if (window.AppState.busy) return;
  hideError();
  try {
    setBusy(true);
    const suffix = window.AppState.getCutoffQuery
      ? window.AppState.getCutoffQuery()
      : "";
    const url = apiUrl(API.globalReport + suffix);
    const response = await fetchWithTimeout(url, {}, 180000);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const downloadUrl = window.URL.createObjectURL(blob);
    window.open(downloadUrl, "_blank");
    $("#action-result").textContent = "Reporte global generado";
  } catch (error) {
    console.error("Error en global-report:", error);
    showError("No se pudo generar el reporte global");
  } finally {
    setBusy(false);
  }
};

/* =====================================================================
   VIEW SWITCH
   ===================================================================== */
function syncHash() {
  const section = (window.AppState.currentSection || "CLEANUP").toLowerCase();
  const hash = `#/${section}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function applyHash() {
  const h = (location.hash || "").toLowerCase();
  if (h.includes("historic")) window.AppState.currentSection = "HISTORIC";
  else if (h.includes("cleanup")) window.AppState.currentSection = "CLEANUP";
}

/* =====================================================================
   BOOT
   ===================================================================== */
document.addEventListener("click", (e) => {
  const btn =
    e.target && e.target.closest
      ? e.target.closest(".status-toggle-btn")
      : null;
  if (btn && btn.dataset && btn.dataset.s3Path) {
    window.AppState.toggleStatus(btn.dataset.s3Path);
  }
});

document.addEventListener("change", (e) => {
  if (e.target && e.target.classList.contains("select-all-check")) {
    window.AppState.setAllActive(
      e.target.checked ? "Descartado" : "Conservado",
    );
    const next = document.querySelector(
      "#main-content-container .select-all-check",
    );
    if (next) next.focus();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 1000);
  applyHash();

  const retryBtn = document.getElementById("app-alert-retry");
  if (retryBtn) {
    retryBtn.addEventListener("click", () => {
      hideError();
      window.AppState.fetchScanBundles();
    });
  }

  const recentSortBtn = document.getElementById("recent-sort-btn");
  if (recentSortBtn) {
    recentSortBtn.addEventListener("click", () => {
      const app = window.AppState;
      app.recentSort = app.recentSort === "proximity" ? "project" : "proximity";
      renderRecentSection();
    });
  }

  window.AppState.init();
});
