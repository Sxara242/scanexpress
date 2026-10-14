const queueElement = document.getElementById("pendingQueue");
const queueSummary = document.getElementById("pendingQueueSummary");
const refreshButton = document.getElementById("refreshPendingButton");
const emptyState = document.getElementById("pendingEmptyState");
const review = document.getElementById("pendingReview");
const positionLabel = document.getElementById("pendingPosition");
const stateBadge = document.getElementById("pendingStateBadge");
const preview = document.getElementById("pendingPreview");
const fileSummary = document.getElementById("pendingFileSummary");
const downloadLink = document.getElementById("pendingDownloadLink");
const form = document.getElementById("pendingMetadataForm");
const titleInput = document.getElementById("pendingTitle");
const dateInput = document.getElementById("pendingDocumentDate");
const typeSelect = document.getElementById("pendingDocumentType");
const correspondentSelect = document.getElementById("pendingCorrespondent");
const storageSelect = document.getElementById("pendingStoragePath");
const tagsSelect = document.getElementById("pendingTags");
const previousButton = document.getElementById("previousPendingButton");
const nextButton = document.getElementById("nextPendingButton");
const submitButton = document.getElementById("submitPendingButton");
const statusElement = document.getElementById("pendingStatus");

let pendingItems = [];
let selectedId = window.location.hash.replace(/^#/, "") || null;
let metadataOptions = {
  tags: [],
  correspondents: [],
  document_types: [],
  storage_paths: [],
};
let pollTimer = null;

function setStatus(message, isError = false) {
  statusElement.textContent = message || "";
  statusElement.classList.toggle("pending-status-error", Boolean(isError));
}

function selectedItem() {
  return pendingItems.find((item) => item.id === selectedId) || null;
}

function selectedIndex() {
  return pendingItems.findIndex((item) => item.id === selectedId);
}

function optionLabel(item) {
  return typeof item?.name === "string" ? item.name : String(item?.id ?? "");
}

function fillSingleSelect(select, items, selectedValue) {
  select.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "—";
  select.appendChild(blank);

  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.id);
    option.textContent = optionLabel(item);
    option.selected = selectedValue !== null
      && selectedValue !== undefined
      && String(selectedValue) === String(item.id);
    select.appendChild(option);
  }
}

function fillTags(items, selectedIds) {
  const selected = new Set((selectedIds || []).map((value) => String(value)));
  tagsSelect.innerHTML = "";
  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.id);
    option.textContent = optionLabel(item);
    option.selected = selected.has(String(item.id));
    tagsSelect.appendChild(option);
  }
}

function renderQueue() {
  queueElement.innerHTML = "";
  queueSummary.textContent = pendingItems.length === 1
    ? "1 Pending document"
    : `${pendingItems.length} Pending documents`;

  if (!pendingItems.length) {
    const empty = document.createElement("p");
    empty.className = "muted-label";
    empty.textContent = "Queue is empty.";
    queueElement.appendChild(empty);
    return;
  }

  for (const item of pendingItems) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pending-queue-item";
    if (item.id === selectedId) {
      button.classList.add("pending-queue-item-selected");
    }
    button.dataset.pendingId = item.id;

    const name = document.createElement("strong");
    name.textContent = item.metadata?.title || item.original_filename || item.id;
    button.appendChild(name);

    const meta = document.createElement("span");
    meta.textContent = `${item.page_count} page(s) · ${item.state}`;
    button.appendChild(meta);

    button.addEventListener("click", () => {
      selectPending(item.id);
    });
    queueElement.appendChild(button);
  }
}

function renderSelected() {
  const item = selectedItem();
  const index = selectedIndex();

  if (!item) {
    emptyState.hidden = false;
    review.hidden = true;
    return;
  }

  emptyState.hidden = true;
  review.hidden = false;
  positionLabel.textContent = `${index + 1} / ${pendingItems.length}`;
  stateBadge.textContent = item.state;
  stateBadge.dataset.state = item.state;
  preview.src = `${item.file_url}#toolbar=1&navpanes=0`;
  downloadLink.href = item.download_url;
  fileSummary.textContent = `${item.original_filename || "scan.pdf"} · ${item.page_count} page(s)`;

  titleInput.value = item.metadata?.title || "";
  dateInput.value = item.metadata?.document_date || "";
  fillSingleSelect(typeSelect, metadataOptions.document_types, item.metadata?.document_type_id);
  fillSingleSelect(correspondentSelect, metadataOptions.correspondents, item.metadata?.correspondent_id);
  fillSingleSelect(storageSelect, metadataOptions.storage_paths, item.metadata?.storage_path_id);
  fillTags(metadataOptions.tags, item.metadata?.tag_ids || []);

  previousButton.disabled = index <= 0;
  nextButton.disabled = index < 0 || index >= pendingItems.length - 1;
  const locked = item.state === "submitted" || item.state === "submitting";
  submitButton.disabled = locked;
  form.querySelectorAll("input, select").forEach((control) => {
    control.disabled = item.state === "submitted";
  });

  if (item.state === "failed" && item.last_error) {
    setStatus(item.last_error, true);
  } else if (item.state === "submitting" && item.paperless_task_id) {
    setStatus("Paperless ingestion is in progress…");
    scheduleTaskPolling(item.paperless_task_id);
  } else if (item.state === "submitted") {
    const suffix = item.paperless_document_id
      ? ` Document ID: ${item.paperless_document_id}`
      : "";
    setStatus(`Submitted successfully.${suffix}`);
  } else {
    setStatus("");
  }
}

function render() {
  renderQueue();
  renderSelected();
}

function selectPending(id) {
  selectedId = id;
  window.history.replaceState(null, "", `#${id}`);
  render();
}

async function loadMetadata() {
  const response = await fetch("/api/paperless/metadata");
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.message || "Failed to load Paperless metadata.");
  }
  metadataOptions = {
    tags: Array.isArray(payload.tags) ? payload.tags : [],
    correspondents: Array.isArray(payload.correspondents) ? payload.correspondents : [],
    document_types: Array.isArray(payload.document_types) ? payload.document_types : [],
    storage_paths: Array.isArray(payload.storage_paths) ? payload.storage_paths : [],
  };
}

async function loadPending(preferredId = selectedId) {
  const response = await fetch("/api/pending");
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.message || "Failed to load Pending queue.");
  }

  pendingItems = Array.isArray(payload.pending) ? payload.pending : [];
  if (preferredId && pendingItems.some((item) => item.id === preferredId)) {
    selectedId = preferredId;
  } else if (!selectedId || !pendingItems.some((item) => item.id === selectedId)) {
    selectedId = pendingItems[0]?.id || null;
  }
  render();
}

function metadataPayload() {
  const selectedTags = Array.from(tagsSelect.selectedOptions).map((option) => Number(option.value));
  const numberOrNull = (value) => value ? Number(value) : null;
  return {
    title: titleInput.value.trim() || null,
    document_date: dateInput.value || null,
    document_type_id: numberOrNull(typeSelect.value),
    correspondent_id: numberOrNull(correspondentSelect.value),
    storage_path_id: numberOrNull(storageSelect.value),
    tag_ids: selectedTags,
  };
}

async function saveMetadata() {
  const item = selectedItem();
  if (!item) return null;

  const response = await fetch(`/api/pending/${item.id}/metadata`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(metadataPayload()),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.message || "Failed to save metadata.");
  }

  const index = pendingItems.findIndex((entry) => entry.id === item.id);
  if (index >= 0) {
    pendingItems[index] = payload.pending;
  }
  render();
  return payload.pending;
}

async function submitSelected() {
  const item = selectedItem();
  if (!item) return;

  submitButton.disabled = true;
  setStatus("Saving metadata…");
  try {
    await saveMetadata();
    setStatus("Submitting to Paperless…");
    const response = await fetch(`/api/pending/${item.id}/submit`, {method: "POST"});
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload?.message || "Paperless submission failed.");
    }
    await loadPending(item.id);
    if (payload.paperless_task_id) {
      scheduleTaskPolling(payload.paperless_task_id);
    }
  } catch (error) {
    setStatus(error.message || "Submission failed.", true);
    await loadPending(item.id).catch(() => {});
  } finally {
    const refreshed = selectedItem();
    submitButton.disabled = refreshed?.state === "submitted" || refreshed?.state === "submitting";
  }
}

function stopTaskPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function scheduleTaskPolling(taskId) {
  stopTaskPolling();
  pollTimer = window.setTimeout(async () => {
    try {
      const response = await fetch(`/api/paperless/tasks/${encodeURIComponent(taskId)}`);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload?.message || "Task status lookup failed.");
      }
      await loadPending(selectedId);
      if (payload.task_status !== "SUCCESS" && payload.task_status !== "FAILURE") {
        scheduleTaskPolling(taskId);
      } else {
        stopTaskPolling();
      }
    } catch (error) {
      setStatus(error.message || "Task status lookup failed.", true);
      scheduleTaskPolling(taskId);
    }
  }, 2000);
}

async function createMetadata(kind) {
  const labels = {
    tags: "tag",
    correspondents: "correspondent",
    document_types: "document type",
  };
  const label = labels[kind];
  if (!label) return;

  const name = window.prompt(`New ${label} name:`);
  if (!name || !name.trim()) return;

  const response = await fetch(`/api/paperless/metadata/${kind}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: name.trim()}),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload?.message || `Failed to create ${label}.`);
  }

  await loadMetadata();
  renderSelected();

  const target = kind === "tags"
    ? tagsSelect
    : kind === "correspondents"
      ? correspondentSelect
      : typeSelect;
  const option = Array.from(target.options).find(
    (entry) => String(entry.value) === String(payload.item.id)
  );
  if (option) {
    option.selected = true;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    setStatus("Saving metadata…");
    await saveMetadata();
    setStatus("Metadata saved.");
  } catch (error) {
    setStatus(error.message || "Failed to save metadata.", true);
  }
});

submitButton.addEventListener("click", () => {
  void submitSelected();
});

refreshButton.addEventListener("click", async () => {
  try {
    await loadPending(selectedId);
  } catch (error) {
    setStatus(error.message || "Refresh failed.", true);
  }
});

previousButton.addEventListener("click", () => {
  const index = selectedIndex();
  if (index > 0) selectPending(pendingItems[index - 1].id);
});

nextButton.addEventListener("click", () => {
  const index = selectedIndex();
  if (index >= 0 && index < pendingItems.length - 1) {
    selectPending(pendingItems[index + 1].id);
  }
});

document.querySelectorAll(".create-metadata-button").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await createMetadata(button.dataset.kind);
      setStatus("Paperless metadata created.");
    } catch (error) {
      setStatus(error.message || "Failed to create metadata.", true);
    }
  });
});

(async () => {
  try {
    await Promise.all([loadMetadata(), loadPending()]);
    render();
  } catch (error) {
    setStatus(error.message || "Failed to load Pending review.", true);
  }
})();
