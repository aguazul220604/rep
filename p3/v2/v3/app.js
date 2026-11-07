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

function showError(message) {
  const box = $("#app-alert");
  const txt = $("#app-alert-text");
  if (!box || !txt) {
    alert(message);
    return;
  }
  txt.textContent = message;
  box.classList.remove("alert-warn");
  box.classList.add("alert-error");
  box.hidden = false;
}

function showWarning(message) {
  const box = $("#app-alert");
  const txt = $("#app-alert-text");
  if (!box || !txt) {
    console.warn(message);
    return;
  }
  txt.textContent = message;
  box.classList.remove("alert-error");
  box.classList.add("alert-warn");
  box.hidden = false;
}

function hideError() {
  const box = $("#app-alert");
  if (box) box.hidden = true;
}

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
  currentFlow: "NEW",
  currentSection: "CLEANUP",
  currentEnv: "ALL",
  isLoading: false,
  busy: false,

  async init() {
    this.renderCurrentView();
    await this.fetchScanBundles();
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
    hideError();
    this.renderCurrentView();
    try {
      const url = apiUrl(API.scan + this.getCutoffQuery());
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.status === "success") {
        this.scanBundles = data.bundles || [];
        this.periodoEjecucion = data.periodo_ejecucion || this.periodoEjecucion;
        this.criterioAntiguedad =
          data.criterio_antiguedad || this.criterioAntiguedad;
        if (Array.isArray(data.warnings) && data.warnings.length > 0) {
          showWarning(data.warnings.join(" · "));
        }
        try {
          const nNew = this.scanBundles.filter((b) => b.flow === "NEW").length;
          const nLegacy = this.scanBundles.filter(
            (b) => b.flow === "LEGACY",
          ).length;
          console.log(
            `[scan-bundles] total=${this.scanBundles.length} LAB(NEW)=${nNew} PROD(LEGACY)=${nLegacy} cutoff_days=${data.cutoff_days ?? "n/a"}`,
          );
        } catch (_) {}
      } else {
        showError(
          "Error al obtener versionamiento: respuesta inválida del backend",
        );
      }
    } catch (error) {
      console.error("Error al obtener /scan-bundles:", error);
      showError(
        "No se pudo cargar el versionamiento (S3). Se debe revisar la conexión",
      );
    } finally {
      this.isLoading = false;
      this.renderMetrics();
      this.renderCurrentView();
    }
  },

  async fetchHistoricBundles() {
    this.isLoading = true;
    hideError();
    this.renderCurrentView();
    try {
      const url = apiUrl(API.historical);
      const response = await fetch(url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.status === "success") {
        this.historicBundles = data.bundles || [];
      } else {
        showError("Error al obtener histórico: respuesta inválida del backend");
      }
    } catch (error) {
      console.error("Error al obtener /get-historical:", error);
      showError(
        "No se pudo cargar el histórico. Revise la conexión e intente de nuevo.",
      );
    } finally {
      this.isLoading = false;
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

  setFlow(flow) {
    if (flow !== "NEW" && flow !== "LEGACY") return;
    this.currentFlow = flow;
    const sel = $("#select-flow-dropdown");
    if (sel && sel.value !== flow) sel.value = flow;
    syncFlowTabs();
    this.switchSection("CLEANUP");
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
};

window.AppState.renderCurrentView = function () {
  const host = document.getElementById("main-content-container");
  if (!host) return;
  if (this.isLoading) {
    host.innerHTML = `<article class="slip" aria-label="Cargando">
      <header class="slip-head"><h2 class="slip-title">Cargando información</h2><p class="slip-ref">S3 · histórico</p></header>
      <div class="skeleton is-tall"></div><div class="skeleton"></div><div class="skeleton is-short"></div>
      <footer class="slip-foot"><span>Leyendo S3 y dataset histórico</span><span>…</span></footer>
    </article>`;
    return;
  }
  const bundles = this.activeBundles;
  if (this.currentFlow === "NEW" && this.currentSection === "CLEANUP") {
    host.innerHTML = renderNewFlowCleanup(bundles);
  } else if (this.currentFlow === "NEW" && this.currentSection === "HISTORIC") {
    host.innerHTML = renderNewFlowHistoric(bundles, this.currentEnv);
  } else if (
    this.currentFlow === "LEGACY" &&
    this.currentSection === "CLEANUP"
  ) {
    host.innerHTML = renderLegacyCleanup(bundles);
  } else if (
    this.currentFlow === "LEGACY" &&
    this.currentSection === "HISTORIC"
  ) {
    host.innerHTML = renderLegacyHistoric(bundles);
  }
  syncFlowTabs();
};

function statusBtnHtml(b) {
  const estado = b.estado || "Conservado";
  const cls = estado.toLowerCase();
  const icon = estado === "Descartado" ? "✖" : "✔";
  return `<button class="status-toggle-btn ${cls}" type="button" data-s3-path="${escAttr(b.s3_path)}" aria-label="${escAttr(estado)} ${escAttr(b.filename || b.s3_path)}"><span class="icon" aria-hidden="true">${icon}</span><span>${escHtml(estado)}</span></button>`;
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

function renderProjectTable(items, emptyMessage, ariaLabel) {
  const rows =
    items.length === 0
      ? `<tr><td colspan="3">${escHtml(emptyMessage)}</td></tr>`
      : items
          .map(
            (b) =>
              `<tr><td>${escHtml(b.proyecto)}</td><td title="${escAttr(b.s3_path)}">${escHtml(b.filename || b.s3_path)}</td><td>${statusBtnHtml(b)}</td></tr>`,
          )
          .join("");
  return `<div class="table-wrap"><table class="table" aria-label="${escAttr(ariaLabel)}">
      <thead><tr><th scope="col">Proyecto</th><th scope="col">Versiones</th><th scope="col">Estatus final</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function renderNewFlowCleanup(bundles) {
  const newBundles = bundles.filter((b) => b.flow === "NEW");
  const uatItems = newBundles.filter(
    (b) => (b.env || "").toUpperCase() === "UAT",
  );
  const prodItems = newBundles.filter(
    (b) => (b.env || "").toUpperCase() === "PROD-1",
  );
  const directItems = newBundles.filter((b) => {
    const env = (b.env || "").toUpperCase();
    return env !== "UAT" && env !== "PROD-1";
  });

  const hasFallbackEnvs = uatItems.length > 0 || prodItems.length > 0;

  let bodyHtml = "";
  if (directItems.length > 0 || !hasFallbackEnvs) {
    bodyHtml += renderProjectTable(
      directItems,
      "No hay registros en LAB",
      "Versionamiento LAB",
    );
  }
  if (hasFallbackEnvs) {
    bodyHtml += `<div class="cards-grid-2" style="${directItems.length > 0 ? "margin-top: var(--s6)" : ""}">
      <div class="env-card"><h3 class="env-title">UAT</h3>${renderProjectGroups(uatItems)}</div>
      <div class="env-card"><h3 class="env-title">PROD-1</h3>${renderProjectGroups(prodItems)}</div>
    </div>`;
  }

  return `${slipOpen("LAB | Versionamiento de proyectos", "versionamiento")}
    <div class="view-title-row">
      <div class="controls-group"><span class="field-label" style="margin:0">Agrupado por proyecto · excluye histórico</span></div>
      <div class="controls-group"><button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('HISTORIC')">Consultar histórico</button></div>
    </div>
    ${bodyHtml}
    ${slipClose(`${newBundles.length} bundles · la versión más reciente se conserva (default)`, "BN-01")}`;
}

function renderNewFlowHistoric(bundles, selectedEnv) {
  const allNewItems = bundles.filter(
    (b) => (b.flow || "").trim().toUpperCase() === "NEW",
  );
  const hasFallbackEnvs = allNewItems.some((b) => {
    const env = (b.env || "").trim().toUpperCase();
    return env === "UAT" || env === "PROD-1";
  });

  if (!hasFallbackEnvs) {
    return `${slipOpen("LAB | Histórico de proyectos", "histórico")}
      <div class="view-title-row">
        <div class="controls-group"><span class="field-label" style="margin:0">Dataset histórico · LAB</span></div>
        <div class="controls-group">
          <button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('CLEANUP')">Consultar versionamiento</button>
        </div>
      </div>
      ${renderProjectTable(allNewItems, "No hay registros históricos en LAB", "Histórico LAB")}
      ${slipClose(`${allNewItems.length} registros en histórico`, "BN-02")}`;
  }

  const targetEnv = (selectedEnv || "ALL").trim().toUpperCase();
  const items =
    targetEnv === "ALL"
      ? allNewItems
      : allNewItems.filter(
          (b) => (b.env || "").trim().toUpperCase() === targetEnv,
        );

  const rows =
    items.length === 0
      ? `<tr><td colspan="4">No hay registros para este ambiente</td></tr>`
      : items
          .map(
            (b) =>
              `<tr><td>${escHtml(b.nickname || b.env || "LAB")}</td><td>${escHtml(b.proyecto)}</td><td title="${escAttr(b.s3_path)}">${escHtml(b.filename || b.s3_path)}</td><td>${statusBtnHtml(b)}</td></tr>`,
          )
          .join("");
  return `${slipOpen("LAB | Histórico de proyectos", "histórico")}
    <div class="view-title-row">
      <div class="controls-group"><span class="field-label" style="margin:0">Dataset histórico · ${escHtml(targetEnv === "ALL" ? "TODOS" : targetEnv)}</span></div>
      <div class="controls-group">
        <button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('CLEANUP')">Consultar versionamiento</button>
        <select class="select" aria-label="Ambiente" onchange="AppState.currentEnv=this.value; AppState.renderCurrentView();">
          <option value="ALL" ${targetEnv === "ALL" ? "selected" : ""}>TODOS</option>
          <option value="LAB" ${targetEnv === "LAB" ? "selected" : ""}>LAB</option>
          <option value="UAT" ${targetEnv === "UAT" ? "selected" : ""}>UAT</option>
          <option value="PROD-1" ${targetEnv === "PROD-1" ? "selected" : ""}>PROD-1</option>
        </select>
      </div>
    </div>
    <div class="table-wrap"><table class="table" aria-label="Histórico LAB">
      <thead><tr><th scope="col">Servidor</th><th scope="col">Proyecto</th><th scope="col">Versiones</th><th scope="col">Estatus final</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${slipClose(`${items.length} registros en histórico`, "BN-02")}`;
}

function renderLegacyCleanup(bundles) {
  const items = bundles.filter((b) => b.flow === "LEGACY");
  return `${slipOpen("PROD | Versionamiento de proyectos", "versionamiento")}
    <div class="view-title-row">
      <div class="controls-group"><span class="field-label" style="margin:0">Agrupado por proyecto · excluye histórico</span></div>
      <div class="controls-group"><button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('HISTORIC')">Consultar histórico</button></div>
    </div>
    ${renderProjectTable(items, "No hay registros en PROD", "Versionamiento PROD")}
    ${slipClose(`${items.length} bundles · la versión más reciente se conserva (default)`, "BN-03")}`;
}

function renderLegacyHistoric(bundles) {
  const items = bundles.filter((b) => b.flow === "LEGACY");
  return `${slipOpen("PROD | Histórico de proyectos", "histórico")}
    <div class="view-title-row">
      <div class="controls-group"><span class="field-label" style="margin:0">Dataset histórico · PROD</span></div>
      <div class="controls-group"><button class="btn btn-secondary btn-highlight" type="button" onclick="AppState.switchSection('CLEANUP')">Consultar versionamiento</button></div>
    </div>
    ${renderProjectTable(items, "No hay registros históricos", "Histórico PROD")}
    ${slipClose(`${items.length} registros en histórico`, "BN-04")}`;
}

function renderProjectGroups(items) {
  if (!items || items.length === 0)
    return emptyBlock("Sin versionamientos detectados");
  const grouped = {};
  items.forEach((item) => {
    const key = `${item.nickname || "N/A"}|||${item.proyecto}`;
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(item);
  });
  return Object.keys(grouped)
    .sort()
    .map((key) => {
      const [nickname, proj] = key.split("|||");
      const rows = grouped[key]
        .map(
          (b) => `
      <div class="version-row">
        <span class="version-name" title="${escAttr(b.s3_path)}">${escHtml(b.proyecto)}/${escHtml(b.filename)}</span>
        ${statusBtnHtml(b)}
      </div>`,
        )
        .join("");
      const tagLabel =
        nickname && nickname !== "LAB" && nickname !== "N/A"
          ? `${escHtml(nickname)} · ${escHtml(proj)}`
          : escHtml(proj);
      return `<div class="project-box"><span class="server-tag">${tagLabel}</span><div class="versions-list">${rows}</div></div>`;
    })
    .join("");
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
  if (!confirm("¿Está seguro de autorizar la eliminación en S3?")) return;
  hideError();
  try {
    const payload = window.AppState.getUnionBundles();
    if (payload.length === 0) {
      showError("No hay bundles para autorizar");
      return;
    }
    setBusy(true);
    const url = apiUrl(API.authorize);
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bundles: payload }),
    });
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
    const response = await fetch(url);
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
function syncFlowTabs() {
  const flow = window.AppState.currentFlow;
  $$(".viewnav-tab").forEach((t) =>
    t.setAttribute("aria-selected", String(t.dataset.flow === flow)),
  );
}

function syncHash() {
  const flow = (window.AppState.currentFlow || "NEW").toLowerCase();
  const section = (window.AppState.currentSection || "CLEANUP").toLowerCase();
  const hash = `#/${flow}/${section}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function applyHash() {
  const h = (location.hash || "").toLowerCase();
  if (h.includes("legacy") || h.includes("prod"))
    window.AppState.currentFlow = "LEGACY";
  else if (h.includes("new") || h.includes("lab"))
    window.AppState.currentFlow = "NEW";
  if (h.includes("historic")) window.AppState.currentSection = "HISTORIC";
  else if (h.includes("cleanup")) window.AppState.currentSection = "CLEANUP";
  const sel = $("#select-flow-dropdown");
  if (sel) sel.value = window.AppState.currentFlow;
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

document.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 1000);
  applyHash();

  const flowDropdown = document.getElementById("select-flow-dropdown");
  if (flowDropdown) {
    flowDropdown.value = window.AppState.currentFlow;
    flowDropdown.addEventListener("change", (e) => {
      window.AppState.setFlow(e.target.value);
    });
  }
  $$(".viewnav-tab").forEach((t) =>
    t.addEventListener("click", () => window.AppState.setFlow(t.dataset.flow)),
  );

  window.AppState.init();
});
