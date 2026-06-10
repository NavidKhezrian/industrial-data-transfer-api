// ============================================================
// Data Transfer Console UI
// ------------------------------------------------------------
// Handles token validation, request building, result rendering,
// storage notices, and readable multipart transfer summaries.
// ============================================================

const AppState = {
  uiOptions: null,
  lastRawResponse: null,
  receiverToken: "",
  nextFilterId: 0,
  progressTimerId: null,
  progressStartedAt: null,
  progressStageIndex: 0,
};

// ============================================================
// DOM helpers
// ============================================================
function getElement(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  const element = getElement(id);
  if (element) {
    element.textContent = value;
  }
}

function setHtml(id, html) {
  const element = getElement(id);
  if (element) {
    element.innerHTML = html;
  }
}

function showElement(id) {
  const element = getElement(id);
  if (element) {
    element.classList.remove("hidden");
  }
}

function hideElement(id) {
  const element = getElement(id);
  if (element) {
    element.classList.add("hidden");
  }
}

function setVisibility(id, shouldShow) {
  if (shouldShow) {
    showElement(id);
  } else {
    hideElement(id);
  }
}

// ============================================================
// API helpers
// ============================================================
function getReceiverToken() {
  return AppState.receiverToken;
}

function buildAuthHeaders(extraHeaders = {}) {
  return {
    Authorization: `Bearer ${getReceiverToken()}`,
    ...extraHeaders,
  };
}

async function requestJson(path, options = {}) {
  const headers = buildAuthHeaders(options.headers || {});
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${errorText}`);
  }

  return response.json();
}

async function fetchHealthStatus() {
  const response = await fetch("/health");
  if (!response.ok) {
    throw new Error(`Receiver health check failed: ${response.status}`);
  }
  return response.json();
}

// ============================================================
// Authentication
// ============================================================
async function validateKey() {
  const tokenInput = getElement("apiKeyInput");
  const authErrorBox = getElement("authError");
  const enteredToken = tokenInput.value.trim();

  authErrorBox.textContent = "";
  if (!enteredToken) {
    authErrorBox.textContent = "Please enter the bearer token.";
    return;
  }

  AppState.receiverToken = enteredToken;
  try {
    await requestJson("/api/v1/auth/verify");
    await loadOptions({ showErrors: false });
    sessionStorage.setItem("receiverApiKey", AppState.receiverToken);
    hideElement("authView");
    showElement("appView");
  } catch (error) {
    AppState.receiverToken = "";
    authErrorBox.textContent = `Invalid token or connection error: ${error.message}`;
  }
}

function lockPanel() {
  AppState.receiverToken = "";
  sessionStorage.removeItem("receiverApiKey");
  hideElement("appView");
  showElement("authView");

  const tokenInput = getElement("apiKeyInput");
  if (tokenInput) {
    tokenInput.value = "";
  }
}

// ============================================================
// Status and option loading
// ============================================================
async function reloadOptions() {
  // Clear the previous missing-file notice before requesting a fresh audit.
  // If the new audit finds no missing files, nothing will be shown.
  clearStorageNotice();
  await loadOptions({ showErrors: true });
  renderStorageNotice(AppState.uiOptions?.storage_audit);
}

async function loadOptions({ showErrors = true } = {}) {
  try {
    await updateReceiverStatus();
    AppState.uiOptions = await requestJson("/api/v1/ui/options");
    updateAgentStatus();
    renderTableSelection();
    renderQueryTableOptions();
    onModeChange();
  } catch (error) {
    if (showErrors) {
      renderConnectionError(error);
    }
    throw error;
  }
}

async function updateReceiverStatus() {
  const receiverStatus = getElement("receiverStatus");
  if (!receiverStatus) {
    return;
  }

  const health = await fetchHealthStatus();
  const isOnline = health.status === "ok";
  receiverStatus.textContent = isOnline ? "Online" : "Unknown";
  receiverStatus.className = isOnline ? "ok" : "warn";
}

function updateAgentStatus() {
  const agentStatus = getElement("agentStatus");
  if (!agentStatus || !AppState.uiOptions) {
    return;
  }

  agentStatus.textContent = "Online";
  agentStatus.className = "ok";
  setText("sourceTableCount", AppState.uiOptions.tables.length);
  setText("tableCount", AppState.uiOptions.storage_audit?.complete_stored_tables_count ?? 0);
}

function renderConnectionError(error) {
  const agentStatus = getElement("agentStatus");
  if (agentStatus) {
    agentStatus.textContent = "Error";
    agentStatus.className = "bad";
  }

  setHtml(
    "result",
    `<div class="result-card error">
      <h3>Connection error</h3>
      <p>${escapeHtml(error.message)}</p>
      <p>Check that the Factory Agent is running and that the configured URL and token are correct.</p>
    </div>`
  );
}

// ============================================================
// Storage notice
// ============================================================
function renderStorageNotice(audit) {
  const noticeBox = getElement("statusNotice");
  if (!noticeBox) {
    return;
  }

  const repairableRecords = audit?.repairable_missing_records || [];
  const needsRerunRecords = audit?.needs_rerun_missing_records || [];
  const notRepairableRecords = audit?.not_repairable_missing_records || [];
  const totalMissing = Number(audit?.missing_file_records || 0);

  if (!totalMissing) {
    noticeBox.className = "hidden";
    noticeBox.innerHTML = "";
    return;
  }

  noticeBox.className = "status-notice";
  noticeBox.innerHTML = buildStorageNoticeHtml({ repairableRecords, needsRerunRecords, notRepairableRecords, audit });
}

function buildStorageNoticeHtml({ repairableRecords, needsRerunRecords, notRepairableRecords, audit }) {
  const repairableCount = Number(audit?.repairable_missing_records_count ?? repairableRecords.length);
  const needsRerunCount = Number(audit?.needs_rerun_missing_records_count ?? needsRerunRecords.length);
  const notRepairableCount = Number(audit?.not_repairable_missing_records_count ?? notRepairableRecords.length);
  const total = Number(audit?.missing_file_records ?? repairableCount + needsRerunCount + notRepairableCount);

  const repairButton = repairableCount > 0
    ? `<button type="button" onclick="repairMissingFiles()">Try repair from Factory database</button>`
    : "";
  const ignoreButton = notRepairableCount > 0
    ? `<button type="button" class="secondary" onclick="ignoreCannotRepairFiles()">Ignore cannot repair files</button>`
    : "";
  const sourceCheckText = audit?.source_schema_checked
    ? "The Factory database schema was checked for this status."
    : "The Factory database schema was not checked for this status; click Reload status to refresh.";

  return `
    <h3>Missing stored files</h3>
    <p><strong>${escapeHtml(String(total))} stored batch file(s) are missing on the Receiver.</strong></p>
    <p>The Receiver still has metadata for these batches, but the Parquet file or its metadata sidecar is missing from disk.</p>
    <p><strong>Important:</strong> Sync New Data will not send these rows again, because they were already marked as transferred earlier.</p>
    <p class="small">${escapeHtml(sourceCheckText)}</p>
    <div class="notice-actions">
      ${repairButton}
      ${ignoreButton}
      <button type="button" class="secondary" onclick="reloadOptions()">Reload status</button>
    </div>
    ${buildMissingSectionHtml("Repair can be tried", repairableRecords, repairableCount, "The source table currently exists and the metadata is sufficient. Repair will recreate the missing file from the current Factory database without moving the sync cursor. It can still fail if the old source rows were changed or removed.")}
    ${buildMissingSectionHtml("Run Custom Query again", needsRerunRecords, needsRerunCount, "These are old Custom Query results. The original query was not stored in metadata, so the safe action is to run the Custom Query again.")}
    ${buildMissingSectionHtml("Cannot repair automatically", notRepairableRecords, notRepairableCount, "These files cannot be recreated from the current Factory database. You can restore them from backup, take a new full export if the source table becomes available again, or ignore them so this warning is no longer shown.")}
  `;
}

function buildMissingSectionHtml(title, records, total, description) {
  if (!total) {
    return "";
  }
  const maxItemsToShow = 10;
  const itemsHtml = (records || [])
    .slice(0, maxItemsToShow)
    .map(buildMissingRecordItemHtml)
    .join("");
  const remaining = total > maxItemsToShow ? `<p class="small">Showing ${maxItemsToShow} of ${total} item(s).</p>` : "";
  return `
    <div class="missing-section">
      <h4>${escapeHtml(title)} (${escapeHtml(String(total))})</h4>
      <p class="small">${escapeHtml(description)}</p>
      <ul>${itemsHtml}</ul>
      ${remaining}
    </div>
  `;
}

function buildMissingRecordItemHtml(record) {
  const missingParts = [];
  if (!record.parquet_exists) {
    missingParts.push(`<strong>Missing Parquet file:</strong><br>${escapeHtml(record.storage_path || "-")}`);
  }
  if (!record.metadata_exists) {
    missingParts.push(`<strong>Missing sidecar metadata file:</strong><br>${escapeHtml(record.metadata_path || "-")}`);
  }
  const header = `${record.source_table || "unknown table"} · ${friendlyQueryType(record.query_type)} · ${record.batch_id || "unknown batch"}`;
  const reason = record.repair_reason ? `<p><strong>Why this is shown:</strong><br>${escapeHtml(record.repair_reason)}</p>` : "";
  const action = record.repair_action ? `<p><strong>Recommended action:</strong><br>${escapeHtml(friendlyRepairAction(record.repair_action))}</p>` : "";
  return `<li><strong>${escapeHtml(header)}</strong>${reason}${action}<p>${missingParts.join("<br>")}</p></li>`;
}

function friendlyQueryType(value) {
  const labels = {
    incremental: "Sync New Data batch",
    full_table_snapshot: "Full snapshot batch",
    limited_query: "Custom Query result",
  };
  return labels[value] || value || "batch";
}

function friendlyRepairAction(value) {
  const labels = {
    repair_from_source: "Click repair to try recreating this file from the current Factory database.",
    repair_from_stored_query: "Click repair to rerun the stored Custom Query definition from the current Factory database.",
    rerun_custom_query: "Run the Custom Query again, because the original query definition was not saved for this old file.",
    restore_from_backup_or_new_full_export: "Restore this file from backup, or create a new full export if the source table exists again.",
    manual_review: "Check this file manually. Automatic repair is not available for this record.",
  };
  return labels[value] || value || "Manual review required.";
}

function clearStorageNotice() {
  const noticeBox = getElement("statusNotice");
  if (!noticeBox) {
    return;
  }
  noticeBox.className = "hidden";
  noticeBox.innerHTML = "";
}

// ============================================================
// Table selection
// ============================================================
function renderTableSelection() {
  const tablesBox = getElement("tables");
  if (!tablesBox || !AppState.uiOptions) {
    return;
  }

  tablesBox.innerHTML = "";
  for (const table of AppState.uiOptions.tables) {
    tablesBox.appendChild(createTableSelectionItem(table));
  }
}

function createTableSelectionItem(table) {
  const item = document.createElement("label");
  item.className = "table-item";
  item.innerHTML = `
    <input type="checkbox" class="tableCheck" value="${escapeAttribute(table.name)}">
    <div>
      <strong>${escapeHtml(table.name)}</strong>
      <div class="table-meta">Columns: ${table.columns.map(escapeHtml).join(", ") || "-"}</div>
    </div>
  `;
  return item;
}

function getSelectedTables() {
  return Array.from(document.querySelectorAll(".tableCheck:checked")).map((checkbox) => checkbox.value);
}

// ============================================================
// Custom Query, table and column selection
// ============================================================
function renderQueryTableOptions() {
  const tableSelect = getElement("queryTable");
  if (!tableSelect || !AppState.uiOptions) {
    return;
  }

  tableSelect.innerHTML = AppState.uiOptions.tables
    .map((table) => `<option value="${escapeAttribute(table.name)}">${escapeHtml(table.name)}</option>`)
    .join("");

  renderQueryColumns();
}

function getCurrentQueryTable() {
  const selectedTableName = getElement("queryTable")?.value;
  return AppState.uiOptions?.tables?.find((table) => table.name === selectedTableName) || null;
}

function renderQueryColumns() {
  const table = getCurrentQueryTable();
  const columnBox = getElement("queryColumns");
  if (!table || !columnBox) {
    return;
  }

  columnBox.innerHTML = table.columns
    .map((column) => {
      return `
        <label class="column-item">
          <input type="checkbox" class="queryColumn" value="${escapeAttribute(column)}" checked>
          <span>${escapeHtml(column)}</span>
        </label>
      `;
    })
    .join("");

  resetFilterRows();
}

function getSelectedQueryColumns() {
  return Array.from(document.querySelectorAll(".queryColumn:checked")).map((checkbox) => checkbox.value);
}

function setAllQueryColumns(checked) {
  document.querySelectorAll(".queryColumn").forEach((checkbox) => {
    checkbox.checked = checked;
  });
}

// ============================================================
// Filter metadata helpers
// ============================================================
function getColumnDetails(columnName) {
  const table = getCurrentQueryTable();
  return table?.column_details?.find((column) => column.name === columnName) || null;
}

function isTimeLikeColumn(columnName) {
  const details = getColumnDetails(columnName);
  const name = String(columnName || "").toLowerCase();
  const type = String(details?.type || "").toLowerCase();

  return (
    name.includes("time") ||
    name.includes("date") ||
    name.includes("timestamp") ||
    name.includes("created") ||
    name.includes("updated") ||
    type.includes("date") ||
    type.includes("time")
  );
}

function buildFilterColumnOptions() {
  const table = getCurrentQueryTable();
  const columns = table?.columns || [];
  const columnOptions = columns
    .map((column) => {
      const timeHint = isTimeLikeColumn(column) ? " · time" : "";
      return `<option value="${escapeAttribute(column)}">${escapeHtml(column)}${timeHint}</option>`;
    })
    .join("");

  return `<option value="">Choose column</option>${columnOptions}`;
}

// ============================================================
// Filter row rendering
// ============================================================
function resetFilterRows() {
  const filtersBox = getElement("filtersBox");
  if (!filtersBox) {
    return;
  }

  filtersBox.innerHTML = "";
  addFilterRow();
}

function addFilterRow() {
  const filtersBox = getElement("filtersBox");
  if (!filtersBox) {
    return;
  }

  const filterId = createNextFilterId();
  const filterRow = createFilterRow(filterId);
  filtersBox.appendChild(filterRow);
  updateFilterRow(filterId);
}

function createNextFilterId() {
  AppState.nextFilterId += 1;
  return AppState.nextFilterId;
}

function createFilterRow(filterId) {
  const row = document.createElement("div");
  row.className = "filter-row";
  row.dataset.filterId = String(filterId);
  row.innerHTML = `
    <div class="filter-row-grid">
      <label>Column
        <select class="filterColumn" onchange="updateFilterRow(${filterId})">${buildFilterColumnOptions()}</select>
      </label>
      <div class="filterSummary small">Filter type</div>
    </div>
    <div class="filterControls"></div>
    <div class="filter-actions">
      <button type="button" class="secondary small-btn" onclick="removeFilterRow(${filterId})">Remove filter</button>
    </div>
  `;
  return row;
}

function updateFilterRow(filterId) {
  const row = findFilterRow(filterId);
  if (!row) {
    return;
  }

  const selectedColumn = getFilterRowColumn(row);
  const summaryBox = row.querySelector(".filterSummary");
  const controlsBox = row.querySelector(".filterControls");

  if (!selectedColumn) {
    renderEmptyFilterControls(summaryBox, controlsBox);
    return;
  }

  if (isTimeLikeColumn(selectedColumn)) {
    renderTimeFilterControls(summaryBox, controlsBox);
  } else {
    renderValueFilterControls(summaryBox, controlsBox);
  }
}

function findFilterRow(filterId) {
  return document.querySelector(`[data-filter-id="${filterId}"]`);
}

function getFilterRowColumn(row) {
  return row.querySelector(".filterColumn")?.value || "";
}

function renderEmptyFilterControls(summaryBox, controlsBox) {
  summaryBox.innerHTML = "Filter type";
  controlsBox.innerHTML = "";
}

function renderTimeFilterControls(summaryBox, controlsBox) {
  summaryBox.innerHTML = "Filter type: time range";
  controlsBox.innerHTML = `
    <div class="filter-row-grid">
      <label>Start time <input type="datetime-local" class="filterStartTime"></label>
      <label>End time <input type="datetime-local" class="filterEndTime"></label>
    </div>
    <p class="small">At least one start or end time is required for this time filter.</p>
  `;
}

function renderValueFilterControls(summaryBox, controlsBox) {
  summaryBox.innerHTML = "Filter type: value";
  controlsBox.innerHTML = `
    <div class="filter-row-grid">
      <label>Operator
        <select class="filterOperator">
          <option value="eq">equals</option>
          <option value="ne">not equals</option>
          <option value="gt">greater than</option>
          <option value="gte">greater than or equal</option>
          <option value="lt">less than</option>
          <option value="lte">less than or equal</option>
          <option value="contains">contains text</option>
        </select>
      </label>
      <label>Value <input type="text" class="filterValue"></label>
    </div>
  `;
}

function removeFilterRow(filterId) {
  const row = findFilterRow(filterId);
  if (row) {
    row.remove();
  }
}

// ============================================================
// Filter collection
// ============================================================
function collectFilters() {
  const filters = [];
  document.querySelectorAll(".filter-row").forEach((row) => {
    const column = getFilterRowColumn(row);
    if (!column) {
      return;
    }

    if (isTimeLikeColumn(column)) {
      filters.push(collectTimeFilter(row, column));
    } else {
      filters.push(collectValueFilter(row, column));
    }
  });
  return filters;
}

function collectTimeFilter(row, column) {
  const startTime = row.querySelector(".filterStartTime")?.value || "";
  const endTime = row.querySelector(".filterEndTime")?.value || "";

  if (!startTime && !endTime) {
    throw new Error(`Enter a start or end time for time column ${column}, or remove the filter.`);
  }

  const filter = {
    column,
    filter_type: "time",
  };

  if (startTime) {
    filter.start_time = normalizeDateTimeForSqlite(startTime);
  }
  if (endTime) {
    filter.end_time = normalizeDateTimeForSqlite(endTime);
  }
  return filter;
}

function collectValueFilter(row, column) {
  const operator = row.querySelector(".filterOperator")?.value || "eq";
  const value = row.querySelector(".filterValue")?.value || "";

  if (value === "") {
    throw new Error(`Filter value for column ${column} is empty.`);
  }

  return { column, operator, value };
}

// ============================================================
// Operation mode handling
// ============================================================
function onModeChange() {
  const mode = getElement("mode").value;
  updateModeDescription(mode);
  updateVisiblePanels(mode);
  updatePolicyBox(mode);
}

function updateModeDescription(mode) {
  // The short description under the operation dropdown is intentionally hidden.
  // The detailed behavior is shown only in the Execution policy box.
  const descriptionElement = getElement("modeDescription");
  if (descriptionElement) {
    descriptionElement.textContent = "";
    descriptionElement.classList.add("hidden");
  }
}

function updateVisiblePanels(mode) {
  setVisibility("selectedTablesPanel", mode.includes("selected_tables"));
  setVisibility("limitedQueryPanel", mode === "limited_query");
  setVisibility("limitPanel", mode !== "limited_query" && mode !== "schema_only");
}

function updatePolicyBox(mode) {
  const policyText = getPolicyText(mode);
  setHtml("policyBox", `<p>${escapeHtml(policyText)}</p>`);
}

function getPolicyText(mode) {
  const policies = {
    new_data: "Normal transfer. Sends only new rows since the last successful sync. The row limit controls how many new rows per table are sent in this request. Large results are split into numbered part files.",
    full_database: "Complete database request. Requests all tables and all rows from the current database state. If an unchanged full snapshot already exists, it may be skipped. Large tables are split into numbered part files.",
    selected_tables_new_data: "Selected new-data transfer. Sends only new rows from the tables you select. Other tables are not checked or transferred.",
    selected_tables_full_snapshot: "Selected full refresh. Requests the complete current content of the selected tables. Existing rows may be sent again. Large tables are split into numbered part files.",
    limited_query: "Custom query. Exports selected columns from one table using the filters you define in the UI. Free-form SQL is not allowed.",
    schema_only: "Schema inspection. Reads only table names, columns, and schema information. No row data is transferred.",
  };
  return policies[mode] || "";
}

// ============================================================
// Running progress indicator
// ============================================================
function startRunningProgress() {
  ensureProgressStyles();
  stopRunningProgress();

  AppState.progressStartedAt = Date.now();
  AppState.progressStageIndex = 0;
  renderRunningProgress();

  AppState.progressTimerId = window.setInterval(() => {
    AppState.progressStageIndex += 1;
    updateRunningProgressText();
  }, 1000);
}

function stopRunningProgress() {
  if (AppState.progressTimerId) {
    window.clearInterval(AppState.progressTimerId);
    AppState.progressTimerId = null;
  }
}

function renderRunningProgress() {
  setHtml(
    "result",
    `<div class="result-card progress-card">
      <div class="progress-header">
        <div>
          <h3>Operation is running...</h3>
          <p id="progressStatusText">${escapeHtml(getProgressStageText())}</p>
        </div>
        <div class="progress-time" id="progressElapsedText">00:00</div>
      </div>
      <div class="progress-track" aria-label="Operation progress">
        <div class="progress-bar"></div>
      </div>
      <p class="small progress-note">The request is active. The final result will appear when the Receiver gets the response from the Factory Agent.</p>
    </div>`
  );
}

function updateRunningProgressText() {
  const statusText = getElement("progressStatusText");
  const elapsedText = getElement("progressElapsedText");

  if (statusText) {
    statusText.textContent = getProgressStageText();
  }
  if (elapsedText) {
    elapsedText.textContent = formatElapsedTime(Date.now() - AppState.progressStartedAt);
  }
}

function getProgressStageText() {
  const stages = [
    "Preparing request...",
    "Contacting Factory Agent...",
    "Reading source database...",
    "Creating Parquet part files...",
    "Uploading and verifying checksums...",
    "Saving metadata on Receiver...",
    "Waiting for final response...",
  ];
  const index = Math.floor(AppState.progressStageIndex / 4) % stages.length;
  return stages[index];
}

function formatElapsedTime(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function ensureProgressStyles() {
  if (document.getElementById("progressStyles")) {
    return;
  }

  const style = document.createElement("style");
  style.id = "progressStyles";
  style.textContent = `
    .progress-card {
      overflow: hidden;
    }

    .progress-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 1rem;
    }

    .progress-header h3 {
      margin-bottom: 0.35rem;
    }

    .progress-time {
      min-width: 4.5rem;
      padding: 0.4rem 0.65rem;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.35);
      text-align: center;
      font-variant-numeric: tabular-nums;
      color: #e5e7eb;
      background: rgba(15, 23, 42, 0.45);
    }

    .progress-track {
      position: relative;
      height: 0.75rem;
      width: 100%;
      overflow: hidden;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.35);
      background: rgba(15, 23, 42, 0.75);
    }

    .progress-bar {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 42%;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(34, 197, 94, 0.25), rgba(34, 197, 94, 0.95), rgba(34, 197, 94, 0.25));
      animation: progressSlide 1.4s ease-in-out infinite;
    }

    .progress-note {
      margin-top: 0.85rem;
    }

    .notice-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin: 1rem 0;
    }

    .notice-actions .secondary {
      background: rgba(148, 163, 184, 0.18);
      color: #e5e7eb;
      border: 1px solid rgba(148, 163, 184, 0.35);
    }

    @keyframes progressSlide {
      0% { left: -45%; }
      50% { left: 35%; }
      100% { left: 105%; }
    }
  `;
  document.head.appendChild(style);
}

async function repairMissingFiles() {
  startRunningProgress();

  try {
    const responseData = await requestJson("/api/v1/storage/repair-missing-files", {
      method: "POST",
    });
    stopRunningProgress();
    AppState.lastRawResponse = responseData;
    setText("raw", JSON.stringify(responseData, null, 2));
    renderResult(responseData.explanation || {
      headline: responseData.message || "Repair request completed.",
      summary: {
        transferred_rows: 0,
        part_files_received: responseData.repaired_files || 0,
        transfer_groups: 0,
      },
      table_explanations: [],
      transfer_groups: [],
      schema_events: [],
    });
    await loadOptions({ showErrors: false });
    renderStorageNotice(AppState.uiOptions?.storage_audit);
  } catch (error) {
    stopRunningProgress();
    renderOperationError(error);
  }
}


async function ignoreCannotRepairFiles() {
  const ok = window.confirm(
    "Ignore all files currently listed under 'Cannot repair automatically'?\n\n" +
    "This will only hide these missing-file warnings. It will not restore files, delete metadata, or change the Factory Agent sync state."
  );
  if (!ok) {
    return;
  }

  startRunningProgress();

  try {
    const responseData = await requestJson("/api/v1/storage/ignore-not-repairable-missing-files", {
      method: "POST",
    });
    stopRunningProgress();
    AppState.lastRawResponse = responseData;
    setText("raw", JSON.stringify(responseData, null, 2));
    renderResult(responseData.explanation || {
      headline: responseData.message || "Missing-file warnings ignored.",
      summary: {
        ignored_files: responseData.ignored_files || 0,
      },
      table_explanations: [],
      transfer_groups: [],
      schema_events: [],
    });
    await loadOptions({ showErrors: false });
    renderStorageNotice(AppState.uiOptions?.storage_audit);
  } catch (error) {
    stopRunningProgress();
    renderOperationError(error);
  }
}

// ============================================================
// Request building and sending
// ============================================================
async function sendRequest() {
  startRunningProgress();

  try {
    const payload = buildRequestPayload();
    const responseData = await submitOperation(payload);
    stopRunningProgress();
    AppState.lastRawResponse = responseData;
    setText("raw", JSON.stringify(responseData, null, 2));
    renderResult(responseData.explanation);
    await loadOptions({ showErrors: false });
    renderStorageNotice(AppState.uiOptions?.storage_audit);
  } catch (error) {
    stopRunningProgress();
    renderOperationError(error);
  }
}

async function submitOperation(payload) {
  return requestJson("/api/v1/ui/sync", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

function buildRequestPayload() {
  const mode = getElement("mode").value;
  const limitValue = getElement("limit")?.value;
  const payload = { mode };

  if (shouldIncludeTableLimit(mode, limitValue)) {
    payload.max_records_per_table = Number(limitValue);
  }

  if (mode.includes("selected_tables")) {
    payload.tables = getSelectedTables();
  }

  if (mode === "limited_query") {
    addCustomQueryPayload(payload);
  }

  return payload;
}

function shouldIncludeTableLimit(mode, limitValue) {
  return Boolean(limitValue && mode !== "limited_query");
}

function addCustomQueryPayload(payload) {
  payload.table = getElement("queryTable").value;
  payload.columns = getSelectedQueryColumns();

  if (!payload.columns.length) {
    throw new Error("Select at least one column for the custom query.");
  }

  const filters = collectFilters();
  if (filters.length) {
    payload.filters = filters;
  }

  const queryLimit = getElement("queryLimit")?.value;
  if (queryLimit) {
    payload.max_records = Number(queryLimit);
  }
}

function renderOperationError(error) {
  setHtml(
    "result",
    `<div class="result-card error">
      <h3>Operation failed</h3>
      <p>${escapeHtml(error.message)}</p>
    </div>`
  );
}

// ============================================================
// Result rendering
// ============================================================
function renderResult(explanation) {
  if (explanation.schema_only) {
    renderSchemaOnlyResult(explanation);
    return;
  }

  const summary = explanation.summary || {};
  const html = [
    buildSummaryCard(explanation),
    buildStatsCards(summary),
    buildTransferGroupsHtml(explanation.transfer_groups || []),
    buildSchemaEventsHtml(explanation.schema_events || []),
    buildTableResultsHtml(explanation.table_explanations || []),
  ].join("");

  setHtml("result", html);
}

function renderSchemaOnlyResult(explanation) {
  const tableCards = (explanation.table_explanations || []).map(buildSchemaOnlyTableCard).join("");
  setHtml("result", tableCards || '<div class="result-card"><p>No tables found.</p></div>');
}

function buildSchemaOnlyTableCard(table) {
  const columns = (table.columns || []).join(", ") || "none";
  return `
    <div class="result-card">
      <h3>${escapeHtml(table.table_name || "Table")}</h3>
      <p>Columns found: ${escapeHtml(columns)}</p>
    </div>
  `;
}

function buildSummaryCard(explanation) {
  return `
    <div class="result-card">
      <h3>${escapeHtml(explanation.headline || "Completed")}</h3>
      ${buildStorageRootHtml(explanation.receiver_storage_root)}
      ${explanation.storage_policy ? `<p class="small">${escapeHtml(explanation.storage_policy)}</p>` : ""}
    </div>
  `;
}

function buildStorageRootHtml(storageRoot) {
  if (!storageRoot) {
    return "";
  }
  return `<p><strong>Storage root:</strong><br>${escapeHtml(storageRoot)}</p>`;
}

function buildStatsCards(summary) {
  return `
    <div class="stats">
      <div class="stat"><strong>${summary.transferred_rows ?? 0}</strong><span>total rows received</span></div>
      <div class="stat"><strong>${summary.part_files_received ?? summary.created_batches ?? 0}</strong><span>part files received</span></div>
      <div class="stat"><strong>${summary.transfer_groups ?? 0}</strong><span>transfer request group(s)</span></div>
    </div>
  `;
}

function buildTransferGroupsHtml(groups) {
  if (!groups.length) {
    return "";
  }

  const groupsHtml = groups.map(buildTransferGroupCard).join("");
  return `
    <h3>Transfer parts</h3>
    ${groupsHtml}
  `;
}

function buildTransferGroupCard(group) {
  const parts = group.parts || [];
  const partRows = parts.map(buildPartRowHtml).join("");
  const queryLabel = group.query_type === "full_table_snapshot" ? "full snapshot" : group.query_type || "transfer";

  return `
    <div class="result-card created">
      <h3>${escapeHtml(group.table_name || "Table")}: ${escapeHtml(queryLabel)}</h3>
      <p><strong>Request ID:</strong><br>${escapeHtml(group.transfer_request_id || "-")}</p>
      <p>
        <span class="badge">total rows: ${escapeHtml(String(group.total_rows_received ?? 0))}</span>
        <span class="badge">parts: ${escapeHtml(String(group.received_parts ?? parts.length))}/${escapeHtml(String(group.total_parts ?? parts.length))}</span>
        <span class="badge">schema v${escapeHtml(String(group.schema_version ?? "-"))}</span>
      </p>
      <div>${partRows}</div>
    </div>
  `;
}

function buildPartRowHtml(part) {
  const partNumber = part.part_number ?? "-";
  const totalParts = part.total_parts ?? "-";
  return `
    <div class="result-card">
      <h3>Part ${escapeHtml(String(partNumber))}/${escapeHtml(String(totalParts))}</h3>
      <p><strong>Rows:</strong> ${escapeHtml(String(part.row_count ?? 0))}</p>
      ${buildOptionalParagraph("File name", part.file_name)}
      ${buildOptionalParagraph("Storage path", part.storage_path)}
      ${buildOptionalParagraph("Batch ID", part.batch_id)}
      ${buildOptionalParagraph("Checksum", part.checksum_sha256)}
    </div>
  `;
}

function buildSchemaEventsHtml(schemaEvents) {
  if (!schemaEvents.length) {
    return "";
  }

  const eventsHtml = schemaEvents
    .map((event) => {
      return `
        <div class="result-card">
          <p>Event: ${escapeHtml(event.event)}, table: ${escapeHtml(event.table_name || "-")}, schema version: ${escapeHtml(String(event.schema_version || "-"))}</p>
        </div>
      `;
    })
    .join("");

  return `<h3>Schema changes</h3>${eventsHtml}`;
}

function buildTableResultsHtml(tableResults) {
  return tableResults.map(buildTableResultCard).join("");
}

function buildTableResultCard(table) {
  return `
    <div class="result-card ${escapeAttribute(table.status || "")}">
      <h3>${escapeHtml(table.title || table.table_name || "Table")}</h3>
      <p>${escapeHtml(table.explanation || "")}</p>
      <p>
        <span class="badge">schema v${escapeHtml(String(table.schema_version || "-"))}</span>
        <span class="badge">strategy: ${escapeHtml(table.export_strategy || "-")}</span>
        <span class="badge">rows: ${escapeHtml(String(table.row_count || 0))}</span>
        ${table.part_number ? `<span class="badge">part: ${escapeHtml(String(table.part_number))}/${escapeHtml(String(table.total_parts || "-"))}</span>` : ""}
      </p>
      ${buildOptionalParagraph("Request ID", table.transfer_request_id)}
      ${buildOptionalParagraph("File name", table.file_name)}
      ${buildOptionalParagraph("Storage path", table.storage_path)}
      ${buildOptionalParagraph("Checksum", table.checksum_sha256)}
      ${buildOptionalParagraph("Previous batch", table.previous_batch_id)}
    </div>
  `;
}

function buildOptionalParagraph(label, value) {
  if (!value) {
    return "";
  }
  return `<p><strong>${escapeHtml(label)}:</strong><br>${escapeHtml(value)}</p>`;
}

// ============================================================
// General utilities
// ============================================================
function normalizeDateTimeForSqlite(value) {
  if (!value) {
    return "";
  }

  let normalized = value.trim();
  if (normalized.includes("T")) {
    normalized = normalized.replace("T", " ");
  }
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(normalized)) {
    normalized += ":00";
  }
  return normalized;
}

function toggleRaw() {
  getElement("raw").classList.toggle("hidden");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

// ============================================================
// Initial page load
// ============================================================
window.addEventListener("load", async () => {
  const savedToken = sessionStorage.getItem("receiverApiKey");
  if (!savedToken) {
    return;
  }

  getElement("apiKeyInput").value = savedToken;
  await validateKey();
});
