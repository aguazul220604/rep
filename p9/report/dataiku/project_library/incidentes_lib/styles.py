# -*- coding: utf-8 -*-
"""CSS y logo inline para la webapp de Dataiku."""

APP_CSS = r""":root {
  --teal-950: #002a34;
  --teal-900: #00323d;
  --teal-800: #003b49;
  --teal-700: #004452;
  --teal-600: #005a6e;
  --red: #e1251b;
  --red-dark: #b71c16;
  --text: #e8f1f2;
  --muted: #9bb8bd;
  --line: rgba(255, 255, 255, 0.14);
  --white: #ffffff;
  --table-text: #1b2b30;
}

* {
  box-sizing: border-box;
}

html,
body {
  margin: 0;
  padding: 0;
  background: var(--teal-800);
  color: var(--text);
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
}

body {
  min-height: 100vh;
}

.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, #003b49 0%, #003743 100%);
}

.app-header {
  min-height: 94px;
  background: var(--teal-900);
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 17px 38px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 17px;
}

.brand-logo {
  width: 60px;
  height: 60px;
  flex: 0 0 auto;
}

.brand-text h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
  line-height: 1.05;
  letter-spacing: 0.2px;
}

.brand-text p {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 14px;
}

.header-date {
  color: var(--muted);
  font-size: 12px;
  text-align: right;
}

.page {
  width: 100%;
  max-width: 1460px;
  margin: 0 auto;
  padding: 24px 38px 40px;
  flex: 1;
}

.page-description {
  max-width: 1120px;
  margin: 0 0 24px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}

.area-tabs {
  width: 100%;
}

.area-tabs-list {
  display: flex !important;
  justify-content: flex-start;
  gap: 26px;
  border: none !important;
  background: transparent !important;
  margin-bottom: 20px;
}

.area-tab {
  flex: 0 0 auto !important;
  width: auto !important;
  min-width: 190px;
  background: #ffffff !important;
  color: var(--teal-800) !important;
  border: none !important;
  border-radius: 12px !important;
  padding: 14px 52px !important;
  font-size: 20px !important;
  font-weight: 700 !important;
  line-height: 1.1 !important;
  text-align: center;
  cursor: pointer;
  box-shadow: 0 1px 0 rgba(0, 0, 0, 0.08);
}

.area-tab:hover {
  background: #f2f7f7 !important;
}

.area-tab--selected,
.area-tab--selected:hover {
  background: var(--red) !important;
  color: #ffffff !important;
}

.area-tabs-content {
  padding: 0 !important;
  border: none !important;
}

.upload-section {
  margin-bottom: 10px;
}

.upload-label {
  margin: 0 0 9px;
  color: var(--muted);
  font-size: 13px;
}

.upload-row {
  display: flex;
  align-items: stretch;
  gap: 22px;
  max-width: 1120px;
}

#upload-data {
  flex: 1 1 760px;
  max-width: 900px;
  min-width: 0;
}

.upload-control {
  width: 100%;
  height: 48px;
  border: 1px solid rgba(255, 255, 255, 0.36);
  border-radius: 4px;
  background: #ffffff;
  color: var(--teal-800);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.upload-control:hover {
  border-color: rgba(255, 255, 255, 0.75);
  box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.08);
}

.upload-inner {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 17px;
}

.upload-icon {
  width: 25px;
  height: 25px;
  border-radius: 50%;
  background: #e8f1f2;
  color: var(--teal-800);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  font-weight: 700;
  flex: 0 0 auto;
}

.upload-title {
  display: block;
  color: var(--teal-800);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.2;
}

.upload-hint {
  display: block;
  color: #5e7b82;
  font-size: 10px;
  line-height: 1.3;
}

.upload-filename {
  margin-top: 7px;
  color: var(--muted);
  font-size: 12px;
}

.upload-flow-note {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 10px;
}

.btn-analyze {
  height: 48px;
  padding: 0 26px;
  border: none;
  border-radius: 4px;
  background: #ffffff;
  color: var(--teal-800);
  font-family: inherit;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s ease;
}

.btn-analyze:hover:not(:disabled) {
  background: #e8f1f2;
}

.btn-analyze:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.error-box {
  margin-top: 8px;
  color: #ffb4ae;
  font-size: 12px;
  line-height: 1.5;
}

.error-box:empty {
  display: none;
}

.analysis-meta {
  margin: 15px 0 9px;
  color: var(--muted);
  font-size: 12px;
}

.analysis-meta:empty {
  display: none;
}

.filters {
  display: grid;
  grid-template-columns: 1.35fr 1fr 1.2fr 1.4fr auto;
  gap: 14px;
  align-items: end;
  margin: 14px 0 18px;
  padding: 14px 16px;
  background: rgba(0, 0, 0, 0.1);
  border: 1px solid var(--line);
  border-radius: 10px;
}

.filter-field {
  min-width: 0;
}

.filter-label {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.date-filter {
  width: 100%;
}

.date-filter .DateRangePickerInput {
  width: 100%;
  display: flex;
  align-items: center;
  border: 1px solid #cfdcdf;
  border-radius: 4px;
  background: #ffffff;
}

.date-filter .DateInput {
  width: 47%;
  background: #ffffff;
  border-radius: 4px;
}

.date-filter .DateInput_input {
  padding: 7px 8px;
  border: none;
  color: var(--table-text);
  background: #ffffff;
  font-family: inherit;
  font-size: 12px;
  font-weight: 400;
}

.date-filter .DateRangePickerInput_arrow {
  color: #5e7b82;
}

.date-filter .DateRangePickerInput_clearDates {
  margin: 0 5px;
}

.dark-dropdown .Select-control {
  min-height: 36px;
  height: 36px;
  border: 1px solid #cfdcdf;
  border-radius: 4px;
  background: #ffffff;
  color: var(--table-text);
  font-size: 12px;
}

.dark-dropdown .Select-placeholder,
.dark-dropdown .Select-value-label,
.dark-dropdown .Select-input > input {
  color: var(--table-text) !important;
  font-size: 12px;
}

.dark-dropdown .Select-value {
  background: #e8f1f2 !important;
  border-color: #b8ced2 !important;
  color: var(--table-text) !important;
}

.dark-dropdown .Select-value-icon {
  border-color: #b8ced2 !important;
  color: var(--table-text) !important;
}

.dark-dropdown .Select-menu-outer {
  z-index: 60;
  background: #ffffff;
  border-color: #cfdcdf;
  color: var(--table-text);
  font-size: 12px;
}

.dark-dropdown .VirtualizedSelectOption {
  background: #ffffff;
  color: var(--table-text);
}

.dark-dropdown .VirtualizedSelectFocusedOption {
  background: #e8f1f2;
  color: var(--table-text);
}

.btn-ghost {
  height: 36px;
  padding: 0 16px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.btn-ghost:hover:not(:disabled) {
  color: #ffffff;
  border-color: rgba(255, 255, 255, 0.4);
}

.btn-ghost:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 14px;
  margin: 18px 0 22px;
}

.kpi-card {
  position: relative;
  overflow: hidden;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: linear-gradient(145deg, #004a59, #003e4c);
}

.kpi-card::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 4px;
  background: var(--red);
}

.kpi-label {
  margin: 0;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.kpi-value {
  margin: 7px 0 3px;
  color: #ffffff;
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
}

.kpi-hint {
  margin: 0;
  color: var(--muted);
  font-size: 10px;
}

.primary-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(320px, 1fr);
  grid-template-areas:
    "daily side"
    "apps side";
  gap: 18px;
  align-items: stretch;
}

.daily-card {
  grid-area: daily;
}

.side-card {
  grid-area: side;
  display: flex;
  flex-direction: column;
}

.apps-card {
  grid-area: apps;
}

.chart-card {
  min-width: 0;
  padding: 14px 14px 10px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: linear-gradient(145deg, #004452, #003c4a);
}

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}

.card-titles h3 {
  margin: 0;
  color: #ffffff;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.25;
}

.card-titles h2 {
  margin: 0;
  color: #ffffff;
  font-size: 15px;
  font-weight: 600;
}

.card-titles p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.35;
}

.chart-graph {
  width: 100%;
}

.side-charts {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: space-evenly;
  gap: 2px;
}

.mini-chart {
  min-width: 0;
}

.mini-title {
  margin: 0 0 0 7px;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.peak-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  color: var(--muted);
  font-size: 10px;
  white-space: nowrap;
}

.peak-toggle label {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}

.peak-check {
  accent-color: var(--red);
}

.extra-section {
  margin-top: 24px;
}

.section-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin-bottom: 12px;
}

.section-head h2 {
  margin: 0;
  color: #ffffff;
  font-size: 16px;
  font-weight: 600;
}

.section-head p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}

.extra-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px;
}

.download-row {
  display: flex;
  justify-content: flex-end;
  margin: 20px 0 18px;
}

.btn-download {
  padding: 12px 26px;
  border: none;
  border-radius: 4px;
  background: #ffffff;
  color: var(--teal-800);
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.16);
}

.btn-download:hover:not(:disabled) {
  background: #e8f1f2;
}

.btn-download:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.table-card {
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 12px;
  background: #ffffff;
}

.table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px;
  background: var(--teal-900);
  color: #ffffff;
}

.table-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.table-head p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 11px;
}

.table-meta {
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}

.table-card .dash-table-container {
  background: #ffffff;
}

.table-card .dash-spreadsheet-container .dash-spreadsheet-inner table {
  border: none;
}

.table-card .dash-spreadsheet-container .dash-spreadsheet-inner td,
.table-card .dash-spreadsheet-container .dash-spreadsheet-inner th {
  border: none !important;
}

.table-card .previous-next-container {
  padding: 10px 14px;
  color: var(--table-text);
  background: #ffffff;
  font-size: 11px;
}

.table-card .page-number {
  color: var(--table-text);
  font-size: 11px;
}

.table-card .current-page {
  color: var(--teal-800);
  font-weight: 700;
}

.table-card .first-page,
.table-card .previous-page,
.table-card .next-page,
.table-card .last-page {
  fill: var(--teal-800);
}

.empty-module {
  min-height: 420px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 30px;
  text-align: center;
  border: 1px dashed var(--line);
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.12);
}

.empty-module-title {
  color: #ffffff;
  font-size: 24px;
  font-weight: 700;
}

.empty-module p {
  max-width: 520px;
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.6;
}

.empty-badge {
  padding: 4px 12px;
  border: 1px solid rgba(225, 37, 27, 0.45);
  border-radius: 999px;
  background: rgba(225, 37, 27, 0.08);
  color: #ff9d96;
  font-size: 11px;
}

.app-footer {
  display: flex;
  justify-content: flex-end;
  padding: 16px 38px;
  border-top: 1px solid var(--line);
  background: var(--teal-900);
  color: var(--muted);
  font-size: 12px;
}

.overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  align-items: center;
  justify-content: center;
  background: rgba(0, 32, 40, 0.55);
  backdrop-filter: blur(2px);
}

.overlay-modal {
  min-width: 340px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 18px;
  padding: 42px 36px;
  border: 2px solid rgba(255, 255, 255, 0.55);
  border-radius: 4px;
  background: var(--teal-900);
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.4);
}

.overlay-modal p {
  margin: 0;
  color: #ffffff;
  font-size: 13px;
}

.spinner {
  width: 34px;
  height: 34px;
  border: 3px solid rgba(255, 255, 255, 0.25);
  border-top-color: var(--red);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.js-plotly-plot .plotly .main-svg {
  background: transparent !important;
}

@media (max-width: 1150px) {
  .kpi-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .extra-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .filters {
    grid-template-columns: 1fr 1fr;
  }

  .btn-ghost {
    width: fit-content;
  }
}

@media (max-width: 860px) {
  .app-header {
    padding: 16px 20px;
  }

  .page {
    padding: 22px 20px 32px;
  }

  .brand-logo {
    width: 50px;
    height: 50px;
  }

  .brand-text h1 {
    font-size: 20px;
  }

  .brand-text p {
    font-size: 12px;
  }

  .area-tabs-list {
    gap: 14px;
  }

  .area-tab {
    padding: 12px 28px !important;
    font-size: 16px !important;
  }

  .primary-grid {
    grid-template-columns: 1fr;
    grid-template-areas:
      "daily"
      "side"
      "apps";
  }

  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .extra-grid {
    grid-template-columns: 1fr;
  }

  .filters {
    grid-template-columns: 1fr;
  }

  .upload-row {
    flex-direction: column;
  }

  #upload-data {
    flex: 0 0 auto;
    width: 100%;
    max-width: none;
  }

  .btn-analyze {
    width: 100%;
  }

  .table-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .app-footer {
    padding: 14px 20px;
  }
}

@media (max-width: 520px) {
  .kpi-grid {
    grid-template-columns: 1fr;
  }

  .header-date {
    display: none;
  }

  .area-tabs-list {
    gap: 10px;
  }

  .area-tab {
    flex: 1 1 0 !important;
    min-width: 0;
    padding: 11px 10px !important;
    font-size: 14px !important;
  }

  .page-description {
    font-size: 12px;
  }

  .overlay-modal {
    min-width: 0;
    width: calc(100vw - 48px);
  }
}
"""

LOGO_DATA_URI = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCIgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByb2xlPSJpbWciIGFyaWEtbGFiZWw9IkJhbmFtZXgiPgogIDxnIGZpbGw9IiNFMTI1MUIiPgogICAgPGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMzIgMzIpIj4KICAgICAgPGNpcmNsZSByPSIyMSIvPgogICAgICA8cmVjdCB4PSItNC41IiB5PSItMzEiIHdpZHRoPSI5IiBoZWlnaHQ9IjE1IiByeD0iMi41Ii8+CiAgICAgIDxyZWN0IHg9Ii00LjUiIHk9Ii0zMSIgd2lkdGg9IjkiIGhlaWdodD0iMTUiIHJ4PSIyLjUiIHRyYW5zZm9ybT0icm90YXRlKDQ1KSIvPgogICAgICA8cmVjdCB4PSItNC41IiB5PSItMzEiIHdpZHRoPSI5IiBoZWlnaHQ9IjE1IiByeD0iMi41IiB0cmFuc2Zvcm09InJvdGF0ZSg5MCkiLz4KICAgICAgPHJlY3QgeD0iLTQuNSIgeT0iLTMxIiB3aWR0aD0iOSIgaGVpZ2h0PSIxNSIgcng9IjIuNSIgdHJhbnNmb3JtPSJyb3RhdGUoMTM1KSIvPgogICAgICA8cmVjdCB4PSItNC41IiB5PSItMzEiIHdpZHRoPSI5IiBoZWlnaHQ9IjE1IiByeD0iMi41IiB0cmFuc2Zvcm09InJvdGF0ZSgxODApIi8+CiAgICAgIDxyZWN0IHg9Ii00LjUiIHk9Ii0zMSIgd2lkdGg9IjkiIGhlaWdodD0iMTUiIHJ4PSIyLjUiIHRyYW5zZm9ybT0icm90YXRlKDIyNSkiLz4KICAgICAgPHJlY3QgeD0iLTQuNSIgeT0iLTMxIiB3aWR0aD0iOSIgaGVpZ2h0PSIxNSIgcng9IjIuNSIgdHJhbnNmb3JtPSJyb3RhdGUoMjcwKSIvPgogICAgICA8cmVjdCB4PSItNC41IiB5PSItMzEiIHdpZHRoPSI5IiBoZWlnaHQ9IjE1IiByeD0iMi41IiB0cmFuc2Zvcm09InJvdGF0ZSgzMTUpIi8+CiAgICA8L2c+CiAgICA8Y2lyY2xlIGN4PSIzMiIgY3k9IjMyIiByPSI5IiBmaWxsPSIjMDAzQjQ5Ii8+CiAgPC9nPgo8L3N2Zz4K"
