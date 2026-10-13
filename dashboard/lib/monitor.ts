import "server-only";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import type { Mode, Snapshot } from "./types";

type Entry = { data?: Snapshot; expires: number; pending?: Promise<Snapshot> };
const globalState = globalThis as typeof globalThis & { polybotMonitorCache?: Map<Mode, Entry> };
const cache = globalState.polybotMonitorCache ??= new Map();

export async function readSnapshot(mode: Mode, refresh = false): Promise<Snapshot> {
  const prior = cache.get(mode);
  if (prior?.pending) return prior.pending;
  if (prior?.data && Date.now() < prior.expires && (!refresh || Date.now() < prior.expires - 15_000)) return prior.data;
  const root = path.resolve(process.cwd(), "..");
  const localPython = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const executable = existsSync(localPython) ? localPython : process.platform === "win32" ? "python" : "python3";
  const entry: Entry = { data: prior?.data, expires: 0 };
  entry.pending = new Promise<Snapshot>((resolve, reject) => {
    execFile(executable, ["-m", "polybot.monitor", "--mode", mode, "--output-json"],
      { cwd: root, timeout: 60_000, maxBuffer: 5 * 1024 * 1024, windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" } },
      (error, stdout) => {
        // Raw stderr and SDK exceptions may contain credentials; never return them.
        if (error) { reject(new Error("Pemantauan belum dapat terhubung. Periksa konfigurasi Python dan akun di komputer ini.")); return; }
        try {
          const data = JSON.parse(stdout) as Snapshot;
          if (data.mode !== mode || !data.account || !Array.isArray(data.positions) || !Array.isArray(data.errors)) throw new Error();
          resolve(data);
        } catch { reject(new Error("Format data pemantauan tidak valid.")); }
      });
  });
  cache.set(mode, entry);
  try {
    const snapshot = await entry.pending;
    entry.data = snapshot;
    entry.expires = Date.now() + 20_000;
    return snapshot;
  } catch (error) {
    if (entry.data) {
      return { ...entry.data, stale: true, errors: [...entry.data.errors,
        { code: "refresh_failed", message: "Pembaruan gagal. Data terakhir tetap ditampilkan dengan waktu aslinya." }] };
    }
    throw error;
  } finally { entry.pending = undefined; }
}
