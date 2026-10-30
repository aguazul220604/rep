# Cambios PII en `index.html`

Referencia: `data_gate/index.html` local (893 líneas). Los números de línea son los del archivo actual; al insertar bloques, replica de abajo hacia arriba para evitar desplazamientos.

## 1. Líneas 467–493 — Tarjeta PII del Paso 2

Insertar después del bloque `validation-summary` existente y antes de `volume-warning`.

```html
                    <div class="validation-summary" id="pii-analysis-section" style="display:none;">
                        <h3><i class="fas fa-user-shield"></i> PII Analysis <span class="preview-rows-count" id="pii-summary-badge"></span></h3>
                        <div class="validation-grid" id="pii-summary-grid"></div>
                        <div style="margin-top:12px;">
                            <button type="button" class="btn btn-sm btn-secondary" id="pii-analyze-btn">
                                <i class="fas fa-search"></i> View PII details
                            </button>
                        </div>
                        <div class="table-wrapper" id="pii-results-wrapper" style="display:none; margin-top:12px;">
                            <table class="data-table" id="pii-results-table">
                                <thead>
                                    <tr>
                                        <th>Column</th>
                                        <th>PII</th>
                                        <th>Entity</th>
                                        <th>PII Prob.</th>
                                        <th>Entity Prob.</th>
                                        <th>Evidence</th>
                                        <th>Match Rate</th>
                                        <th>Review</th>
                                    </tr>
                                </thead>
                                <tbody id="pii-results-tbody"></tbody>
                            </table>
                        </div>
                        <p class="preview-note" id="pii-note" style="display:none;"></p>
                    </div>
```

## 2. Línea 581 — Columna PII en My Uploads

Insertar dentro del `<thead>` de la tabla `my-uploads-tbody`, después de `<th>Status</th>` y antes de `<th>Approver Notes</th>`.

```html
                                    <th>PII</th>
```
