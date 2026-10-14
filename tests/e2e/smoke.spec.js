const { test, expect } = require("@playwright/test");
const http = require("http");
const { promises: fs } = require("fs");

const modeFilePath = process.env.SCANEXPRESS_FAKE_SCAN_MODE_FILE;
const e2eDataDir = process.env.SCANEXPRESS_DATA_DIR;

if (!modeFilePath) {
  throw new Error("SCANEXPRESS_FAKE_SCAN_MODE_FILE must be set for e2e tests.");
}
if (!e2eDataDir) {
  throw new Error("SCANEXPRESS_DATA_DIR must be set for e2e tests.");
}

let fakePaperlessServer;
let paperlessUploadDelayMs = 0;

test.beforeAll(async () => {
  fakePaperlessServer = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1:18089");
    const metadataLists = {
      "/api/tags/": [
        { id: 1, name: "Personal" },
        { id: 2, name: "Education" },
      ],
      "/api/correspondents/": [
        { id: 21, name: "School" },
      ],
      "/api/document_types/": [
        { id: 11, name: "Certificate" },
      ],
      "/api/storage_paths/": [
        { id: 31, name: "Personal documents" },
      ],
    };

    if (req.method === "GET" && metadataLists[url.pathname]) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ results: metadataLists[url.pathname], next: null }));
      return;
    }

    if (
      req.method === "POST"
      && ["/api/tags/", "/api/correspondents/", "/api/document_types/"].includes(url.pathname)
    ) {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        const payload = body ? JSON.parse(body) : {};
        res.writeHead(201, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ id: 99, name: payload.name || "Created" }));
      });
      return;
    }

    if (req.method === "POST" && url.pathname === "/api/documents/post_document/") {
      req.on("data", () => {});
      req.on("end", () => {
        setTimeout(() => {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ id: 4242 }));
        }, paperlessUploadDelayMs);
      });
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "not found" }));
  });

  await new Promise((resolve, reject) => {
    fakePaperlessServer.once("error", reject);
    fakePaperlessServer.listen(18089, "127.0.0.1", resolve);
  });
});

test.afterAll(async () => {
  if (!fakePaperlessServer) return;

  await new Promise((resolve, reject) => {
    fakePaperlessServer.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
});

test.beforeEach(async () => {
  paperlessUploadDelayMs = 0;
  await fs.rm(e2eDataDir, { recursive: true, force: true });
  await fs.writeFile(modeFilePath, "success", "utf-8");
});

test("loads page with idle state", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "ScanExpress" })).toBeVisible();
  const filenameInput = page.getByLabel("Filename");
  await expect(filenameInput).toBeVisible();
  await expect(filenameInput).toHaveValue(/^scan_[0-9A-Za-z]+$/);
  await expect(page.getByText("Device configuration")).toBeVisible();
  await expect(page.locator('input[name="deviceConfig"]')).toHaveCount(2);
  await expect(page.getByRole("button", { name: "Start Scan" })).toBeVisible();
  await expect(page.locator("#statusText")).toHaveText("Status: idle");
});

test("shows username in header and exposes logout action", async ({ page }) => {
  let logoutCalled = false;

  await page.route("**/auth/logout", async (route) => {
    logoutCalled = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "ok" }),
    });
  });

  await page.goto("/");

  await expect(page.locator("#headerUsername")).toHaveText("e2e");
  const accountMenuButton = page.locator("#accountMenuButton");
  await expect(accountMenuButton).toBeVisible();
  await expect(page.locator("#accountMenuList")).toBeHidden();

  await accountMenuButton.click();

  const logoutMenuItem = page.getByRole("menuitem", { name: "Log out" });
  await expect(logoutMenuItem).toBeVisible();
  await logoutMenuItem.click();

  await expect.poll(() => logoutCalled).toBeTruthy();
});

test("filename input click does not auto-select full content", async ({ page }) => {
  await page.goto("/");

  const filenameInput = page.getByLabel("Filename");
  await filenameInput.click();

  const selection = await filenameInput.evaluate((element) => ({
    start: element.selectionStart,
    end: element.selectionEnd,
    length: element.value.length,
  }));
  expect(selection.start).toBe(selection.end);
  expect(selection.end).toBeLessThanOrEqual(selection.length);
});

test("filename input shows error style when empty filename is submitted", async ({ page }) => {
  await page.goto("/");

  const filenameInput = page.getByLabel("Filename");
  await filenameInput.fill("   ");
  await page.getByRole("button", { name: "Start Scan" }).click();

  await expect(filenameInput).toHaveClass(/input-error/);
  await expect(page.locator("#statusText")).toHaveText("Status: error (Filename cannot be empty)");
});

test("scan controls appear above device selection", async ({ page }) => {
  await page.goto("/");

  const scanButton = page.getByRole("button", { name: "Start Scan" });
  const deviceLegend = page.getByText("Device configuration", { exact: true });

  const scanBox = await scanButton.boundingBox();
  const deviceBox = await deviceLegend.boundingBox();

  expect(scanBox).not.toBeNull();
  expect(deviceBox).not.toBeNull();
  expect(scanBox.y).toBeLessThan(deviceBox.y);
});

test("device configuration selector shows available devices and selected details", async ({ page }) => {
  await page.goto("/");

  const fakeRadio = page.locator('input[name="deviceConfig"][value="fake"]');
  const flatbedRadio = page.locator('input[name="deviceConfig"][value="flatbed"]');
  await expect(fakeRadio).toBeChecked();
  await expect(flatbedRadio).not.toBeChecked();

  const detailsPanel = page.locator("#deviceDetailsPanel");
  await expect(detailsPanel).not.toHaveAttribute("open", "");

  await page.locator("#deviceDetailsSummary").click();
  await expect(detailsPanel).toHaveAttribute("open", "");
  await expect(page.locator("#deviceDetails")).toContainText("fake-device");
  await expect(page.locator("#deviceDetails")).toContainText("Automatic Document Feeder");

  await flatbedRadio.check();

  await expect(flatbedRadio).toBeChecked();
  await expect(page.locator("#deviceDetails")).toContainText("flatbed-device");
  await expect(page.locator("#deviceDetails")).toContainText("Flatbed");
  await expect(page.locator("#statusText")).toHaveText("Status: selected (flatbed)");
});

test("hash device deep link preselects configuration", async ({ page }) => {
  await page.goto("/#device=flatbed");

  const fakeRadio = page.locator('input[name="deviceConfig"][value="fake"]');
  const flatbedRadio = page.locator('input[name="deviceConfig"][value="flatbed"]');
  await expect(fakeRadio).not.toBeChecked();
  await expect(flatbedRadio).toBeChecked();
});

test("clicking Start Scan runs backend with fake scanner and fake Paperless", async ({ page }) => {
  await page.goto("/");
  const filenameInput = page.getByLabel("Filename");
  const filenameBefore = await filenameInput.inputValue();
  await page.getByRole("button", { name: "Start Scan" }).click();

  await expect(page.locator("#statusText")).toHaveText(/^Status: ok \(.+\)$/);
  await expect(page.locator("#statusStats")).toHaveText(
    /^Total: \d+s\nScan: \d+s\nPaperless: \d+s\nScan\/page: \d+s\nPaperless\/page: \d+s$/
  );
  const filenameAfter = await filenameInput.inputValue();
  expect(filenameAfter).toMatch(/^scan_[0-9A-Za-z]+$/);
  expect(filenameAfter).not.toBe(filenameBefore);
});

test("clicking Start Scan surfaces backend error when fake scanner fails", async ({ page }) => {
  await fs.writeFile(modeFilePath, "fail", "utf-8");

  await page.goto("/");
  await page.getByRole("button", { name: "Start Scan" }).click();

  await expect(page.locator("#statusText")).toHaveText(/^Status: error \(.+\)$/);
});

test("clicking Start Scan preserves multipage TIFF as multipage PDF", async ({ page }) => {
  await fs.writeFile(modeFilePath, "adf", "utf-8");

  await page.goto("/");
  await page.getByRole("button", { name: "Start Scan" }).click();

  await expect(page.locator("#statusText")).toHaveText(/Status: ok \(.+pages=3.+\)/);
});

test("reload while busy keeps Start Scan disabled", async ({ page }) => {
  await page.route("**/api/scan/status**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        in_progress: true,
        username: "test",
        device_name: "fake",
        device_id: "fake-device",
        scanimage_device_name: "fake-device",
        device_lock_id: "fake-device",
      }),
    });
  });

  await page.goto("/");

  const scanButton = page.getByRole("button", { name: "Start Scan" });
  await expect(scanButton).toBeDisabled();

  await page.reload();
  await expect(scanButton).toBeDisabled();
});

test("switching device while scan is running enables Start Scan for other device", async ({ page }) => {
  await fs.writeFile(modeFilePath, "slow", "utf-8");

  await page.goto("/");

  const scanButton = page.getByRole("button", { name: "Start Scan" });
  const flatbedRadio = page.locator('input[name="deviceConfig"][value="flatbed"]');

  await scanButton.click();
  await expect(scanButton).toBeDisabled();

  await flatbedRadio.check();
  await expect(flatbedRadio).toBeChecked();
  await expect(scanButton).toBeEnabled();

  await expect(page.locator("#statusText")).toHaveText(/^Status: ok \(.+\)$/);
  await expect(flatbedRadio).toBeChecked();
  await expect(scanButton).toBeEnabled();
});

test("scan is saved as Pending, reviewed, and explicitly submitted", async ({ page }) => {
  await page.goto("/");
  await page.locator('input[name="deviceConfig"][value="flatbed"]').check();
  await page.getByLabel("Filename").fill("school-certificate");
  await page.getByRole("button", { name: "Start Scan" }).click();

  await expect(page.locator("#statusText")).toContainText("Scan saved to Pending");

  await page.getByRole("link", { name: "Review Pending" }).click();
  await expect(page.getByRole("heading", { name: "Pending scans" })).toBeVisible();
  await expect(page.locator(".pending-queue-item")).toHaveCount(1);
  await expect(page.locator("#pendingPosition")).toHaveText("1 / 1");
  await expect(page.locator("#pendingStateBadge")).toHaveText("ready");
  await expect(page.locator("#pendingPreview")).toHaveAttribute("src", /\/api\/pending\/.+\/file/);

  await page.locator("#pendingTitle").fill("School Certificate - 2002");
  await page.locator("#pendingDocumentDate").fill("2002-06-10");
  await page.locator("#pendingDocumentType").selectOption("11");
  await page.locator("#pendingCorrespondent").selectOption("21");
  await page.locator("#pendingStoragePath").selectOption("31");
  await page.locator("#pendingTags").selectOption(["1", "2"]);

  await page.getByRole("button", { name: "Save metadata" }).click();
  await expect(page.locator("#pendingStatus")).toHaveText("Metadata saved.");

  await page.getByRole("button", { name: "Submit to Paperless" }).click();
  await expect(page.locator("#pendingStateBadge")).toHaveText("submitted");
  await expect(page.locator("#pendingStatus")).toContainText("Document ID: 4242");
});

test("recent uploads shows task polling transition and success document link", async ({ page }) => {
  let pollCount = 0;

  await page.route("**/api/scan/stream", async (route) => {
    const body =
      '{"status":"ok","message":"Scan uploaded to Paperless-ngx. pages=1","paperless_task_id":"task-1","device_name":"flatbed","complete":true}\n';
    await route.fulfill({
      status: 200,
      contentType: "application/x-ndjson",
      body,
    });
  });

  await page.route("**/api/paperless/tasks/task-1", async (route) => {
    pollCount += 1;
    if (pollCount < 2) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok",
          task_id: "task-1",
          task_status: "PENDING",
          related_document: null,
          result: null,
          date_done: null,
          task_file_name: "Offer.pdf",
        }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        task_id: "task-1",
        task_status: "SUCCESS",
        related_document: "21",
        result: "Success. New document id 21 created",
        date_done: "2026-02-24T14:33:09.254628+01:00",
        task_file_name: "Offer.pdf",
      }),
    });
  });

  await page.goto("/");
  await page.locator('input[name="deviceConfig"][value="flatbed"]').check();
  await page.getByRole("button", { name: "Start Scan" }).click();

  await expect(page.getByRole("heading", { name: "Recent uploads" })).toBeVisible();
  await expect(page.locator("#recentUploadsBody tr").first()).toContainText("SUCCESS");
  await expect(page.locator("#recentUploadsBody tr").first()).toContainText("flatbed");

  const successLink = page.locator("#recentUploadsBody tr a").first();
  await expect(successLink).toHaveAttribute("href", "http://127.0.0.1:18089/documents/21");
  await expect(successLink).toHaveAttribute("target", "_blank");
});

test("recent uploads records permanent upload timeout as failure", async ({ page }) => {
  await page.route("**/api/scan/stream", async (route) => {
    const body = [
      JSON.stringify({
        status: "uploading",
        message: "Uploading 1 page(s) to Paperless-ngx...",
        page_count: 1,
        paperless_timeout_seconds: 30,
      }),
      JSON.stringify({
        status: "error",
        message: "Paperless upload request failed: HTTPSConnectionPool(host='paperless.cloud.zonny.de', port=43443): Read timed out",
        device_name: "flatbed",
        complete: true,
      }),
    ].join("\n");

    await route.fulfill({
      status: 200,
      contentType: "application/x-ndjson",
      body: `${body}\n`,
    });
  });

  await page.goto("/");
  await page.locator('input[name="deviceConfig"][value="flatbed"]').check();
  await page.getByRole("button", { name: "Start Scan" }).click();

  const firstRow = page.locator("#recentUploadsBody tr").first();
  await expect(firstRow).toContainText("FAILURE");
  await expect(firstRow).toContainText("Read timed out");
  await expect(firstRow).toContainText("flatbed");
  await expect(firstRow.locator("a")).toHaveCount(0);
});

test("recent uploads keeps max 10 entries in newest-first order", async ({ page }) => {
  let sequence = 0;

  await page.route("**/api/scan/stream", async (route) => {
    sequence += 1;
    const taskId = `task-${sequence}`;
    const body = JSON.stringify({
      status: "ok",
      message: `Scan uploaded to Paperless-ngx. pages=1 ${taskId}`,
      paperless_task_id: taskId,
      complete: true,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/x-ndjson",
      body: `${body}\n`,
    });
  });

  await page.route("**/api/paperless/tasks/*", async (route) => {
    const taskId = route.request().url().split("/").pop();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        task_id: taskId,
        task_status: "SUCCESS",
        related_document: null,
        result: "done",
        date_done: "2026-02-24T14:33:09.254628+01:00",
        task_file_name: `${taskId}.pdf`,
      }),
    });
  });

  await page.goto("/");

  const scanButton = page.getByRole("button", { name: "Start Scan" });
  for (let index = 0; index < 11; index += 1) {
    await scanButton.click();
    await expect(page.locator("#statusText")).toHaveText(/^Status: ok \(.+\)$/);
  }

  const entries = page.locator("#recentUploadsBody tr");
  await expect(entries).toHaveCount(10);
  await expect(entries.first()).toContainText("task-11");
  await expect(entries.last()).toContainText("task-2");
});

test("recent uploads is restored from backend after page reload", async ({ page }) => {
  let recentUploadsCalls = 0;

  await page.route("**/api/recent-uploads", async (route) => {
    recentUploadsCalls += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        username: "test",
        recent_uploads: [
          {
            task_id: "task-server-1",
            submitted_at: Date.now() - 5000,
            device_name: "flatbed",
            file_name: "server-side.pdf",
            task_status: "SUCCESS",
            result_text: "Success. New document id 44 created",
            related_document: "44",
            is_polling: false,
            last_error: null,
            last_updated_at: Date.now() - 3000,
            poll_failure_count: 0,
          },
        ],
      }),
    });
  });

  await page.goto("/");

  const firstRow = page.locator("#recentUploadsBody tr").first();
  await expect(firstRow).toContainText("task-server-1");
  await expect(firstRow).toContainText("server-side.pdf");

  await page.reload();
  await expect(firstRow).toContainText("task-server-1");
  await expect(firstRow).toContainText("server-side.pdf");
  expect(recentUploadsCalls).toBeGreaterThanOrEqual(2);
});
