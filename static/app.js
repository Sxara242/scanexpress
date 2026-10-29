const scanButton = document.getElementById("scanButton");
const statusText = document.getElementById("statusText");
const statusStats = document.getElementById("statusStats");
const flashTarget = document.body;
const deviceConfigFieldset = document.getElementById("deviceConfigFieldset");
const deviceRadioGroups = document.getElementById("deviceRadioGroups");
const deviceDetailsPanel = document.getElementById("deviceDetailsPanel");
const deviceDetailsSummary = document.getElementById("deviceDetailsSummary");
const deviceDetails = document.getElementById("deviceDetails");
const recentUploadsBody = document.getElementById("recentUploadsBody");
const filenameInput = document.getElementById("filenameInput");
const headerUsername = document.getElementById("headerUsername");
const accountMenuButton = document.getElementById("accountMenuButton");
const accountMenuList = document.getElementById("accountMenuList");
const logoutButton = document.getElementById("logoutButton");
const queryParams = new URLSearchParams(window.location.search);
const isDemoMode = queryParams.get("demo") === "1";
let deviceMap = new Map();
let selectedDeviceName = null;
let paperlessBaseUrl = "";
let currentStatus = "idle";
let currentMessage = "idle";
let currentStatsLines = [];
let selectedDeviceBusy = false;
let refreshingScanStatus = false;
let refreshScanStatusPending = false;
let refreshScanStatusRequestCounter = 0;
let activeScanDeviceName = null;
let localScanPhaseActive = false;
let lastScanButtonDisabled = null;
let lastScanButtonBusyOrScanning = false;
let completionFlashDeviceName = null;
const PAPERLESS_POLL_INTERVAL_MS = 2000;
const PAPERLESS_MAX_RETRY_INTERVAL_MS = 15000;
const MAX_RECENT_UPLOADS = 30;
const PAPERLESS_TERMINAL_STATUSES = new Set(["SUCCESS", "FAILURE"]);
const recentPaperlessTasks = [];
const paperlessPollTimers = new Map();
const BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
const timeoutState = {
  phase: "idle",
  countdownStartSeconds: 15,
  scan: {
    timeoutSeconds: null,
    startedAtMs: null,
    label: "scanimage",
  },
  upload: {
    timeoutSeconds: null,
    startedAtMs: null,
    label: "paperless",
  },
};

function clearTimeoutCountdownState() {
  timeoutState.phase = "idle";
  timeoutState.countdownStartSeconds = 15;
  timeoutState.scan.timeoutSeconds = null;
  timeoutState.scan.startedAtMs = null;
  timeoutState.upload.timeoutSeconds = null;
  timeoutState.upload.startedAtMs = null;
}

function encodeBase62(value) {
  if (!Number.isFinite(value) || value < 0) {
    return "0";
  }

  let remainder = Math.floor(value);
  if (remainder === 0) {
    return "0";
  }

  let encoded = "";
  const base = BASE62_ALPHABET.length;
  while (remainder > 0) {
    encoded = `${BASE62_ALPHABET[remainder % base]}${encoded}`;
    remainder = Math.floor(remainder / base);
  }

  return encoded;
}

function generateDefaultFilenameBase() {
  const timestampPart = encodeBase62(Date.now() * 1000);
  const randomPartInt = Math.floor(Math.random() * BASE62_ALPHABET.length ** 2);
  const randomPartBase62 = encodeBase62(randomPartInt).padStart(2, "0");
  return `scan_${timestampPart}${randomPartBase62}`;
}

function setFilenameInputError(hasError) {
  if (!filenameInput) {
    return;
  }

  filenameInput.classList.toggle("input-error", hasError);
}

function setFilenameInputValue(filenameBase) {
  if (!filenameInput) {
    return;
  }

  const nextValue = typeof filenameBase === "string" && filenameBase.trim()
    ? filenameBase.trim()
    : generateDefaultFilenameBase();
  filenameInput.value = nextValue;
  setFilenameInputError(false);
}

function renderHeaderAccount(username) {
  if (!headerUsername) {
    return;
  }

  const normalizedUsername = typeof username === "string" && username.trim()
    ? username.trim()
    : "Unknown user";
  headerUsername.textContent = normalizedUsername;
}

function setAccountMenuOpen(isOpen) {
  if (!accountMenuButton || !accountMenuList) {
    return;
  }

  accountMenuButton.setAttribute("aria-expanded", isOpen ? "true" : "false");
  accountMenuList.hidden = !isOpen;
}

function isAccountMenuOpen() {
  if (!accountMenuButton || !accountMenuList) {
    return false;
  }

  return accountMenuButton.getAttribute("aria-expanded") === "true";
}

function closeAccountMenu() {
  setAccountMenuOpen(false);
}

function clearUiForLoggedOutState() {
  closeAccountMenu();
  renderHeaderAccount("Unknown user");
  selectedDeviceName = null;
  selectedDeviceBusy = false;
  activeScanDeviceName = null;
  localScanPhaseActive = false;
  completionFlashDeviceName = null;
  clearTimeoutCountdownState();

  for (const [taskId] of paperlessPollTimers.entries()) {
    stopTaskPolling(taskId);
  }
  recentPaperlessTasks.splice(0, recentPaperlessTasks.length);
  renderRecentUploads();

  if (deviceMap instanceof Map) {
    deviceMap.clear();
  }
  if (deviceRadioGroups) {
    deviceRadioGroups.innerHTML = "";
  }
  if (deviceConfigFieldset) {
    deviceConfigFieldset.disabled = true;
  }
  renderDeviceDetails(null);
  updateScanButtonState();
  setStatus("logged_out", "logged out");
}

function normalizeFilenameBaseInput(rawValue) {
  if (typeof rawValue !== "string") {
    return "";
  }

  const trimmed = rawValue.trim();
  return trimmed.replace(/(?:\.pdf)+$/i, "");
}

function initializeFilenameInputInteractions() {
  if (!filenameInput) {
    return;
  }

  filenameInput.addEventListener("input", () => {
    setFilenameInputError(false);
  });
}

function getDeepLinkedDeviceName() {
  const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const deepLinkedValue = hashParams.get("device");
  if (typeof deepLinkedValue !== "string") {
    return null;
  }

  const normalizedValue = deepLinkedValue.trim();
  return normalizedValue ? normalizedValue : null;
}

function updateConfigurationDeepLink(deviceName) {
  if (!window.history || typeof window.history.replaceState !== "function") {
    return;
  }

  const nextParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  if (typeof deviceName === "string" && deviceName.trim()) {
    nextParams.set("device", deviceName);
  } else {
    nextParams.delete("device");
  }

  const nextHash = nextParams.toString();
  const nextUrl = `${window.location.pathname}${window.location.search}${nextHash ? `#${nextHash}` : ""}`;
  window.history.replaceState(null, "", nextUrl);
}

function setStatus(status, message, statsLines = []) {
  currentStatus = status;
  currentMessage = message;
  currentStatsLines = statsLines;
  renderCurrentStatus();
}

function isLocallyScanning() {
  return (
    localScanPhaseActive
    && activeScanDeviceName !== null
    && selectedDeviceName === activeScanDeviceName
  );
}

function triggerCompletionFlash() {
  if (!flashTarget) {
    return;
  }

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) {
    flashTarget.classList.remove("scan-complete-flash-reduced");
    void flashTarget.offsetWidth;
    flashTarget.classList.add("scan-complete-flash-reduced");
    window.setTimeout(() => {
      flashTarget.classList.remove("scan-complete-flash-reduced");
    }, 120);
    return;
  }

  flashTarget.classList.remove("scan-complete-flash");
  void flashTarget.offsetWidth;
  flashTarget.classList.add("scan-complete-flash");
  window.setTimeout(() => {
    flashTarget.classList.remove("scan-complete-flash");
  }, 2000);
}

function updateScanButtonState() {
  if (!scanButton) return;
  const noDeviceSelected = !selectedDeviceName;
  const busyOrScanning = selectedDeviceBusy || isLocallyScanning();
  const nextDisabled = noDeviceSelected || busyOrScanning;
  scanButton.disabled = nextDisabled;

  if (
    lastScanButtonDisabled === true
    && nextDisabled === false
    && lastScanButtonBusyOrScanning
    && completionFlashDeviceName
    && selectedDeviceName === completionFlashDeviceName
  ) {
    triggerCompletionFlash();
    completionFlashDeviceName = null;
  }

  lastScanButtonDisabled = nextDisabled;
  lastScanButtonBusyOrScanning = busyOrScanning;
}

function buildTimeoutSuffixForTarget(nowMs, targetState) {
  if (!Number.isFinite(targetState.timeoutSeconds) || targetState.startedAtMs === null) {
    return "";
  }

  const elapsedSeconds = (nowMs - targetState.startedAtMs) / 1000;
  if (
    elapsedSeconds < timeoutState.countdownStartSeconds ||
    elapsedSeconds >= targetState.timeoutSeconds
  ) {
    return "";
  }

  const remainingSeconds = Math.max(Math.ceil(targetState.timeoutSeconds - elapsedSeconds), 0);
  return ` (timeout ${targetState.label}: ${remainingSeconds}s)`;
}

function formatTimeoutSuffix() {
  const nowMs = Date.now();
  if (timeoutState.phase === "scanning") {
    return buildTimeoutSuffixForTarget(nowMs, timeoutState.scan);
  }
  if (timeoutState.phase === "uploading") {
    return buildTimeoutSuffixForTarget(nowMs, timeoutState.upload);
  }
  return "";
}

function escapeTaskId(taskId) {
  return encodeURIComponent(taskId);
}

function buildDocumentUrl(relatedDocumentId) {
  if (!relatedDocumentId || !paperlessBaseUrl) {
    return null;
  }

  return `${paperlessBaseUrl}/documents/${relatedDocumentId}`;
}

function formatSubmittedAt(submittedAt) {
  if (typeof submittedAt !== "number") {
    return "n/a";
  }

  try {
    return new Date(submittedAt).toLocaleString();
  } catch (_error) {
    return "n/a";
  }
}

function buildStatusChipClass(taskStatus) {
  const normalized = String(taskStatus ?? "unknown").toLowerCase();
  if (normalized === "started") return "task-status-chip task-status-started";
  if (normalized === "pending") return "task-status-chip task-status-pending";
  if (normalized === "success") return "task-status-chip task-status-success";
  if (normalized === "failure") return "task-status-chip task-status-failure";
  return "task-status-chip task-status-pending";
}

function formatTaskIdDisplay(taskId) {
  if (typeof taskId !== "string") {
    return "n/a";
  }

  if (taskId.length <= 14) {
    return taskId;
  }

  return `${taskId.slice(0, 8)}…${taskId.slice(-4)}`;
}

function upsertRecentTaskEntry(taskId, updates = {}) {
  const existingIndex = recentPaperlessTasks.findIndex((entry) => entry.taskId === taskId);
  if (existingIndex >= 0) {
    recentPaperlessTasks[existingIndex] = {
      ...recentPaperlessTasks[existingIndex],
      ...updates,
      taskId,
    };
    return recentPaperlessTasks[existingIndex];
  }

  const entry = {
    taskId,
    submittedAt: Date.now(),
    deviceName: null,
    fileName: null,
    taskStatus: "STARTED",
    resultText: null,
    relatedDocumentId: null,
    documentUrl: null,
    isPolling: true,
    lastError: null,
    lastUpdatedAt: Date.now(),
    pollFailureCount: 0,
    ...updates,
  };

  recentPaperlessTasks.unshift(entry);
  while (recentPaperlessTasks.length > MAX_RECENT_UPLOADS) {
    const removedEntry = recentPaperlessTasks.pop();
    if (removedEntry?.taskId) {
      stopTaskPolling(removedEntry.taskId);
    }
  }

  return entry;
}

function normalizeRecentUploadEntryFromServer(rawEntry) {
  const taskId = typeof rawEntry?.task_id === "string" ? rawEntry.task_id.trim() : "";
  if (!taskId) {
    return null;
  }

  const taskStatus = typeof rawEntry?.task_status === "string"
    ? rawEntry.task_status
    : "STARTED";
  const relatedDocumentId = rawEntry?.related_document
    ? String(rawEntry.related_document)
    : null;

  return {
    taskId,
    submittedAt: Number.isFinite(Number(rawEntry?.submitted_at))
      ? Number(rawEntry.submitted_at)
      : Date.now(),
    deviceName: typeof rawEntry?.device_name === "string" && rawEntry.device_name.trim()
      ? rawEntry.device_name.trim()
      : null,
    fileName: typeof rawEntry?.file_name === "string" && rawEntry.file_name.trim()
      ? rawEntry.file_name.trim()
      : null,
    taskStatus,
    resultText: typeof rawEntry?.result_text === "string" ? rawEntry.result_text : null,
    relatedDocumentId,
    documentUrl: buildDocumentUrl(relatedDocumentId),
    isPolling: rawEntry?.is_polling === true || !PAPERLESS_TERMINAL_STATUSES.has(taskStatus),
    lastError: typeof rawEntry?.last_error === "string" ? rawEntry.last_error : null,
    lastUpdatedAt: Number.isFinite(Number(rawEntry?.last_updated_at))
      ? Number(rawEntry.last_updated_at)
      : Date.now(),
    pollFailureCount: Number.isFinite(Number(rawEntry?.poll_failure_count))
      ? Number(rawEntry.poll_failure_count)
      : 0,
  };
}

async function loadRecentUploadsFromServer() {
  try {
    const response = await fetch("/api/recent-uploads", { method: "GET" });
    if (!response.ok) {
      renderRecentUploads();
      return;
    }

    const payload = await response.json();
    const serverEntries = Array.isArray(payload?.recent_uploads)
      ? payload.recent_uploads
      : [];

    recentPaperlessTasks.splice(0, recentPaperlessTasks.length);
    for (const rawEntry of serverEntries) {
      const normalizedEntry = normalizeRecentUploadEntryFromServer(rawEntry);
      if (!normalizedEntry) {
        continue;
      }

      recentPaperlessTasks.push(normalizedEntry);
    }

    for (const [taskId] of paperlessPollTimers.entries()) {
      stopTaskPolling(taskId);
    }

    for (const entry of recentPaperlessTasks) {
      if (!entry?.taskId || !entry.isPolling) {
        continue;
      }
      scheduleTaskPolling(entry.taskId, 0);
    }

    renderRecentUploads();
  } catch (_error) {
    renderRecentUploads();
  }
}

function renderRecentUploads() {
  if (!recentUploadsBody) {
    return;
  }

  recentUploadsBody.innerHTML = "";
  if (recentPaperlessTasks.length === 0) {
    const emptyRow = document.createElement("tr");
    const emptyCell = document.createElement("td");
    emptyCell.colSpan = 7;
    emptyCell.textContent = "No recent uploads yet.";
    emptyRow.appendChild(emptyCell);
    recentUploadsBody.appendChild(emptyRow);
    return;
  }

  for (const entry of recentPaperlessTasks) {
    const row = document.createElement("tr");
    const submittedCell = document.createElement("td");
    submittedCell.textContent = formatSubmittedAt(entry.submittedAt);

    const deviceCell = document.createElement("td");
    deviceCell.textContent = entry.deviceName ?? "unknown device";

    const fileCell = document.createElement("td");
    fileCell.textContent = entry.fileName ?? "unknown file";

    const taskIdCell = document.createElement("td");
    taskIdCell.className = "task-id";
    taskIdCell.title = entry.taskId ?? "";
    taskIdCell.textContent = formatTaskIdDisplay(entry.taskId);

    const statusCell = document.createElement("td");
    const statusLabel = entry.taskStatus ?? "unknown";
    const statusChip = document.createElement("span");
    statusChip.className = buildStatusChipClass(statusLabel);
    statusChip.textContent = statusLabel;
    statusCell.appendChild(statusChip);

    const resultCell = document.createElement("td");
    const resultLabel = entry.lastError
      ? `poll_error: ${entry.lastError}`
      : (entry.resultText ?? "");
    resultCell.textContent = resultLabel || "—";

    const docCell = document.createElement("td");

    if (entry.documentUrl) {
      const link = document.createElement("a");
      link.href = entry.documentUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open document";
      docCell.appendChild(link);
    } else {
      docCell.textContent = "—";
    }

    row.appendChild(submittedCell);
    row.appendChild(deviceCell);
    row.appendChild(fileCell);
    row.appendChild(taskIdCell);
    row.appendChild(statusCell);
    row.appendChild(resultCell);
    row.appendChild(docCell);
    recentUploadsBody.appendChild(row);
  }
}

function stopTaskPolling(taskId) {
  const timer = paperlessPollTimers.get(taskId);
  if (timer) {
    clearTimeout(timer);
    paperlessPollTimers.delete(taskId);
  }

  const existingIndex = recentPaperlessTasks.findIndex((entry) => entry.taskId === taskId);
  if (existingIndex >= 0) {
    recentPaperlessTasks[existingIndex] = {
      ...recentPaperlessTasks[existingIndex],
      isPolling: false,
    };
  }
}

function scheduleTaskPolling(taskId, delayMs) {
  const existingTimer = paperlessPollTimers.get(taskId);
  if (existingTimer) {
    clearTimeout(existingTimer);
  }

  const timer = setTimeout(() => {
    void pollPaperlessTask(taskId);
  }, delayMs);
  paperlessPollTimers.set(taskId, timer);
}

async function pollPaperlessTask(taskId) {
  const entry = recentPaperlessTasks.find((taskEntry) => taskEntry.taskId === taskId);
  if (!entry) {
    stopTaskPolling(taskId);
    renderRecentUploads();
    return;
  }

  try {
    const response = await fetch(`/api/paperless/tasks/${escapeTaskId(taskId)}`, {
      method: "GET",
    });
    const payload = await response.json();
    if (!response.ok || payload?.status !== "ok") {
      const nextFailureCount = (entry.pollFailureCount ?? 0) + 1;
      const message = payload?.message ?? "polling failed";
      upsertRecentTaskEntry(taskId, {
        isPolling: true,
        lastError: message,
        lastUpdatedAt: Date.now(),
        pollFailureCount: nextFailureCount,
      });
      const backoffDelay = Math.min(
        PAPERLESS_POLL_INTERVAL_MS * 2 ** nextFailureCount,
        PAPERLESS_MAX_RETRY_INTERVAL_MS
      );
      renderRecentUploads();
      scheduleTaskPolling(taskId, backoffDelay);
      return;
    }

    const nextTaskStatus = payload?.task_status ?? "unknown";
    const isTerminalStatus = PAPERLESS_TERMINAL_STATUSES.has(nextTaskStatus);
    const relatedDocumentId = payload?.related_document ? String(payload.related_document) : null;
    const nextEntry = upsertRecentTaskEntry(taskId, {
      taskStatus: nextTaskStatus,
      resultText: payload?.result ? String(payload.result) : null,
      relatedDocumentId,
      documentUrl: buildDocumentUrl(relatedDocumentId),
      fileName: payload?.task_file_name ? String(payload.task_file_name) : entry.fileName,
      isPolling: !isTerminalStatus,
      lastError: null,
      lastUpdatedAt: Date.now(),
      pollFailureCount: 0,
    });

    renderRecentUploads();
    if (!isTerminalStatus) {
      scheduleTaskPolling(taskId, PAPERLESS_POLL_INTERVAL_MS);
      return;
    }

    stopTaskPolling(taskId);
  } catch (_error) {
    const nextFailureCount = (entry.pollFailureCount ?? 0) + 1;
    upsertRecentTaskEntry(taskId, {
      isPolling: true,
      lastError: "error contacting backend",
      lastUpdatedAt: Date.now(),
      pollFailureCount: nextFailureCount,
    });
    const backoffDelay = Math.min(
      PAPERLESS_POLL_INTERVAL_MS * 2 ** nextFailureCount,
      PAPERLESS_MAX_RETRY_INTERVAL_MS
    );
    renderRecentUploads();
    scheduleTaskPolling(taskId, backoffDelay);
  }
}

function registerPaperlessTaskFromScanPayload(payload) {
  const taskId = payload?.paperless_task_id;
  if (typeof taskId !== "string" || taskId.trim() === "") {
    return;
  }

  const normalizedTaskId = taskId.trim();
  const payloadDeviceName = typeof payload?.device_name === "string"
    ? payload.device_name.trim()
    : "";
  const payloadFilename = typeof payload?.filename === "string"
    ? payload.filename.trim()
    : "";
  const normalizedDeviceName = payloadDeviceName || activeScanDeviceName || selectedDeviceName || null;
  upsertRecentTaskEntry(normalizedTaskId, {
    submittedAt: Date.now(),
    deviceName: normalizedDeviceName,
    fileName: payloadFilename || null,
    taskStatus: "STARTED",
    resultText: null,
    relatedDocumentId: null,
    documentUrl: null,
    isPolling: true,
    lastError: null,
    lastUpdatedAt: Date.now(),
    pollFailureCount: 0,
  });
  renderRecentUploads();
  scheduleTaskPolling(normalizedTaskId, 0);
}

function isPaperlessUploadFailureMessage(message) {
  if (typeof message !== "string") {
    return false;
  }

  return message.includes("Paperless upload request failed")
    || message.includes("Paperless upload failed (");
}

function registerPaperlessUploadFailure(payload, options = {}) {
  const status = payload?.status;
  if (status !== "error") {
    return options.existingFailureTaskId ?? null;
  }

  const message = typeof payload?.message === "string" ? payload.message.trim() : "";
  if (!message) {
    return options.existingFailureTaskId ?? null;
  }

  const phaseBeforeUpdate = options.phaseBeforeUpdate;
  const uploadPhaseFailure = phaseBeforeUpdate === "uploading";
  const messageIndicatesUploadFailure = isPaperlessUploadFailureMessage(message);
  if (!uploadPhaseFailure && !messageIndicatesUploadFailure) {
    return options.existingFailureTaskId ?? null;
  }

  const payloadTaskId = typeof payload?.paperless_task_id === "string"
    ? payload.paperless_task_id.trim()
    : "";
  const taskId = payloadTaskId
    || options.existingFailureTaskId
    || `upload-failure-${Date.now()}`;
  const payloadDeviceName = typeof payload?.device_name === "string"
    ? payload.device_name.trim()
    : "";
  const deviceName = payloadDeviceName
    || options.fallbackDeviceName
    || activeScanDeviceName
    || selectedDeviceName
    || null;

  upsertRecentTaskEntry(taskId, {
    submittedAt: Date.now(),
    deviceName,
    taskStatus: "FAILURE",
    resultText: message,
    relatedDocumentId: null,
    documentUrl: null,
    isPolling: false,
    lastError: null,
    lastUpdatedAt: Date.now(),
    pollFailureCount: 0,
  });
  stopTaskPolling(taskId);
  renderRecentUploads();
  return taskId;
}

function renderCurrentStatus() {
  if (statusText) {
    statusText.textContent = `Status: ${currentStatus} (${currentMessage}${formatTimeoutSuffix()})`;
  }

  if (statusStats) {
    statusStats.textContent = currentStatsLines.join("\n");
  }
}

setInterval(() => {
  if (!statusText) return;
  if (timeoutState.phase !== "scanning" && timeoutState.phase !== "uploading") return;
  renderCurrentStatus();
}, 1000);

function applyTimeoutMetadata(payload) {
  const nextCountdownStart = Number(payload?.timeout_countdown_start_seconds);
  if (Number.isFinite(nextCountdownStart) && nextCountdownStart >= 0) {
    timeoutState.countdownStartSeconds = nextCountdownStart;
  }

  const nextScanTimeout = Number(payload?.scan_timeout_seconds);
  if (Number.isFinite(nextScanTimeout) && nextScanTimeout > 0) {
    timeoutState.scan.timeoutSeconds = nextScanTimeout;
  }

  const nextPaperlessTimeout = Number(payload?.paperless_timeout_seconds);
  if (Number.isFinite(nextPaperlessTimeout) && nextPaperlessTimeout > 0) {
    timeoutState.upload.timeoutSeconds = nextPaperlessTimeout;
  }
}

function updatePhaseState(payload, status, nowMs) {
  if (status === "scanning") {
    timeoutState.phase = "scanning";
    if (timeoutState.scan.startedAtMs === null || Number.isFinite(Number(payload?.page_count))) {
      timeoutState.scan.startedAtMs = nowMs;
    }
    return;
  }

  if (status === "uploading") {
    timeoutState.phase = "uploading";
    if (timeoutState.upload.startedAtMs === null) {
      timeoutState.upload.startedAtMs = nowMs;
    }
    return;
  }

  if (status === "processing") {
    timeoutState.phase = "processing";
    return;
  }

  if (payload?.complete || status === "ok" || status === "error") {
    timeoutState.phase = "done";
  }
}

function formatTimingStats(timingMetrics) {
  if (!timingMetrics || typeof timingMetrics !== "object") {
    return [];
  }

  const totalSeconds = Math.round(Number(timingMetrics.total_seconds));
  const scanSeconds = Math.round(Number(timingMetrics.scan_seconds));
  const paperlessSeconds = Math.round(Number(timingMetrics.paperless_seconds));
  const scanSecondsPerPage = Math.round(Number(timingMetrics.scan_seconds_per_page));
  const paperlessSecondsPerPage = Math.round(Number(timingMetrics.paperless_seconds_per_page));

  if (
    !Number.isFinite(totalSeconds) ||
    !Number.isFinite(scanSeconds) ||
    !Number.isFinite(paperlessSeconds) ||
    !Number.isFinite(scanSecondsPerPage) ||
    !Number.isFinite(paperlessSecondsPerPage)
  ) {
    return [];
  }

  return [
    `Total: ${totalSeconds}s`,
    `Scan: ${scanSeconds}s`,
    `Paperless: ${paperlessSeconds}s`,
    `Scan/page: ${scanSecondsPerPage}s`,
    `Paperless/page: ${paperlessSecondsPerPage}s`,
  ];
}

function stripTimingStatsFromMessage(message) {
  if (typeof message !== "string") {
    return "No message provided";
  }

  return message
    .replace(
      /\s+total=\d+(?:\.\d+)?s\s+scan=\d+(?:\.\d+)?s\s+paperless=\d+(?:\.\d+)?s\s+scan_per_page=\d+(?:\.\d+)?s\s+paperless_per_page=\d+(?:\.\d+)?s/g,
      ""
    )
    .trim();
}

function buildStatusPresentation(payload) {
  const status = payload?.status ?? "unknown";
  const baseMessage = payload?.message ?? "No message provided";
  if (status !== "ok") {
    return {
      message: baseMessage,
      statsLines: [],
    };
  }

  return {
    message: stripTimingStatsFromMessage(baseMessage),
    statsLines: formatTimingStats(payload?.timing_metrics),
  };
}

function renderDeviceDetails(selectedDevice) {
  if (!deviceDetails) return;

  if (!selectedDevice) {
    deviceDetails.textContent = "No device selected.";
    return;
  }

  const detailLines = [
    `Name: ${selectedDevice.device_name ?? "n/a"}`,
    `Configured ID: ${selectedDevice.device_id ?? "n/a"}`,
    `Runtime ID: ${selectedDevice.scanimage_device_name ?? "n/a"}`,
    `Scan command: ${selectedDevice.scan_command ?? "n/a"}`,
    `Scan timeout: ${selectedDevice.scan_timeout_seconds ?? "n/a"}`,
  ];

  const scanimageParams = selectedDevice.scanimage_params ?? {};
  const scanimageParamKeys = Object.keys(scanimageParams).sort();
  if (scanimageParamKeys.length > 0) {
    detailLines.push("scanimage params:");
    for (const paramKey of scanimageParamKeys) {
      detailLines.push(`- ${paramKey}: ${scanimageParams[paramKey]}`);
    }
  } else {
    detailLines.push("scanimage params: none");
  }

  deviceDetails.textContent = detailLines.join("\n");
}

function syncDeviceDetailsSummaryLabel() {
  if (!deviceDetailsSummary || !deviceDetailsPanel) {
    return;
  }

  deviceDetailsSummary.textContent = deviceDetailsPanel.open ? "Hide details" : "Show details";
}

function buildDeviceOptionSummary(selectedDevice) {
  if (!selectedDevice || typeof selectedDevice !== "object") {
    return "";
  }

  const params = selectedDevice.scanimage_params ?? {};
  const mode = params.mode ? `${params.mode}` : null;
  const resolution = params.resolution ? `${params.resolution}dpi` : null;
  const source = params.source ? `${params.source}` : null;
  return [mode, resolution, source].filter(Boolean).join(", ");
}

function renderDeviceRadioGroups(devices, selectedDeviceName) {
  if (!deviceRadioGroups || !deviceConfigFieldset) {
    return;
  }

  deviceRadioGroups.innerHTML = "";
  if (!Array.isArray(devices) || devices.length === 0) {
    const empty = document.createElement("p");
    empty.className = "device-empty";
    empty.textContent = "No device configurations available.";
    deviceRadioGroups.appendChild(empty);
    deviceConfigFieldset.disabled = true;
    return;
  }

  const grouped = new Map();
  for (const device of devices) {
    const groupKey = device?.device_id || "unknown_device";
    if (!grouped.has(groupKey)) {
      grouped.set(groupKey, []);
    }
    grouped.get(groupKey).push(device);
  }

  for (const [deviceId, groupDevices] of grouped.entries()) {
    const groupContainer = document.createElement("div");
    groupContainer.className = "device-group";

    const groupHeader = document.createElement("div");
    groupHeader.className = "device-group-header";
    groupHeader.textContent = deviceId;
    groupContainer.appendChild(groupHeader);

    for (const device of groupDevices) {
      const optionId = `device-option-${device.device_name}`;
      const optionRow = document.createElement("label");
      optionRow.className = "device-option";
      optionRow.htmlFor = optionId;

      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "deviceConfig";
      radio.id = optionId;
      radio.value = device.device_name;
      radio.checked = device.device_name === selectedDeviceName;

      const textWrap = document.createElement("span");
      const main = document.createElement("span");
      main.className = "device-option-main";
      main.textContent = device.device_name ?? "unnamed";
      textWrap.appendChild(main);

      const summary = buildDeviceOptionSummary(device);
      if (summary) {
        const meta = document.createElement("span");
        meta.className = "device-option-meta";
        meta.textContent = ` (${summary})`;
        textWrap.appendChild(meta);
      }

      optionRow.appendChild(radio);
      optionRow.appendChild(textWrap);
      groupContainer.appendChild(optionRow);
    }

    deviceRadioGroups.appendChild(groupContainer);
  }

  deviceConfigFieldset.disabled = false;
}

function applyDeviceConfigurations(payload) {
  const devices = payload?.devices ?? [];
  deviceMap = new Map(devices.map((device) => [device.device_name, device]));
  selectedDeviceName = payload?.selected_device_name ?? null;

  const deepLinkedDeviceName = getDeepLinkedDeviceName();
  if (deepLinkedDeviceName && deviceMap.has(deepLinkedDeviceName)) {
    selectedDeviceName = deepLinkedDeviceName;
  }

  paperlessBaseUrl = typeof payload?.paperless_base_url === "string"
    ? payload.paperless_base_url.replace(/\/$/, "")
    : "";

  const hasSelectedDevice = selectedDeviceName && deviceMap.has(selectedDeviceName);
  if (!hasSelectedDevice) {
    selectedDeviceName = devices[0]?.device_name ?? null;
  }

  renderHeaderAccount(payload?.username);
  setFilenameInputValue(payload?.default_filename_base);
  renderDeviceRadioGroups(devices, selectedDeviceName);
  renderDeviceDetails(deviceMap.get(selectedDeviceName) ?? null);
  updateConfigurationDeepLink(selectedDeviceName);
  updateScanButtonState();
}

async function triggerLogout() {
  if (!logoutButton) {
    return;
  }

  closeAccountMenu();
  if (accountMenuButton) {
    accountMenuButton.disabled = true;
  }
  logoutButton.disabled = true;
  try {
    const response = await fetch("/auth/logout", { method: "POST" });
    if (!response.ok) {
      setStatus("error", "logout failed");
      return;
    }

    clearUiForLoggedOutState();
    window.setTimeout(() => {
      window.location.reload();
    }, 120);
  } catch (_error) {
    setStatus("error", "error contacting backend");
  } finally {
    logoutButton.disabled = false;
    if (accountMenuButton) {
      accountMenuButton.disabled = false;
    }
  }
}

function loadDemoData() {
  setFilenameInputValue(generateDefaultFilenameBase());
  applyDeviceConfigurations({
    selected_device_name: "duplex_bw",
    paperless_base_url: "https://paperless.example.local",
    devices: [
      {
        device_name: "default",
        device_id: "brother_ads_2200",
        scanimage_device_name: "brother_ads_2200",
        scan_command: "scanimage",
        scan_timeout_seconds: "90",
        scanimage_params: {
          mode: "Color",
          resolution: "300",
          source: "Automatic Document Feeder",
        },
      },
      {
        device_name: "duplex_bw",
        device_id: "brother_ads_2200",
        scanimage_device_name: "brother_ads_2200",
        scan_command: "scanimage",
        scan_timeout_seconds: "90",
        scanimage_params: {
          mode: "Gray",
          resolution: "300",
          source: "Automatic Document Feeder",
          batch: "yes",
        },
      },
      {
        device_name: "receipt_mode",
        device_id: "fujitsu_ix500",
        scanimage_device_name: "fujitsu_ix500",
        scan_command: "scanimage",
        scan_timeout_seconds: "60",
        scanimage_params: {
          mode: "Gray",
          resolution: "200",
          source: "Flatbed",
        },
      },
    ],
  });

  upsertRecentTaskEntry("task-demo-success", {
    submittedAt: Date.now() - 3 * 60 * 1000,
    deviceName: "duplex_bw",
    fileName: "invoice_2026-02.pdf",
    taskStatus: "SUCCESS",
    resultText: "Success. New document id 48 created",
    relatedDocumentId: "48",
    documentUrl: "https://paperless.example.local/documents/48",
    isPolling: false,
    pollFailureCount: 0,
  });

  upsertRecentTaskEntry("task-demo-pending", {
    submittedAt: Date.now() - 40 * 1000,
    deviceName: "default",
    fileName: "delivery_note_2026-02.pdf",
    taskStatus: "PENDING",
    resultText: "Queued in Paperless task worker",
    isPolling: true,
    pollFailureCount: 0,
  });

  upsertRecentTaskEntry("task-demo-failure", {
    submittedAt: Date.now() - 8 * 60 * 1000,
    deviceName: "receipt_mode",
    fileName: "receipt_batch_12.pdf",
    taskStatus: "FAILURE",
    resultText: "OCR pipeline failed: unsupported encoding",
    isPolling: false,
    pollFailureCount: 0,
  });

  renderRecentUploads();
  setStatus("idle", "demo mode loaded");

  window.setTimeout(() => {
    upsertRecentTaskEntry("task-demo-pending", {
      taskStatus: "STARTED",
      resultText: "Processing in Paperless worker",
      isPolling: true,
      lastError: null,
      relatedDocumentId: null,
      documentUrl: null,
      lastUpdatedAt: Date.now(),
    });
    renderRecentUploads();
  }, 1800);

  window.setTimeout(() => {
    upsertRecentTaskEntry("task-demo-pending", {
      taskStatus: "SUCCESS",
      resultText: "Success. New document id 57 created",
      relatedDocumentId: "57",
      documentUrl: "https://paperless.example.local/documents/57",
      isPolling: false,
      lastError: null,
      lastUpdatedAt: Date.now(),
    });
    renderRecentUploads();
  }, 3600);
}

async function refreshScanStatus() {
  if (!selectedDeviceName) {
    selectedDeviceBusy = false;
    updateScanButtonState();
    return;
  }

  if (refreshingScanStatus) {
    refreshScanStatusPending = true;
    return;
  }

  const requestedDeviceName = selectedDeviceName;
  const requestId = ++refreshScanStatusRequestCounter;
  refreshingScanStatus = true;
  try {
    const params = new URLSearchParams({ device_name: requestedDeviceName });
    const response = await fetch(`/api/scan/status?${params.toString()}`, { method: "GET" });

    const isCurrentSelection = selectedDeviceName === requestedDeviceName;
    const isLatestRequest = requestId === refreshScanStatusRequestCounter;
    if (!isCurrentSelection || !isLatestRequest) {
      return;
    }

    if (!response.ok) {
      selectedDeviceBusy = false;
      updateScanButtonState();
      return;
    }

    const payload = await response.json();
    selectedDeviceBusy = payload?.in_progress === true;
    updateScanButtonState();

    if (selectedDeviceBusy && !isLocallyScanning()) {
      const lockId = payload?.device_lock_id ?? selectedDeviceName;
      setStatus("busy", `scan in progress on ${lockId}`);
    }
  } catch (_error) {
    if (
      selectedDeviceName === requestedDeviceName
      && requestId === refreshScanStatusRequestCounter
    ) {
      selectedDeviceBusy = false;
      updateScanButtonState();
    }
  } finally {
    refreshingScanStatus = false;
    if (refreshScanStatusPending) {
      refreshScanStatusPending = false;
      void refreshScanStatus();
    }
  }
}

async function loadDeviceConfigurations() {
  try {
    const response = await fetch("/api/device-configurations", { method: "GET" });
    if (!response.ok) {
      statusText.textContent = "Status: failed to load device configuration";
      return;
    }

    const payload = await response.json();
    applyDeviceConfigurations(payload);
    await loadRecentUploadsFromServer();
    await refreshScanStatus();
  } catch (_error) {
    statusText.textContent = "Status: error loading device configuration";
  }
}

async function selectDeviceConfiguration(deviceName) {
  selectedDeviceName = deviceName;
  selectedDeviceBusy = activeScanDeviceName !== null && selectedDeviceName === activeScanDeviceName;
  renderDeviceDetails(deviceMap.get(selectedDeviceName) ?? null);
  updateConfigurationDeepLink(selectedDeviceName);
  updateScanButtonState();
  setStatus("selected", selectedDeviceName);
  await refreshScanStatus();
}

async function triggerScan() {
  if (!statusText || !scanButton) return;
  const normalizedFilenameBase = normalizeFilenameBaseInput(filenameInput?.value ?? "");
  if (normalizedFilenameBase === "") {
    setFilenameInputError(true);
    setStatus("error", "Filename cannot be empty");
    return;
  }
  setFilenameInputError(false);

  if (isDemoMode) {
    completionFlashDeviceName = selectedDeviceName;
    localScanPhaseActive = true;
    selectedDeviceBusy = true;
    updateScanButtonState();
    setFilenameInputValue(generateDefaultFilenameBase());
    setStatus("triggering", "triggering...");
    await new Promise((resolve) => {
      window.setTimeout(resolve, 450);
    });
    localScanPhaseActive = false;
    selectedDeviceBusy = false;
    updateScanButtonState();
    setStatus("ok", "demo scan complete");
    return;
  }
  if (selectedDeviceBusy) {
    setStatus("busy", "scan in progress for selected device");
    updateScanButtonState();
    return;
  }

  clearTimeoutCountdownState();
  activeScanDeviceName = selectedDeviceName;
  completionFlashDeviceName = selectedDeviceName;
  localScanPhaseActive = true;
  selectedDeviceBusy = true;
  updateScanButtonState();
  setFilenameInputValue(generateDefaultFilenameBase());
  setStatus("triggering", "triggering...");

  try {
    const response = await fetch("/api/scan/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        device_name: selectedDeviceName,
        filename_base: normalizedFilenameBase,
      }),
    });
    if (!response.ok || !response.body) {
      let message = "invalid backend response";
      try {
        const errorPayload = await response.json();
        if (typeof errorPayload?.message === "string") {
          message = errorPayload.message;
        }
      } catch (_parseError) {
        // ignore parse error and keep default message
      }

      setStatus(response.status === 409 ? "busy" : "error", message);
      if (typeof message === "string" && message.includes("Filename cannot be empty")) {
        setFilenameInputError(true);
      }
      if (response.status === 409) {
        completionFlashDeviceName = null;
      }
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let bufferedText = "";
    let failureTaskId = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      bufferedText += decoder.decode(value, { stream: true });
      const lines = bufferedText.split("\n");
      bufferedText = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.trim()) continue;

        let payload;
        try {
          payload = JSON.parse(line);
        } catch (error) {
          setStatus("error", "invalid backend response");
          continue;
        }

        const status = payload?.status ?? "unknown";
        const phaseBeforeUpdate = timeoutState.phase;
        const streamComplete =
          payload?.complete === true || status === "ok" || status === "error" || status === "busy";
        if (status === "scanning") {
          localScanPhaseActive = true;
        } else if (status === "processing" || status === "uploading" || streamComplete) {
          localScanPhaseActive = false;
        }
        const presentation = buildStatusPresentation(payload);
        const nowMs = Date.now();
        if (activeScanDeviceName && selectedDeviceName === activeScanDeviceName) {
          selectedDeviceBusy = !streamComplete;
        }
        applyTimeoutMetadata(payload);
        updatePhaseState(payload, status, nowMs);
        updateScanButtonState();
        setStatus(status, presentation.message, presentation.statsLines);
        registerPaperlessTaskFromScanPayload(payload);
        failureTaskId = registerPaperlessUploadFailure(payload, {
          phaseBeforeUpdate,
          existingFailureTaskId: failureTaskId,
          fallbackDeviceName: activeScanDeviceName || selectedDeviceName || null,
        });
      }
    }
  } catch (error) {
    clearTimeoutCountdownState();
    localScanPhaseActive = false;
    setStatus("error", "error contacting backend");
  } finally {
    clearTimeoutCountdownState();
    localScanPhaseActive = false;
    activeScanDeviceName = null;
    selectedDeviceBusy = false;
    updateScanButtonState();
    await refreshScanStatus();
  }
}

initializeFilenameInputInteractions();

if (scanButton) {
  scanButton.addEventListener("click", triggerScan);
}

if (logoutButton) {
  logoutButton.addEventListener("click", () => {
    void triggerLogout();
  });
}

if (accountMenuButton && accountMenuList) {
  accountMenuButton.addEventListener("click", () => {
    setAccountMenuOpen(!isAccountMenuOpen());
  });

  document.addEventListener("click", (event) => {
    if (!isAccountMenuOpen()) {
      return;
    }

    const target = event.target;
    if (!(target instanceof Node)) {
      closeAccountMenu();
      return;
    }

    if (accountMenuButton.contains(target) || accountMenuList.contains(target)) {
      return;
    }

    closeAccountMenu();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeAccountMenu();
    }
  });
}

if (deviceRadioGroups) {
  deviceRadioGroups.addEventListener("change", async (event) => {
    const target = event?.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.name !== "deviceConfig") return;
    const nextDeviceName = target.value;
    if (!nextDeviceName) return;
    await selectDeviceConfiguration(nextDeviceName);
  });
}

if (deviceDetailsPanel) {
  deviceDetailsPanel.addEventListener("toggle", syncDeviceDetailsSummaryLabel);
}

syncDeviceDetailsSummaryLabel();

setInterval(() => {
  if (!selectedDeviceName) return;
  if (isLocallyScanning()) return;
  if (isDemoMode) return;
  void refreshScanStatus();
}, 5000);

if (isDemoMode) {
  loadDemoData();
} else {
  void loadDeviceConfigurations();
}
