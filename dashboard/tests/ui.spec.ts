import { test, expect } from "@playwright/test";

test("real account renders, positions search/details and privacy work", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Ringkasan portofolio." })).toBeVisible();
  await expect(page.getByText("Akun terhubung", {exact:true})).toBeVisible({timeout:65_000});
  await expect(page.locator(".metric-value").first()).not.toHaveText("—");
  await page.screenshot({ path: "test-results/dashboard-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "Sembunyikan saldo", exact:true }).click();
  await expect(page.locator(".metric-value").first()).toHaveText("••••••");
  await page.getByRole("button", { name: "Tampilkan saldo", exact:true }).click();
  await page.getByRole("button", { name: /^Posisi pasar/ }).click();
  const search = page.getByRole("textbox", { name: "Cari pasar" });
  await search.fill("zz-no-such-market-test");
  await expect(page.getByText("Pasar tidak ditemukan", {exact:true})).toBeVisible();
  await search.fill("");
  const details = page.getByRole("button", {name:/^Detail /});
  if (await details.count()) {
    await details.first().click();
    await expect(page.locator("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator("dialog")).not.toBeVisible();
    const download = page.waitForEvent("download");
    await page.getByRole("button",{name:"Ekspor CSV"}).click();
    expect((await download).suggestedFilename()).toMatch(/^polybot-live-.*\.csv$/);
  }
  await page.getByRole("button", {name:"Aktivitas",exact:true}).click();
  await expect(page.getByRole("heading", {name:"Riwayat transaksi bot"})).toBeVisible();
  await page.getByRole("button", {name:"Strategi & risiko",exact:true}).click();
  await expect(page.getByRole("heading", {name:"Status operasional"})).toBeVisible();
  expect(errors).toEqual([]);
});

test("mobile navigation, paper mode and preferences persist", async ({ page }) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto("/");
  await expect(page.getByText("Akun terhubung",{exact:true})).toBeVisible({timeout:65_000});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({path:"test-results/dashboard-mobile.png",fullPage:true});
  await page.getByRole("button",{name:"Buka menu"}).click();
  await page.getByRole("button",{name:"Preferensi",exact:true}).click();
  await page.getByRole("combobox",{name:"Interval pembaruan"}).selectOption("60");
  await page.getByRole("switch",{name:"Sembunyikan nilai uang"}).click();
  await page.reload();
  await page.getByRole("button",{name:"Buka menu"}).click();
  await page.getByRole("button",{name:"Preferensi",exact:true}).click();
  await expect(page.getByRole("combobox",{name:"Interval pembaruan"})).toHaveValue("60");
  await expect(page.getByRole("switch",{name:"Sembunyikan nilai uang"})).toHaveAttribute("aria-checked","true");
  await page.getByRole("combobox",{name:"Sumber data"}).selectOption("paper");
  await expect(page.getByText("Ledger simulasi terhubung",{exact:true})).toBeVisible({timeout:15_000});
  await page.screenshot({path:"test-results/dashboard-settings-mobile.png",fullPage:true});
});

test("API rejects external origins and returns a safe monitored schema", async ({ request }) => {
  const blocked = await request.get("/api/monitor",{headers:{origin:"https://evil.example","sec-fetch-site":"cross-site"}});
  expect(blocked.status()).toBe(403);
  expect((await request.get("/api/monitor?mode=invalid")).status()).toBe(400);
  const response = await request.get("/api/monitor?mode=live");
  expect(response.ok()).toBeTruthy();
  const text = await response.text();
  expect(text).not.toMatch(/POLYMARKET_PRIVATE_KEY|api_secret|passphrase|0x[a-fA-F0-9]{64}/);
  const data = JSON.parse(text);
  expect(data.mode).toBe("live");
  expect(data.account.connected).toBe(true);
  expect(Array.isArray(data.equityHistory)).toBe(true);
  expect(response.headers()["cache-control"]).toContain("no-store");
});
