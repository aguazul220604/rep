# Cambios PII en `app.js`

Referencia: `data_gate/app.js` local (1575 líneas). Los números de línea son los del archivo actual; al insertar bloques, replica de abajo hacia arriba para evitar desplazamientos.

## 1. Líneas 13–14 — Estado global

Insertar después de `validationResult: null,`.

```javascript
    piiResult: null,
    approvalPii: {},
```

## 2. Línea 663 — Limpiar PII al seleccionar archivo

Insertar después de `resetSheetPicker();` en `handleFileSelect()`.

```javascript
    renderPiiAnalysis(null);
```

## 3. Línea 701 — Renderizar PII al validar

Insertar después de `if (result.checks) showValidationSummary(result.checks);`.

```javascript
        renderPiiAnalysis(result.pii);
```

## 4. Línea 710 — Ocultar PII si la validación falla

Insertar después de `uploadZone.classList.add('validation-failed');` en la rama `else`.

```javascript
        renderPiiAnalysis(null);
```

## 5. Línea 713 — Ocultar PII si hay excepción

Insertar como primera línea dentro del bloque `catch (err)`.

```javascript
      renderPiiAnalysis(null);
```

## 6. Líneas 805–920 — Funciones PII

Insertar después de `showValidationSummary()` y antes de `async function checkStorageThreshold()`.

```javascript
  function piiPct(value) {
    const n = Number(value);
    return Number.isFinite(n) ? (n * 100).toFixed(1) + '%' : '—';
  }

  function piiRowHTML(c) {
    return `<tr>
      <td><strong>${esc(c.column)}</strong></td>
      <td>${c.pii ? '<span class="status-badge error">PII</span>' : '<span class="status-badge success">No</span>'}</td>
      <td>${esc(c.entity || '—')}</td>
      <td>${piiPct(c.prob_pii)}</td>
      <td>${piiPct(c.prob_entity)}</td>
      <td>${esc(c.evidence || '—')}</td>
      <td>${piiPct(c.match_rate)}</td>
      <td>${c.needs_review ? '<span class="status-badge pending">Review</span>' : '—'}</td>
    </tr>`;
  }

  function piiSummaryLine(pii) {
    if (!pii || pii.status !== 'ok') return 'PII summary unavailable.';
    const review = Number(pii.needs_review) || 0;
    return `PII: ${Number(pii.pii_columns) || 0} of ${Number(pii.analyzed_columns) || 0} columns` +
      (review > 0 ? ` · ${review} need review` : '');
  }

  function piiBadgeCell(pii) {
    if (!pii || pii.status !== 'ok') return '<span style="color:var(--ink-faint)">—</span>';
    const cls = (Number(pii.pii_columns) || 0) > 0 ? 'error' : 'success';
    return `<span class="status-badge ${cls}">${Number(pii.pii_columns) || 0}/${Number(pii.analyzed_columns) || 0}</span>`;
  }

  function piiDetailTableHTML(pii) {
    const cols = (pii && pii.status === 'ok' && Array.isArray(pii.columns)) ? pii.columns : [];
    const body = cols.length
      ? cols.map(piiRowHTML).join('')
      : '<tr><td colspan="8" style="text-align:center;color:var(--ink-faint)">No PII or review columns recorded.</td></tr>';
    return `<div class="table-wrapper">
      <table class="data-table">
        <thead>
          <tr>
            <th>Column</th><th>PII</th><th>Entity</th><th>PII Prob.</th>
            <th>Entity Prob.</th><th>Evidence</th><th>Match Rate</th><th>Review</th>
          </tr>
        </thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
  }

  function renderPiiAnalysis(pii) {
    const section = $('pii-analysis-section');
    if (!section) return;
    if (!pii || pii.status !== 'ok') {
      state.piiResult = null;
      section.style.display = 'none';
      $('pii-summary-grid').innerHTML = '';
      $('pii-results-tbody').innerHTML = '';
      $('pii-results-wrapper').style.display = 'none';
      $('pii-note').style.display = 'none';
      return;
    }

    state.piiResult = pii;
    const total = Number(pii.analyzed_columns) || 0;
    const piiCols = Number(pii.pii_columns) || 0;
    const review = Number(pii.needs_review) || 0;
    $('pii-summary-badge').textContent = `${total} columns`;

    const items = [
      { label: `${total} columns analyzed`, status: 'pass' },
      { label: `${piiCols} PII columns detected`, status: piiCols > 0 ? 'warn' : 'pass' },
      { label: `${review} require review`, status: review > 0 ? 'warn' : 'pass' }
    ];
    $('pii-summary-grid').innerHTML = items.map(item => {
      const icon = item.status === 'pass' ? 'fa-check-circle' : 'fa-exclamation-circle';
      return `<div class="validation-item ${item.status}"><i class="fas ${icon}"></i><span class="validation-item-text">${esc(item.label)}</span></div>`;
    }).join('');

    const cols = Array.isArray(pii.columns) ? pii.columns : [];
    $('pii-results-tbody').innerHTML = cols.length
      ? cols.map(piiRowHTML).join('')
      : '<tr><td colspan="8" style="text-align:center;color:var(--ink-faint)">No PII or review columns detected.</td></tr>';
    $('pii-results-wrapper').style.display = 'none';
    $('pii-note').style.display = 'block';
    $('pii-note').textContent = `Engine ${pii.engine_version || '—'} · Models ${pii.model_version || '—'} · Config ${pii.config_version || '—'}`;

    const btn = $('pii-analyze-btn');
    btn.disabled = false;
    btn.innerHTML = '<i class="fas fa-search"></i> View PII details';
    section.style.display = 'block';
  }

  function showPiiDetailModal(pii, uploadId) {
    showModal('PII Analysis', `
      <div class="confirm-panel">
        <div class="confirm-panel-head">
          <span class="confirm-panel-icon"><i class="fas fa-user-shield"></i></span>
          <div class="confirm-panel-titles">
            <strong>${esc(uploadId || 'Upload')}</strong>
            <span>${esc(piiSummaryLine(pii))}</span>
          </div>
        </div>
        ${piiDetailTableHTML(pii)}
      </div>
    `, () => hideModal(), '<i class="fas fa-times"></i> Close', 'btn-secondary');
  }

  $('pii-analyze-btn').addEventListener('click', () => {
    const wrapper = $('pii-results-wrapper');
    if (!wrapper) return;
    const show = wrapper.style.display === 'none' || !wrapper.style.display;
    wrapper.style.display = show ? 'block' : 'none';
    $('pii-analyze-btn').innerHTML = show
      ? '<i class="fas fa-eye-slash"></i> Hide PII details'
      : '<i class="fas fa-search"></i> View PII details';
  });
```

## 7. Línea 946 — Limpiar estado PII en `resetUpload()`

Insertar después de `state.validationResult = null;`.

```javascript
    state.piiResult = null;
```

## 8. Líneas 956–962 — Limpiar DOM PII en `resetUpload()`

Insertar después de `$('validation-summary').style.display = 'none';`.

```javascript
    $('pii-analysis-section').style.display = 'none';
    $('pii-results-wrapper').style.display = 'none';
    $('pii-results-tbody').innerHTML = '';
    $('pii-summary-grid').innerHTML = '';
    $('pii-summary-badge').textContent = '';
    $('pii-note').style.display = 'none';
    $('pii-analyze-btn').innerHTML = '<i class="fas fa-search"></i> View PII details';
```

## 9. Línea 1012 — Fila PII en el modal de confirmación

Insertar después del `confirm-row` de `Target project`.

```javascript
          ${state.piiResult && state.piiResult.status === 'ok' ? `<div class="confirm-row"><span class="confirm-row-label"><i class="fas fa-user-shield"></i> PII</span><span class="confirm-row-value">${esc(piiSummaryLine(state.piiResult))}</span></div>` : ''}
```

## 10. Línea 1042 — Enviar `pii_summary` en metadata

Insertar después de `uploaded_by: state.currentUser.username,` dentro del `JSON.stringify({...})`.

```javascript
          pii_summary: state.piiResult || null
```

## 11. Línea 1105 — Celda PII en My Uploads

Insertar entre `<td>${getStatusBadge(u.status)}</td>` y `<td>${u.approver_notes ...}</td>`.

```javascript
        <td>${piiBadgeCell(u.pii)}</td>
```

## 12. Líneas 1141 y 1146 — Guardar PII de la cola de aprobación

Insertar `state.approvalPii = {};` después de `const empty = $('empty-approval');`.

```javascript
    state.approvalPii = {};
```

Insertar `state.approvalPii[item.id] = item.pii || null;` como primera línea dentro de `approvals.forEach(item => {`.

```javascript
      state.approvalPii[item.id] = item.pii || null;
```

## 13. Líneas 1163–1166 — Resumen PII en cada aprobación

Insertar después de `<div class="approval-item-desc">${item.description}</div>`.

```javascript
        <div class="approval-item-desc" style="margin-top:6px;">
          <i class="fas fa-user-shield"></i> ${esc(piiSummaryLine(item.pii))}
          ${item.pii && item.pii.status === 'ok' ? `<button class="btn btn-sm btn-secondary pii-detail-btn" data-id="${item.id}"><i class="fas fa-search"></i> PII details</button>` : ''}
        </div>
```

## 14. Línea 1176 — Listener del botón PII

Insertar después de los listeners de `.approve-btn` y `.reject-btn`, dentro de `renderApprovalQueue()`.

```javascript
    queue.querySelectorAll('.pii-detail-btn').forEach(btn => btn.addEventListener('click', () => showPiiDetailModal(state.approvalPii[btn.dataset.id], btn.dataset.id)));
```

## 15. Líneas 1180–1188 — Bloque PII en el modal del Approver

Insertar después de `const color = action === 'approve' ? 'var(--ok)' : 'var(--err)';` en `handleApproverAction()`.

```javascript
    const pii = state.approvalPii[uploadId] || null;
    const piiBlock = `
      <div class="confirm-callout" style="margin-bottom:12px;">
        <i class="fas fa-user-shield"></i>
        <span>${esc(piiSummaryLine(pii))}</span>
      </div>
      ${pii && pii.status === 'ok' && Array.isArray(pii.columns) && pii.columns.length
        ? `<details style="margin-bottom:12px;"><summary>PII details (${pii.columns.length} columns)</summary>${piiDetailTableHTML(pii)}</details>`
        : ''}`;
```

## 16. Línea 1205 — Insertar `piiBlock` en el modal

Insertar antes de `${snowBlock}` dentro del HTML de `showModal(...)`.

```javascript
      ${piiBlock}
```
