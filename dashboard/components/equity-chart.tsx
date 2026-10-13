"use client";

import { useEffect, useId, useMemo, useState, type KeyboardEvent, type PointerEvent } from "react";
import { ArrowDownRight, ArrowUpRight, Clock3, Minus } from "lucide-react";

type EquityPoint = { time: string; equity: number };
type ChartPoint = EquityPoint & { timestamp: number };
type Range = "1J" | "24J" | "7H" | "Semua";

const ranges: { label: Range; title: string; duration: number }[] = [
  { label: "1J", title: "1 jam terakhir", duration: 60 * 60 * 1000 },
  { label: "24J", title: "24 jam terakhir", duration: 24 * 60 * 60 * 1000 },
  { label: "7H", title: "7 hari terakhir", duration: 7 * 24 * 60 * 60 * 1000 },
  { label: "Semua", title: "Semua snapshot", duration: Infinity },
];

const money = new Intl.NumberFormat("id-ID", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const percentage = new Intl.NumberFormat("id-ID", { maximumFractionDigits: 2 });
const WIDTH = 800;
const HEIGHT = 220;
const PADDING = { left: 65, right: 18, top: 20, bottom: 34 };
const PLOT_WIDTH = WIDTH - PADDING.left - PADDING.right;
const PLOT_HEIGHT = HEIGHT - PADDING.top - PADDING.bottom;
const BASELINE = HEIGHT - PADDING.bottom;

function pointTime(timestamp: number, full = false, includeDate = false) {
  return new Intl.DateTimeFormat("id-ID", {
    ...(full || includeDate ? { day: "2-digit", month: "short" } as const : {}),
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

export function EquityChart({
  points,
  hidden = false,
  loading = false,
}: {
  points: EquityPoint[];
  hidden?: boolean;
  loading?: boolean;
}) {
  const gradientId = `equity-fill-${useId().replace(/:/g, "")}`;
  const [range, setRange] = useState<Range>("Semua");
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const validPoints = useMemo(() => {
    const unique = new Map<number, ChartPoint>();
    for (const point of points) {
      const timestamp = new Date(point.time).getTime();
      if (Number.isFinite(timestamp) && Number.isFinite(point.equity)) {
        unique.set(timestamp, { ...point, timestamp });
      }
    }
    return [...unique.values()].sort((a, b) => a.timestamp - b.timestamp);
  }, [points]);

  const duration = ranges.find((item) => item.label === range)!.duration;
  const cutoff = now - duration;
  const visiblePoints = validPoints.filter((point) => point.timestamp >= cutoff);
  const latest = validPoints.at(-1);
  const firstVisible = visiblePoints[0];
  const lastVisible = visiblePoints.at(-1);
  const hasChange = visiblePoints.length >= 2;
  const change = hasChange ? lastVisible!.equity - firstVisible.equity : null;
  const changePercent = hasChange && firstVisible.equity !== 0
    ? (change! / Math.abs(firstVisible.equity)) * 100
    : null;
  const isNegative = change !== null && change < 0;
  const ChangeIcon = change === 0 ? Minus : isNegative ? ArrowDownRight : ArrowUpRight;

  let lower = 0;
  let upper = 1;
  if (visiblePoints.length) {
    lower = visiblePoints.reduce((value, point) => Math.min(value, point.equity), Infinity);
    upper = visiblePoints.reduce((value, point) => Math.max(value, point.equity), -Infinity);
    const padding = Math.max((upper - lower) * 0.2, Math.abs(upper) * 0.005, 0.02);
    lower = lower >= 0 ? Math.max(0, lower - padding) : lower - padding;
    upper += padding;
  }

  const startTime = firstVisible?.timestamp ?? 0;
  const endTime = lastVisible?.timestamp ?? 1;
  const xAt = (timestamp: number) => endTime === startTime
    ? PADDING.left + PLOT_WIDTH / 2
    : PADDING.left + ((timestamp - startTime) / (endTime - startTime)) * PLOT_WIDTH;
  const yAt = (equity: number) => PADDING.top + (1 - (equity - lower) / (upper - lower)) * PLOT_HEIGHT;
  const line = visiblePoints.map((point, index) => `${index ? "L" : "M"} ${xAt(point.timestamp)} ${yAt(point.equity)}`).join(" ");
  const area = hasChange ? `${line} L ${xAt(endTime)} ${BASELINE} L ${xAt(startTime)} ${BASELINE} Z` : "";
  const selectedIndex = activeIndex === null || !visiblePoints.length
    ? null
    : Math.min(activeIndex, visiblePoints.length - 1);
  const selected = selectedIndex === null ? null : visiblePoints[selectedIndex];
  const selectedX = selected ? xAt(selected.timestamp) : 0;
  const selectedY = selected ? yAt(selected.equity) : 0;
  const dateAxis = endTime - startTime > 24 * 60 * 60 * 1000;
  const xLabelCount = Math.min(3, visiblePoints.length);
  const xIndices = [...new Set(Array.from({ length: xLabelCount }, (_, index) =>
    Math.round(index * (visiblePoints.length - 1) / Math.max(1, xLabelCount - 1)),
  ))];

  function inspectPointer(event: PointerEvent<HTMLDivElement>) {
    if (!visiblePoints.length) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - bounds.left) / bounds.width) * WIDTH;
    let nearestIndex = 0;
    let nearestDistance = Infinity;
    visiblePoints.forEach((point, index) => {
      const distance = Math.abs(xAt(point.timestamp) - x);
      if (distance < nearestDistance) {
        nearestIndex = index;
        nearestDistance = distance;
      }
    });
    setActiveIndex(nearestIndex);
  }

  function inspectKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (!visiblePoints.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = selectedIndex ?? visiblePoints.length - 1;
    if (event.key === "Home") setActiveIndex(0);
    if (event.key === "End") setActiveIndex(visiblePoints.length - 1);
    if (event.key === "ArrowLeft") setActiveIndex(Math.max(0, current - 1));
    if (event.key === "ArrowRight") setActiveIndex(Math.min(visiblePoints.length - 1, current + 1));
  }

  return (
    <section className="min-w-0 rounded-xl border border-[#233133] bg-[#11191b] p-5 sm:p-6" aria-label="Perjalanan portofolio" aria-busy={loading}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-[15px] font-semibold tracking-[-0.02em] text-[#edf2ed]">Perjalanan portofolio</h2>
          <p className="mt-1 text-xs leading-relaxed text-[#859499]">Snapshot saldo sejak pemantauan dimulai</p>
        </div>
        <div className="flex gap-1 rounded-lg border border-[#263234] bg-[#0d1416] p-1" role="group" aria-label="Rentang waktu grafik">
          {ranges.map((item) => (
            <button
              key={item.label}
              type="button"
              title={item.title}
              aria-label={item.title}
              aria-pressed={range === item.label}
              onClick={() => { setRange(item.label); setActiveIndex(null); setNow(Date.now()); }}
              className={`rounded-md px-3 py-1.5 text-[11px] font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#b4f36d] ${range === item.label ? "bg-[#283629] text-[#c4f991]" : "text-[#859499] hover:bg-[#1b2628] hover:text-[#edf2ed]"}`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-6 flex min-h-14 flex-wrap items-start gap-x-4 gap-y-2">
        <div>
          <div className="text-[28px] leading-none font-semibold tracking-[-0.045em] text-[#f0f5ed] tabular-nums">
            {hidden ? "••••••" : latest ? money.format(latest.equity) : loading ? "Memuat…" : "—"}
          </div>
          <p className="mt-2 text-[10px] text-[#859499]">Saldo pada snapshot terakhir</p>
        </div>
        {hasChange && (
          <div className="pt-0.5">
            <span className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium tabular-nums ${hidden || change === 0 ? "bg-[#1e2a2c] text-[#a5b2b4]" : isNegative ? "bg-[#332320] text-[#f1a494]" : "bg-[#243224] text-[#b4f36d]"}`}>
              {!hidden && <ChangeIcon size={13} aria-hidden="true" />}
              {hidden ? "••••" : `${change! > 0 ? "+" : ""}${money.format(change!)}${changePercent === null ? "" : ` (${changePercent > 0 ? "+" : ""}${percentage.format(changePercent)}%)`}`}
            </span>
            <p className="mt-1.5 text-[10px] text-[#859499]">Perubahan saldo dalam rentang</p>
          </div>
        )}
      </div>

      {loading && !validPoints.length ? (
        <div className="mt-5 flex h-[220px] animate-pulse items-center justify-center rounded-lg border border-dashed border-[#283638] bg-[#0e1719] text-xs text-[#859499]" role="status">
          Mengambil snapshot saldo…
        </div>
      ) : !visiblePoints.length ? (
        <div className="mt-5 flex h-[220px] flex-col items-center justify-center rounded-lg border border-dashed border-[#283638] px-5 text-center">
          <span className="mb-3 rounded-xl border border-[#2b3a34] bg-[#1b2923] p-3 text-[#b4f36d]"><Clock3 size={21} aria-hidden="true" /></span>
          <p className="text-sm font-medium text-[#dce6de]">{validPoints.length ? "Belum ada snapshot di rentang ini" : "Belum ada riwayat saldo"}</p>
          <p className="mt-1.5 max-w-xs text-xs leading-5 text-[#859499]">{validPoints.length ? "Pilih rentang yang lebih panjang untuk melihat snapshot yang tersimpan." : "Grafik akan terisi dari saldo akun yang berhasil dibaca saat pemantauan berjalan."}</p>
          {validPoints.length > 0 && range !== "Semua" && <button type="button" className="mt-3 text-xs font-medium text-[#b4f36d] underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#b4f36d]" onClick={() => setRange("Semua")}>Lihat semua snapshot</button>}
        </div>
      ) : (
        <>
          <div
            className="relative mt-5 w-full rounded-lg outline-none focus-visible:ring-1 focus-visible:ring-[#b4f36d]/60"
            role="slider"
            tabIndex={0}
            aria-orientation="horizontal"
            aria-label="Snapshot saldo. Gunakan tombol panah kiri dan kanan untuk menjelajah."
            aria-valuemin={1}
            aria-valuemax={visiblePoints.length}
            aria-valuenow={(selectedIndex ?? visiblePoints.length - 1) + 1}
            aria-valuetext={`${pointTime((selected ?? lastVisible)!.timestamp, true)}: ${hidden ? "nilai disembunyikan" : money.format((selected ?? lastVisible)!.equity)}`}
            onPointerMove={inspectPointer}
            onPointerDown={inspectPointer}
            onPointerLeave={() => setActiveIndex(null)}
            onFocus={() => setActiveIndex(visiblePoints.length - 1)}
            onBlur={() => setActiveIndex(null)}
            onKeyDown={inspectKeyboard}
          >
            <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="block w-full overflow-visible" aria-hidden="true">
              <defs>
                <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#b4f36d" stopOpacity="0.18" />
                  <stop offset="100%" stopColor="#b4f36d" stopOpacity="0.005" />
                </linearGradient>
              </defs>
              {[0, 1, 2, 3].map((tick) => {
                const value = upper - (tick / 3) * (upper - lower);
                const y = PADDING.top + (tick / 3) * PLOT_HEIGHT;
                return (
                  <g key={tick}>
                    <line x1={PADDING.left} x2={WIDTH - PADDING.right} y1={y} y2={y} stroke="#253134" strokeDasharray="3 5" strokeWidth="1" />
                    <text x={PADDING.left - 11} y={y + 3.5} fill="#6f8084" fontSize="10" textAnchor="end" fontFamily="inherit">{hidden ? "•••" : `$${value.toFixed(2)}`}</text>
                  </g>
                );
              })}
              {area && <path d={area} fill={`url(#${gradientId})`} />}
              {hasChange && <path d={line} fill="none" stroke="#b4f36d" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />}
              {!hasChange && (
                <g>
                  <circle cx={xAt(startTime)} cy={yAt(firstVisible.equity)} r="12" fill="#b4f36d" fillOpacity="0.08" />
                  <circle cx={xAt(startTime)} cy={yAt(firstVisible.equity)} r="5" fill="#b4f36d" stroke="#11191b" strokeWidth="2" />
                </g>
              )}
              {xIndices.map((index) => {
                const point = visiblePoints[index];
                return <text key={point.timestamp} x={xAt(point.timestamp)} y={HEIGHT - 9} fill="#6f8084" fontSize="10" fontFamily="inherit" textAnchor={visiblePoints.length === 1 ? "middle" : index === 0 ? "start" : index === visiblePoints.length - 1 ? "end" : "middle"}>{pointTime(point.timestamp, false, dateAxis)}</text>;
              })}
              {selected && (
                <g>
                  <line x1={selectedX} x2={selectedX} y1={PADDING.top} y2={BASELINE} stroke="#68877c" strokeDasharray="3 4" />
                  <circle cx={selectedX} cy={selectedY} r="9" fill="#b4f36d" fillOpacity="0.12" />
                  <circle cx={selectedX} cy={selectedY} r="4" fill="#b4f36d" stroke="#11191b" strokeWidth="2" />
                </g>
              )}
            </svg>
            {selected && (
              <div className="pointer-events-none absolute top-0 z-10 w-[168px] rounded-lg border border-[#354640] bg-[#172321] px-3 py-2.5 shadow-lg" style={{ left: `clamp(0px, calc(${(selectedX / WIDTH) * 100}% - 84px), calc(100% - 168px))` }}>
                <p className="text-[10px] text-[#94a49f]">{pointTime(selected.timestamp, true)}</p>
                <p className="mt-1 text-sm font-semibold text-[#c4f991] tabular-nums">{hidden ? "••••••" : money.format(selected.equity)}</p>
              </div>
            )}
          </div>
          {visiblePoints.length === 1 && <p className="mt-1 text-center text-[11px] leading-5 text-[#859499]">{validPoints.length === 1 ? "Snapshot pertama tersimpan. Perubahan saldo terlihat setelah pembaruan berikutnya." : "Satu snapshot dalam rentang ini. Pilih rentang lebih panjang untuk melihat perubahan saldo."}</p>}
        </>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-[#233133] pt-3 text-[10px] text-[#859499]">
        <span className="inline-flex items-center gap-1.5"><span className="h-1.5 w-1.5 rounded-full bg-[#b4f36d]" />{visiblePoints.length} snapshot dalam rentang</span>
        <span>Setoran dan penarikan ikut mengubah saldo.</span>
      </div>
    </section>
  );
}
