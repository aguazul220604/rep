const API = {
  inactivos: (umbral) => `/obtener-proyectos-inactivos?umbral=${umbral}`,
  analizar: "/analizar-proyecto",
  limpieza: "/ejecutar-limpieza",
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

/* HELPERS */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function escapeHtml(valor) {
  if (valor === null || valor === undefined) return "";
  return String(valor)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/* STATE */
const PROFUNDIDAD_DEFECTO = 4;
const state = {
  proyectos: [],
  seleccionado: null,
  profundidad: PROFUNDIDAD_DEFECTO,
  filtroTexto: "",
  listBusy: false,
  detailBusy: false,
  authBusy: false,
  confirmOpen: false,
};
let proyectosReqId = 0;
let detalleReqId = 0;

function normalizeText(valor) {
  return String(valor || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function coincideBusqueda(p) {
  if (!state.filtroTexto) return true;
  return normalizeText(p.nombre_proyecto).includes(state.filtroTexto);
}

function showToast(message, type = "info", details = []) {
  const region = $("#toast-region");
  if (!region) return;
  const toast = el("div", "toast toast--" + type);
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const body = el("div", "toast-body");
  body.append(el("p", "toast-msg", message));
  if (details.length) {
    const ul = el("ul", "toast-details");
    details.forEach((d) => ul.append(el("li", "", d)));
    body.append(ul);
  }
  const close = el("button", "toast-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Cerrar aviso");
  close.addEventListener("click", () => toast.remove());
  toast.append(body, close);
  region.append(toast);
  window.setTimeout(() => toast.remove(), type === "error" ? 9000 : 6000);
}

function updateAuthorizeButton() {
  const btn = $("#btn-autorizar-cambios");
  if (!btn) return;
  const n = state.proyectos.filter((p) => p.enEliminacion).length;
  btn.textContent = `Autorizar eliminación (${n})`;
  btn.disabled = n === 0 || state.authBusy;
}

function modalFocusables() {
  const modal = $("#confirm-modal");
  if (!modal) return [];
  return $$(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    modal,
  ).filter((node) => !node.disabled && node.offsetParent !== null);
}

function abrirConfirmacion() {
  const cola = state.proyectos.filter((p) => p.enEliminacion);
  if (!cola.length) {
    showToast("No hay proyectos en la lista de eliminación.", "warning");
    return;
  }
  const modal = $("#confirm-modal");
  const list = $("#confirm-list");
  const desc = $("#confirm-desc");
  const cancel = $("#confirm-cancel");
  const accept = $("#confirm-accept");
  if (!modal || !list || !desc || !accept) return;
  desc.textContent = `Se eliminarán ${cola.length} proyecto(s) de la instancia actual de forma permanente:`;
  list.textContent = "";
  cola.forEach((p) => list.append(el("li", "", p.nombre_proyecto)));
  state.confirmOpen = true;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  if (cancel) cancel.focus();
  else accept.focus();
}

function cerrarConfirmacion() {
  const modal = $("#confirm-modal");
  if (!modal) return;
  modal.hidden = true;
  state.confirmOpen = false;
  document.body.style.overflow = "";
  const btn = $("#btn-autorizar-cambios");
  if (btn && !btn.disabled) btn.focus();
}

/* API */
async function getJson(path, opts) {
  const res = await fetch(apiUrl(path), opts);
  if (!res.ok) throw new Error("HTTP " + res.status + " en " + path);
  return res.json();
}

/* RENDER */
function renderProjects() {
  const host = $("#sidebar-projects-list");
  if (!host) return;
  host.textContent = "";
  const total = state.proyectos.filter((p) => !p.enEliminacion).length;
  const activos = state.proyectos
    .filter((p) => !p.enEliminacion)
    .filter(coincideBusqueda)
    .sort(
      (a, b) =>
        (b.meses_inactivo || 0) - (a.meses_inactivo || 0) ||
        a.nombre_proyecto.localeCompare(b.nombre_proyecto, "es"),
    );
  const count = $("#projects-count");
  if (count) {
    count.textContent = state.filtroTexto
      ? `Proyectos candidatos · ${activos.length} de ${total}`
      : `Proyectos candidatos · ${total}`;
  }
  if (!activos.length) {
    const p = el(
      "p",
      "state-sub",
      total
        ? "Ningún proyecto coincide con la búsqueda."
        : "No hay proyectos pendientes.",
    );
    p.style.textAlign = "center";
    host.append(p);
    return;
  }
  for (const p of activos) {
    const b = el(
      "button",
      "btn-project" +
        (state.seleccionado &&
        String(state.seleccionado.id_proyecto) === String(p.id_proyecto)
          ? " active"
          : ""),
    );
    b.type = "button";
    b.title = `Mod: ${p.ultima_modificacion || "-"} | Ejec: ${p.ultima_ejecucion || "-"} | Dueño: ${p.dueno || "-"}`;
    b.append(el("span", "project-name", p.nombre_proyecto));
    const meta = el("span", "project-meta");
    if (p.meses_inactivo !== undefined) {
      meta.append(el("span", "project-badge", `${p.meses_inactivo}m inactivo`));
    }
    if (p.dueno && p.dueno !== "-") {
      meta.append(el("span", "project-owner", p.dueno));
    }
    if (meta.childNodes.length) b.append(meta);
    b.addEventListener("click", () => seleccionarProyecto(p.id_proyecto));
    host.append(b);
  }
}

function renderDeleteQueue() {
  const host = $("#lista-eliminar");
  const cola = state.proyectos.filter((p) => p.enEliminacion);
  const count = $("#delete-count");
  if (count) count.textContent = String(cola.length);
  updateAuthorizeButton();
  if (!host) return;
  host.textContent = "";
  if (!cola.length) {
    host.append(el("p", "state-sub", "Sin proyectos por eliminar."));
    return;
  }
  for (const p of cola) {
    const div = el("div", "delete-item");
    const info = el("div", "delete-item-info");
    const strong = document.createElement("strong");
    strong.textContent = p.nombre_proyecto;
    info.append(strong);
    const btn = el("button", "btn-revertir", "Revertir");
    btn.type = "button";
    btn.setAttribute("data-id-proyecto", String(p.id_proyecto));
    btn.addEventListener("click", () => revertirProyecto(p.id_proyecto));
    div.append(info, btn);
    host.append(div);
  }
}

function resetDashboard(msg) {
  const sub = $("#subtitulo-instancia-actual");
  if (sub) sub.textContent = "Instancia actual";
  const title = $("#titulo-proyecto-seleccionado");
  if (title) title.textContent = "Seleccione un proyecto";
  const cont = $("#contenedor-grafica-backend");
  if (cont) {
    cont.classList.add("hidden");
    cont.innerHTML = "";
  }
  const m1 = $("#metric-last-mod");
  if (m1) m1.textContent = "-";
  const m2 = $("#metric-last-exec");
  if (m2) m2.textContent = "-";
  const extra = $("#detalle-proyecto-extra");
  if (extra) extra.textContent = msg || "";
  const add = $("#btn-agregar-eliminacion");
  if (add) add.classList.add("hidden");
}

/* Gráficas  */
const COLORES_UMBRAL = {
  4: "#FF1B44",
  3: "#FF5F00",
  2: "#FA8D5A",
  1: "#00AD59",
};
function construirSvgActividad(meses, valores, cortes) {
  const ancho = 560,
    alto = 200,
    mIzq = 30,
    mDer = 10,
    mSup = 16,
    mInf = 30;
  const utilW = ancho - mIzq - mDer,
    utilH = alto - mSup - mInf;
  const n = valores.length;
  const maxV = Math.max(1, ...valores);
  const posX = (i) => mIzq + (n === 1 ? 0 : (i / (n - 1)) * utilW);
  const posY = (v) => mSup + utilH - (v / maxV) * utilH;
  const puntos = valores.map((v, i) => `${posX(i)},${posY(v)}`).join(" ");
  const circulos = valores
    .map(
      (v, i) =>
        `<circle cx="${posX(i)}" cy="${posY(v)}" r="3" style="fill:var(--ink)"></circle>`,
    )
    .join("");
  const ejes = meses
    .map(
      (m, i) =>
        `<text x="${posX(i)}" y="${alto - 8}" font-size="9" text-anchor="middle" style="fill:var(--ink-mid)">${escapeHtml(m)}</text>`,
    )
    .join("");
  const lineas = (cortes || [])
    .filter((c) => c.idx >= 0 && c.idx < n)
    .sort((a, b) => a.umbral - b.umbral)
    .map((c) => {
      const x = posX(c.idx);
      const color = COLORES_UMBRAL[c.umbral] || "#FF1B44";
      return `<line x1="${x}" y1="${mSup}" x2="${x}" y2="${alto - mInf}" stroke="${color}" stroke-width="${c.umbral === 4 ? 2 : 1.5}" stroke-dasharray="4,3"></line><text x="${x}" y="${mSup - 4}" font-size="8" fill="${color}" text-anchor="middle">Umbral ${c.umbral}M</text>`;
    })
    .join("");
  return `<svg class="grafica-actividad-svg" viewBox="0 0 ${ancho} ${alto}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Actividad histórica"><line x1="${mIzq}" y1="${mSup}" x2="${mIzq}" y2="${alto - mInf}" stroke="#A0D6E2" stroke-width="1"></line><line x1="${mIzq}" y1="${alto - mInf}" x2="${ancho - mDer}" y2="${alto - mInf}" stroke="#A0D6E2" stroke-width="1"></line>${lineas}<polyline points="${puntos}" fill="none" stroke="#009CC6" stroke-width="2"></polyline>${circulos}${ejes}</svg>`;
}

function construirHtmlEstructura(datasets, recetas, escenarios) {
  const items = [
    { label: "Datasets", valor: datasets || 0 },
    { label: "Recetas", valor: recetas || 0 },
    { label: "Escenarios", valor: escenarios || 0 },
  ];
  const maxV = Math.max(1, ...items.map((i) => i.valor));
  const filas = items
    .map((it) => {
      const pct = Math.round((it.valor / maxV) * 100);
      return `<div class="grafica-estructura-fila"><span>${escapeHtml(it.label)}</span><div class="grafica-estructura-barra-fondo"><div class="grafica-estructura-barra-relleno" style="width:${pct}%"></div></div><span>${escapeHtml(String(it.valor))}</span></div>`;
    })
    .join("");
  return `<div class="grafica-estructura-lista">${filas}</div>`;
}

function renderDashboard(detalle) {
  const p = state.seleccionado;
  if (!p) {
    resetDashboard();
    return;
  }
  $("#subtitulo-instancia-actual").textContent = "Instancia actual";
  $("#titulo-proyecto-seleccionado").textContent = p.nombre_proyecto;
  $("#btn-agregar-eliminacion").classList.remove("hidden");
  const cont = $("#contenedor-grafica-backend");
  const act = detalle && detalle.actividad;
  const est = detalle && detalle.estructura;
  if (act && est) {
    cont.classList.remove("hidden");
    cont.innerHTML =
      `<div class="grafica-panel"><h4>Nivel de Actividad (Histórico)</h4>${construirSvgActividad(act.meses, act.valores, act.cortes)}</div>` +
      `<div class="grafica-panel"><h4>Estructura del Proyecto</h4>${construirHtmlEstructura(est.datasets, est.recetas, est.escenarios)}</div>`;
  }
  $("#metric-last-mod").textContent = p.ultima_modificacion || "-";
  $("#metric-last-exec").textContent = p.ultima_ejecucion || "-";
  const extra = $("#detalle-proyecto-extra");
  if (extra) {
    const partes = [];
    if (p.meses_inactivo !== undefined)
      partes.push(`${p.meses_inactivo}m inactivo`);
    if (p.dueno) partes.push(`Dueño: ${p.dueno}`);
    if (p.creado) partes.push(`Creado: ${p.creado}`);
    if (p.motivo) partes.push(p.motivo);
    extra.textContent = partes.join(" · ");
  }
}

/* FLOW */
async function cargarProyectos() {
  const reqId = ++proyectosReqId;
  detalleReqId++;
  state.detailBusy = false;
  const projectSlip = $("#project-slip");
  if (projectSlip) projectSlip.setAttribute("aria-busy", "false");
  const host = $("#sidebar-projects-list");
  const sel = $("#select-umbral-tiempo");
  const search = $("#buscador-proyectos");
  if (host) {
    host.innerHTML = `<p class="state-sub" style="text-align:center">Analizando actividad de proyectos…</p>`;
    host.setAttribute("aria-busy", "true");
  }
  if (sel) sel.disabled = true;
  if (search) search.disabled = true;
  state.listBusy = true;
  const previos = new Set(
    state.proyectos
      .filter((p) => p.enEliminacion)
      .map((p) => String(p.id_proyecto)),
  );
  let errorCarga = null;
  try {
    const data = await getJson(API.inactivos(state.profundidad));
    if (reqId !== proyectosReqId) return;
    if (data && data.status === "ok" && Array.isArray(data.datos)) {
      state.proyectos = data.datos.map((p) => ({
        id_proyecto: p.id_proyecto,
        nombre_proyecto: p.nombre_proyecto,
        ultima_modificacion: p.ultima_modificacion || "-",
        ultima_ejecucion: p.ultima_ejecucion || "-",
        dueno: p.dueno || "-",
        creado: p.creado || "-",
        meses_inactivo: p.meses_inactivo ?? 0,
        motivo: p.motivo || "",
        enEliminacion: previos.has(String(p.id_proyecto)),
      }));
    } else {
      throw new Error((data && data.message) || "respuesta inesperada");
    }
  } catch (err) {
    if (reqId !== proyectosReqId) return;
    errorCarga = err;
    state.proyectos = [];
  }
  state.listBusy = false;
  if (sel) sel.disabled = false;
  if (search) search.disabled = false;
  if (host) host.setAttribute("aria-busy", "false");
  state.seleccionado = null;
  resetDashboard();
  renderProjects();
  renderDeleteQueue();
  if (errorCarga) {
    showToast(
      "No se pudieron cargar los proyectos — " +
        (errorCarga.message || errorCarga),
      "error",
    );
  }
}

function seleccionarProyecto(id) {
  const p = state.proyectos.find((x) => String(x.id_proyecto) === String(id));
  if (!p || p.enEliminacion) return;
  state.seleccionado = p;
  renderProjects();
  consultarDetalle(p);
}

async function consultarDetalle(p) {
  const reqId = ++detalleReqId;
  state.detailBusy = true;
  const slip = $("#project-slip");
  if (slip) slip.setAttribute("aria-busy", "true");
  renderDashboard(null);
  const extra = $("#detalle-proyecto-extra");
  if (extra) extra.textContent = "Analizando actividad del proyecto…";
  try {
    const data = await getJson(API.analizar, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        proyecto_id: p.id_proyecto,
        umbral_meses: state.profundidad,
      }),
    });
    if (reqId !== detalleReqId) return;
    if (data && data.status === "ok") {
      if (data.metricas && data.metricas.decision === "Preservar") {
        state.proyectos = state.proyectos.filter(
          (x) => String(x.id_proyecto) !== String(p.id_proyecto),
        );
        state.seleccionado = null;
        renderProjects();
        renderDeleteQueue();
        resetDashboard(
          "El proyecto mostró actividad reciente y ya no es candidato.",
        );
        return;
      }
      if (data.metricas) {
        p.ultima_modificacion =
          data.metricas.ultima_modificacion ?? p.ultima_modificacion;
        p.ultima_ejecucion =
          data.metricas.ultima_ejecucion ?? p.ultima_ejecucion;
        p.dueno = data.metricas.dueno || p.dueno;
        p.creado = data.metricas.creado || p.creado;
        p.meses_inactivo = data.metricas.meses_inactivo ?? p.meses_inactivo;
        p.motivo = data.metricas.motivo || p.motivo;
      }
      renderDashboard(data);
      return;
    }
    throw new Error((data && data.message) || "análisis fallido");
  } catch (err) {
    if (reqId !== detalleReqId) return;
    showToast(
      "No se pudieron cargar las métricas — " + (err.message || err),
      "error",
    );
  } finally {
    if (reqId === detalleReqId) {
      state.detailBusy = false;
      if (slip) slip.setAttribute("aria-busy", "false");
    }
  }
}

function agregarAEliminacion() {
  if (!state.seleccionado) return;
  const p = state.proyectos.find(
    (x) => String(x.id_proyecto) === String(state.seleccionado.id_proyecto),
  );
  if (!p) return;
  p.enEliminacion = true;
  state.seleccionado = null;
  detalleReqId++;
  renderProjects();
  renderDeleteQueue();
  resetDashboard("Proyecto en cola. Elija el siguiente candidato.");
  showToast(
    `"${p.nombre_proyecto}" agregado a la lista de eliminación.`,
    "success",
  );
}

function revertirProyecto(id) {
  const p = state.proyectos.find((x) => String(x.id_proyecto) === String(id));
  if (!p) return;
  p.enEliminacion = false;
  renderProjects();
  renderDeleteQueue();
  showToast(`"${p.nombre_proyecto}" devuelto a candidatos.`, "info");
}

async function ejecutarLimpieza() {
  const cola = state.proyectos.filter((p) => p.enEliminacion);
  if (!cola.length) {
    cerrarConfirmacion();
    showToast("No hay proyectos en la lista de eliminación.", "warning");
    return;
  }
  cerrarConfirmacion();
  state.authBusy = true;
  const btn = $("#btn-autorizar-cambios");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Procesando…";
  }
  try {
    const data = await getJson(API.limpieza, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        proyectos_a_limpiar: cola.map((p) => ({ proyecto_id: p.id_proyecto })),
      }),
    });
    if (data && data.status === "ok") {
      const detalles = data.detalles || [];
      const fallidos = detalles.filter((d) => d.status !== "eliminado");
      const idsOk = new Set(
        detalles
          .filter((d) => d.status === "eliminado")
          .map((d) => String(d.proyecto_id)),
      );
      if (idsOk.size) {
        state.proyectos = state.proyectos.filter(
          (p) => !p.enEliminacion || !idsOk.has(String(p.id_proyecto)),
        );
      }
      renderProjects();
      renderDeleteQueue();
      const exitosos = data.exitosos ?? idsOk.size;
      const fallados = data.fallidos ?? fallidos.length;
      showToast(
        `Limpieza finalizada · ${exitosos} exitoso(s), ${fallados} fallido(s).`,
        fallidos.length ? "warning" : "success",
        fallidos.map(
          (d) =>
            `${d.proyecto_id || "Proyecto"}: ${d.message || "no se pudo eliminar"}`,
        ),
      );
    } else {
      throw new Error((data && data.message) || "limpieza fallida");
    }
  } catch (err) {
    showToast("No se pudo autorizar — " + (err.message || err), "error");
  } finally {
    state.authBusy = false;
    updateAuthorizeButton();
  }
}

/* JUMPS */
function initJumps() {
  $$("[data-jump]").forEach((b) => {
    b.addEventListener("click", () => {
      const target = $(b.getAttribute("data-jump"));
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      const input = target.querySelector("input,select,textarea,button");
      if (input && target !== input) input.focus({ preventScroll: true });
    });
  });
}

/* BOOT */
function actualizarAyudaUmbral(v) {
  const help = $("#filter-help");
  if (help) {
    help.textContent = `Proyectos sin actividad por al menos ${v} ${
      v === 1 ? "mes" : "meses"
    }.`;
  }
}

function initConfirmModal() {
  const modal = $("#confirm-modal");
  if (!modal) return;
  const cancel = $("#confirm-cancel");
  const accept = $("#confirm-accept");
  if (cancel) cancel.addEventListener("click", cerrarConfirmacion);
  if (accept) accept.addEventListener("click", ejecutarLimpieza);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) cerrarConfirmacion();
  });
  document.addEventListener("keydown", (e) => {
    if (!state.confirmOpen) return;
    if (e.key === "Escape") {
      e.preventDefault();
      cerrarConfirmacion();
      return;
    }
    if (e.key === "Tab") {
      const items = modalFocusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });
}

async function boot() {
  initJumps();
  initConfirmModal();
  const sel = $("#select-umbral-tiempo");
  if (sel)
    sel.addEventListener("change", (e) => {
      const v = parseInt(e.target.value, 10);
      state.profundidad = [1, 2, 3, 4].includes(v) ? v : PROFUNDIDAD_DEFECTO;
      actualizarAyudaUmbral(state.profundidad);
      cargarProyectos();
    });
  const search = $("#buscador-proyectos");
  if (search)
    search.addEventListener("input", (e) => {
      state.filtroTexto = normalizeText(e.target.value.trim());
      renderProjects();
    });
  const add = $("#btn-agregar-eliminacion");
  if (add) add.addEventListener("click", agregarAEliminacion);
  const auth = $("#btn-autorizar-cambios");
  if (auth) auth.addEventListener("click", abrirConfirmacion);
  actualizarAyudaUmbral(state.profundidad);
  await cargarProyectos();
}

document.addEventListener("DOMContentLoaded", boot);
window.revertirProyecto = revertirProyecto;
window.agregarAEliminacion = agregarAEliminacion;
