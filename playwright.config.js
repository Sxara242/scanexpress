const { defineConfig } = require("@playwright/test");

const fakeScanModeFile = "/tmp/scanexpress-fake-scan-mode.txt";
const e2eDataDir = "/tmp/scanexpress-e2e-data";
process.env.SCANEXPRESS_FAKE_SCAN_MODE_FILE = fakeScanModeFile;
process.env.SCANEXPRESS_DATA_DIR = e2eDataDir;

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:8000",
    headless: true,
  },
  webServer: {
    command: ".venv/bin/python app.py",
    url: "http://127.0.0.1:8000",
    reuseExistingServer: true,
    timeout: 60_000,
    env: {
      SCANEXPRESS_CONFIG_FILE: "tests/e2e/test_config.ini",
      SCANEXPRESS_FAKE_SCAN_MODE_FILE: fakeScanModeFile,
      SCANEXPRESS_DATA_DIR: e2eDataDir,
    },
  },
});
