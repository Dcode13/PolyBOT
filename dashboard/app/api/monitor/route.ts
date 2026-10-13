import { NextRequest, NextResponse } from "next/server";
import { readSnapshot } from "@/lib/monitor";
import { isLocalRequest, validMode } from "@/lib/security.mjs";
import type { Mode } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const headers = { "Cache-Control": "no-store, private", "Vary": "Origin" };
  if (!isLocalRequest(request)) return NextResponse.json({ error: "Akses hanya tersedia dari komputer lokal." }, { status: 403, headers });
  const mode = request.nextUrl.searchParams.get("mode") ?? "live";
  if (!validMode(mode)) return NextResponse.json({ error: "Mode tidak valid." }, { status: 400, headers });
  try {
    return NextResponse.json(await readSnapshot(mode as Mode, request.nextUrl.searchParams.get("refresh") === "1"), { headers });
  } catch {
    return NextResponse.json({ error: "Data akun belum tersedia. Pastikan Python dan konfigurasi akun sudah siap, lalu coba lagi." }, { status: 503, headers });
  }
}
