import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "ui.spec.ts",
  timeout: 90_000,
  workers: 1,
  use: { baseURL: "http://127.0.0.1:3000", browserName: "chromium", channel: "chrome", headless: true,
    viewport: { width: 1440, height: 1100 }, screenshot: "only-on-failure" },
});
