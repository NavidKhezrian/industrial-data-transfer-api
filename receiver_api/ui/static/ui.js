// ============================================================
// Data Transfer Console UI
// ------------------------------------------------------------
// This file controls the browser UI for the Receiver application.
// It handles:
// - Token validation
// - Loading available tables and columns
// - Building sync and custom query requests
// - Rendering operation results
// - Showing storage notices
// ============================================================


// ============================================================
// Application state
// ============================================================

const AppState = {
  uiOptions: null,
  lastRawResponse: null,
  receiverToken: "",
  nextFilterId: 0,
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

  setText(
    "tableCount",
    AppState.uiOptions.storage_audit?.complete_stored_tables_count ?? 0
  );
}

function renderConnectionError(error) {
  const agentStatus = getElement("agentStatus");

  if (agentStatus) {
    agentStatus.textContent = "Error";
    agentStatus.className = "bad";
  }

  setHtml(
    "result",
    `
      <div class="result-card error">
        <h3>Connection error</h3>
        <p>${escapeHtml(error.message)}</p>
        <p class="small">
          Check that the Factory Agent is running and that the configured URL and token are correct.
        </p>
      </div>
    `
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

  const missingSnapshots = getMissingLatestSnapshots(audit);

  if (!missingSnapshots.length) {
    noticeBox.className = "hidden";
    noticeBox.innerHTML = "";
    return;
  }

  noticeBox.className = "status-notice";
  noticeBox.innerHTML = buildStorageNoticeHtml(missingSnapshots);
}

function getMissingLatestSnapshots(audit) {
  return audit?.missing_latest_full_snapshots || [];
}

function buildStorageNoticeHtml(missingSnapshots) {
  const maxItemsToShow = 12;

  const itemsHtml = missingSnapshots
    .slice(0, maxItemsToShow)
    .map(buildMissingSnapshotItemHtml)
    .join("");

  return `
    <h3>Storage notice</h3>

    <p class="small">
      The latest stored full snapshot for ${missingSnapshots.length} table(s) has missing files.
      Recreate the affected full snapshot to clear this notice.
    </p>

    <ul>${itemsHtml}</ul>
  `;
}

function buildMissingSnapshotItemHtml(snapshot) {
  const missingParts = [];

  if (!snapshot.parquet_exists) {
    missingParts.push(`
      <b>Missing parquet:</b>
      <span dir="ltr">${escapeHtml(snapshot.storage_path || "-")}</span>
    `);
  }

  if (!snapshot.metadata_exists) {
    missingParts.push(`
      <b>Missing metadata:</b>
      <span dir="ltr">${escapeHtml(snapshot.metadata_path || "-")}</span>
    `);
  }

  return `<li>${missingParts.join("<br>")}</li>`;
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
  const item = document.createElement("div");

  item.className = "table-item";
  item.innerHTML = `
    <input
      type="checkbox"
      class="tableCheck"
      value="${escapeHtml(table.name)}"
    />

    <div>
      <b>${escapeHtml(table.name)}</b>

      <div class="table-meta">
        Columns: ${table.columns.map(escapeHtml).join(", ") || "-"}
      </div>
    </div>
  `;

  return item;
}

function getSelectedTables() {
  return Array.from(document.querySelectorAll(".tableCheck:checked")).map(
    (checkbox) => checkbox.value
  );
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
    .map((table) => {
      return `
        <option value="${escapeHtml(table.name)}">
          ${escapeHtml(table.name)}
        </option>
      `;
    })
    .join("");

  renderQueryColumns();
}

function getCurrentQueryTable() {
  const selectedTableName = getElement("queryTable")?.value;

  return (
    AppState.uiOptions?.tables?.find(
      (table) => table.name === selectedTableName
    ) || null
  );
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
          <input
            type="checkbox"
            class="queryColumn"
            value="${escapeHtml(column)}"
            checked
          />
          ${escapeHtml(column)}
        </label>
      `;
    })
    .join("");

  resetFilterRows();
}

function getSelectedQueryColumns() {
  return Array.from(document.querySelectorAll(".queryColumn:checked")).map(
    (checkbox) => checkbox.value
  );
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

  return (
    table?.column_details?.find((column) => column.name === columnName) || null
  );
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

      return `
        <option value="${escapeHtml(column)}">
          ${escapeHtml(column)}${timeHint}
        </option>
      `;
    })
    .join("");

  return `
    <option value="">Choose column</option>
    ${columnOptions}
  `;
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
      <div>
        <label>Column</label>

        <select class="filterColumn" onchange="updateFilterRow(${filterId})">
          ${buildFilterColumnOptions()}
        </select>
      </div>

      <div class="filterSummary">
        <label>Filter type</label>
        <input value="Select a column" disabled />
      </div>
    </div>

    <div class="filterControls"></div>

    <div class="filter-actions">
      <button
        type="button"
        class="secondary small-btn"
        onclick="removeFilterRow(${filterId})"
      >
        Remove filter
      </button>
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
  summaryBox.innerHTML = `
    <label>Filter type</label>
    <input value="Select a column" disabled />
  `;

  controlsBox.innerHTML = "";
}

function renderTimeFilterControls(summaryBox, controlsBox) {
  summaryBox.innerHTML = `
    <label>Filter type</label>
    <input value="Time range" disabled />
  `;

  controlsBox.innerHTML = `
    <div class="filter-row-grid">
      <div>
        <label>Start time</label>
        <input class="filterStartTime" type="datetime-local" step="1" />
      </div>

      <div>
        <label>End time</label>
        <input class="filterEndTime" type="datetime-local" step="1" />
      </div>
    </div>

    <p class="small">
      At least one start or end time is required for this time filter.
    </p>
  `;
}

function renderValueFilterControls(summaryBox, controlsBox) {
  summaryBox.innerHTML = `
    <label>Filter type</label>
    <input value="Value filter" disabled />
  `;

  controlsBox.innerHTML = `
    <label>Operator</label>

    <select class="filterOperator">
      <option value="eq">equals</option>
      <option value="ne">not equals</option>
      <option value="gt">greater than</option>
      <option value="gte">greater than or equal</option>
      <option value="lt">less than</option>
      <option value="lte">less than or equal</option>
      <option value="contains">contains text</option>
    </select>

    <label>Value</label>
    <input class="filterValue" placeholder="Filter value" />
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
    throw new Error(
      `Enter a start or end time for time column ${column}, or remove the filter.`
    );
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

  return {
    column,
    operator,
    value,
  };
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
  const selectedMode = AppState.uiOptions?.request_modes?.find(
    (requestMode) => requestMode.id === mode
  );

  setText("modeDescription", selectedMode?.description || "");
}

function updateVisiblePanels(mode) {
  setVisibility("selectedTablesPanel", mode.includes("selected_tables"));
  setVisibility("limitedQueryPanel", mode === "limited_query");

  const shouldShowLimitPanel =
    mode !== "limited_query" && mode !== "schema_only";

  setVisibility("limitPanel", shouldShowLimitPanel);
}

function updatePolicyBox(mode) {
  const policyText = getPolicyText(mode);

  setHtml("policyBox", `<p class="small">${policyText}</p>`);
}

function getPolicyText(mode) {
  const policies = {
    new_data:
      "<b>Default sync.</b> Transfers new data where a reliable sync key exists. First-time tables are copied once.",

    full_database:
      "<b>Full Database.</b> Copies the current state of every table. Unchanged tables are skipped.",

    selected_tables_new_data:
      "<b>Selected sync.</b> Checks only selected tables and transfers new rows where possible.",

    selected_tables_full_snapshot:
      "<b>Selected full refresh.</b> Copies the current state of selected tables. Use it for recovery, validation, or schema migrations.",

    limited_query:
      "<b>Custom Query.</b> Builds a validated table query from selected columns and filters. Free-form SQL is not executed.",

    schema_only: "Reads table and column structure only.",
  };

  return policies[mode] || "";
}


// ============================================================
// Request building and sending
// ============================================================

async function sendRequest() {
  setHtml("result", '<p class="small">Operation is running...</p>');

  try {
    const payload = buildRequestPayload();
    const responseData = await submitOperation(payload);

    AppState.lastRawResponse = responseData;

    setText("raw", JSON.stringify(responseData, null, 2));
    renderResult(responseData.explanation);

    await loadOptions({ showErrors: false });
    renderStorageNotice(AppState.uiOptions?.storage_audit);
  } catch (error) {
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
  const limitValue = getElement("limit").value;

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

  const queryLimit = getElement("queryLimit").value;

  if (queryLimit) {
    payload.max_records = Number(queryLimit);
  }
}

function renderOperationError(error) {
  setHtml(
    "result",
    `
      <div class="result-card error">
        <h3>Operation failed</h3>
        <p>${escapeHtml(error.message)}</p>
      </div>
    `
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
    buildSchemaEventsHtml(explanation.schema_events || []),
    buildTableResultsHtml(explanation.table_explanations || []),
  ].join("");

  setHtml("result", html);
}

function renderSchemaOnlyResult(explanation) {
  const tableCards = (explanation.table_explanations || [])
    .map(buildSchemaOnlyTableCard)
    .join("");

  setHtml("result", tableCards || '<p class="small">No tables found.</p>');
}

function buildSchemaOnlyTableCard(table) {
  const columns = (table.columns || []).join(", ") || "none";

  return `
    <div class="result-card">
      <h3>${escapeHtml(table.table_name || "Table")}</h3>
      <p><b>Columns found:</b> ${escapeHtml(columns)}</p>
    </div>
  `;
}

function buildSummaryCard(explanation) {
  return `
    <div class="result-card created">
      <h3>${escapeHtml(explanation.headline)}</h3>
      ${buildStorageRootHtml(explanation.receiver_storage_root)}
    </div>
  `;
}

function buildStorageRootHtml(storageRoot) {
  if (!storageRoot) {
    return "";
  }

  return `
    <p>
      <b>Storage root:</b>
      <span dir="ltr">${escapeHtml(storageRoot)}</span>
    </p>
  `;
}

function buildStatsCards(summary) {
  return `
    <div class="stats">
      <div class="stat">
        <strong>${summary.created_batches ?? 0}</strong>
        <span>stored batches</span>
      </div>

      <div class="stat">
        <strong>${summary.transferred_rows ?? 0}</strong>
        <span>transferred rows</span>
      </div>

      <div class="stat">
        <strong>${summary.duplicate_full_snapshots_skipped ?? 0}</strong>
        <span>skipped tables</span>
      </div>
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
        <p>
          Event: ${escapeHtml(event.event)},
          table: ${escapeHtml(event.table_name || "-")},
          schema version: ${escapeHtml(String(event.schema_version || "-"))}
        </p>
      `;
    })
    .join("");

  return `
    <div class="result-card">
      <h3>Schema changes</h3>
      ${eventsHtml}
    </div>
  `;
}

function buildTableResultsHtml(tableResults) {
  return tableResults.map(buildTableResultCard).join("");
}

function buildTableResultCard(table) {
  return `
    <div class="result-card ${escapeHtml(table.status || "")}">
      <h3>${escapeHtml(table.title || table.table_name || "Table")}</h3>

      <p>${escapeHtml(table.explanation || "")}</p>

      <div>
        <span class="badge">
          schema v${escapeHtml(String(table.schema_version || "-"))}
        </span>

        <span class="badge">
          strategy: ${escapeHtml(table.export_strategy || "-")}
        </span>

        <span class="badge">
          rows: ${escapeHtml(String(table.row_count || 0))}
        </span>
      </div>

      ${buildOptionalParagraph("Previous batch", table.previous_batch_id)}
      ${buildOptionalParagraph("Storage path", table.storage_path)}
      ${buildOptionalParagraph("Checksum", table.checksum_sha256)}
    </div>
  `;
}

function buildOptionalParagraph(label, value) {
  if (!value) {
    return "";
  }

  return `
    <p>
      <b>${escapeHtml(label)}:</b>
      <span dir="ltr">${escapeHtml(value)}</span>
    </p>
  `;
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