"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDownLeft, ArrowDownRight, ArrowRight, ArrowUpRight, Bell, Bot, Check,
  CheckCheck, ChevronDown, ChevronRight, CircleHelp, Copy, Download, ExternalLink, Eye, EyeOff,
  Gauge, Globe2, Layers3, LayoutDashboard, LoaderCircle, Menu, MoreHorizontal, RefreshCw,
  Search, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Target, TrendingUp, Wallet, X,
  type LucideIcon } from "lucide-react";
import { EquityChart } from "./equity-chart";
import { csvCell } from "@/lib/security.mjs";
import type { Mode, Position, Snapshot } from "@/lib/types";

type Tab = "overview" | "positions" | "activity" | "strategy" | "settings";
const navigation: { id: Tab; label: string; icon: LucideIcon }[] = [
  { id: "overview", label: "Ringkasan", icon: LayoutDashboard },
  { id: "positions", label: "Posisi pasar", icon: Layers3 },
  { id: "activity", label: "Aktivitas", icon: Activity },
  { id: "strategy", label: "Strategi & risiko", icon: ShieldCheck },
];
const usd = (value: number | null | undefined, hidden = false, digits = 2) => hidden ? "••••••" :
  value == null || !Number.isFinite(value) ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
const numeral = (n: number | null | undefined) => n == null ? "—" : new Intl.NumberFormat("id-ID", { maximumFractionDigits: 2 }).format(n);
const timeLabel = (value: string | null | undefined) => !value ? "Belum tersedia" : new Date(value).toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const pct = (value: number | null | undefined) => value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
const shortWallet = (value: string | null | undefined) => value ? `${value.slice(0, 6)}...${value.slice(-4)}` : "Wallet lokal";
const reasonLabels: Record<string, string> = {
  low_volume: "Volume rendah", low_liquidity: "Likuiditas rendah", crypto_excluded: "Pasar crypto dikecualikan",
  outside_time_window: "Di luar horizon waktu", market_not_tradable: "Pasar tidak dapat ditradingkan",
  no_forecast: "Belum ada forecast", wide_or_crossed_spread: "Spread terlalu lebar",
  entry_below_exchange_minimum: "Di bawah minimum order", insufficient_evidence: "Bukti peluang belum cukup",
  profit_target_not_supported: "Target profit belum didukung", data_error: "Data tidak tersedia", stale_book: "Quote kedaluwarsa",
};
const stateLabel = (status: string) => ({ OPEN: "Aktif", REDEEMABLE: "Selesai", REDEEMABLE_LOST: "Selesai", CLOSED: "Ditutup", MERGEABLE: "Mergeable" })[status] ?? status;

export default function Dashboard() {
  const [tab, setTab] = useState<Tab>("overview");
  const [mode, setMode] = useState<Mode>("live");
  const [data, setData] = useState<Snapshot | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);
  const [interval, setIntervalSeconds] = useState(30);
  const [mobileNav, setMobileNav] = useState(false);
  const [notifications, setNotifications] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("value");
  const [selected, setSelected] = useState<Position | null>(null);
  const [copied, setCopied] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [loadedPrefs, setLoadedPrefs] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const requestSequence = useRef(0);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("polybot-monitor-preferences") ?? "{}");
      if (saved.mode === "paper" || saved.mode === "live") setMode(saved.mode);
      if (typeof saved.hidden === "boolean") setHidden(saved.hidden);
      if ([0, 30, 60, 120].includes(saved.interval)) setIntervalSeconds(saved.interval);
    } catch { /* Ignore malformed browser preferences. */ }
    setLoadedPrefs(true);
  }, []);
  useEffect(() => {
    if (loadedPrefs) localStorage.setItem("polybot-monitor-preferences", JSON.stringify({ mode, hidden, interval }));
  }, [mode, hidden, interval, loadedPrefs]);
  const fetchData = useCallback(async (force = false) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    const sequence = ++requestSequence.current;
    setRefreshing(true);
    const timeout = window.setTimeout(() => controller.abort(), 65_000);
    try {
      const response = await fetch(`/api/monitor?mode=${mode}${force ? "&refresh=1" : ""}`, { signal: controller.signal, cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error ?? "Tidak dapat membaca data akun.");
      if (sequence === requestSequence.current) { setData(payload); setError(null); }
    } catch (cause) {
      if (sequence === requestSequence.current && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Koneksi pemantauan terputus.");
      else if (sequence === requestSequence.current && controller.signal.aborted) setError("Koneksi membutuhkan waktu terlalu lama. Coba perbarui kembali.");
    } finally {
      window.clearTimeout(timeout);
      if (sequence === requestSequence.current) setRefreshing(false);
    }
  }, [mode]);
  useEffect(() => {
    if (!loadedPrefs) return;
    setData(null); setError(null);
    void fetchData();
    return () => { requestSequence.current++; abort.current?.abort(); };
  }, [fetchData, loadedPrefs]);
  useEffect(() => {
    if (!loadedPrefs || interval === 0) return;
    const timer = window.setInterval(() => { if (document.visibilityState === "visible" && !refreshing) void fetchData(); }, interval * 1000);
    return () => window.clearInterval(timer);
  }, [interval, fetchData, refreshing, loadedPrefs]);
  useEffect(() => { if (selected) dialog.current?.showModal(); else dialog.current?.close(); }, [selected]);
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 3500); return () => window.clearTimeout(timer); }, [toast]);

  const account = data?.account;
  const positions = useMemo(() => {
    return [...(data?.positions ?? [])].filter(p => {
      const matches = `${p.title} ${p.outcome} ${p.category}`.toLowerCase().includes(query.toLowerCase());
      const active = p.status === "OPEN";
      return matches && (filter === "all" || (filter === "active" ? active : !active));
    }).sort((a, b) => sort === "pnl" ? (b.pnl ?? -Infinity) - (a.pnl ?? -Infinity) : sort === "title" ? a.title.localeCompare(b.title) : (b.value ?? -Infinity) - (a.value ?? -Infinity));
  }, [data, query, filter, sort]);
  const alerts = [...(error ? [{ code: "connection", message: error }] : []), ...(data?.errors ?? [])];
  const connected = !!account?.connected && !error && !data?.stale;
  const totalEquity = account?.equity;
  const riskValue = (key: string, fallback = 0) => typeof data?.risk[key] === "number" ? data.risk[key] as number : fallback;
  const progress = Math.max(0, Math.min(100, ((totalEquity ?? 0) / riskValue("growthBalance", 30)) * 100));
  const exposure = totalEquity && account?.positionsValue != null ? account.positionsValue / totalEquity * 100 : 0;
  const title = ({overview:"Ringkasan portofolio",positions:"Posisi pasar",activity:"Aktivitas akun",strategy:"Strategi & risiko",settings:"Preferensi dashboard"})[tab];
  const subtitle = ({ overview: "Semua pergerakan akun Anda, dalam satu tempat.", positions: "Lihat detail setiap posisi yang terhubung ke akun Anda.", activity: "Transaksi yang tercatat pada ledger bot di komputer ini.", strategy: "Pantau aturan dan batas yang menjaga strategi tetap terukur.", settings: "Atur pengalaman pemantauan sesuai kebutuhan Anda." })[tab];

  function navigate(next: Tab) { setTab(next); setMobileNav(false); setNotifications(false); }
  async function copyWallet() {
    if (!account?.wallet) return;
    try { await navigator.clipboard.writeText(account.wallet); setCopied(true); window.setTimeout(() => setCopied(false), 2000); }
    catch { setToast("Salin alamat melalui detail akun di Polymarket."); }
  }
  function exportPositions() {
    if (!positions.length) { setToast("Belum ada posisi untuk diekspor."); return; }
    const rows = [["Pasar", "Outcome", "Shares", "Harga rata-rata", "Harga terakhir", "Nilai USD", "PnL USD", "Status"],
      ...positions.map(p => [p.title, p.outcome, p.size, p.avgPrice, p.currentPrice, p.value, p.pnl, p.status])];
    const blob = new Blob(["\ufeff" + rows.map(row => row.map(csvCell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob); const link = document.createElement("a");
    link.href = url; link.download = `polybot-${mode}-${new Date().toISOString().slice(0, 10)}.csv`; link.click(); URL.revokeObjectURL(url);
    setToast(`${positions.length} posisi berhasil diekspor.`);
  }

  const positionTable = (compact = false) => <section className="panel positions-panel">
    <div className="panel-heading"><div><div className="heading-line"><h2>Posisi akun</h2><span className="count-badge">{data?.positions.length ?? 0}</span></div><p>Semua kategori, termasuk posisi lama.</p></div>
      {compact ? <button className="text-button" onClick={() => navigate("positions")}>Lihat semua <ArrowUpRight size={15}/></button> : <button className="button button-subtle" onClick={exportPositions}><Download size={15}/> Ekspor CSV</button>}
    </div>
    <div className="table-toolbar"><div className="segmented" aria-label="Filter posisi">
      {[['all', 'Semua'], ['active', 'Aktif'], ['resolved', 'Selesai']].map(([value, label]) => <button key={value} aria-pressed={filter === value} className={filter === value ? "selected" : ""} onClick={() => setFilter(value)}>{label}</button>)}
    </div><div className="table-tools"><label className="search-box"><Search size={15}/><input placeholder="Cari pasar..." aria-label="Cari pasar" value={query} onChange={e => setQuery(e.target.value)}/></label>
      <label className="sort-control" title="Urutkan posisi"><SlidersHorizontal size={15}/><select aria-label="Urutkan posisi" value={sort} onChange={e => setSort(e.target.value)}><option value="value">Nilai</option><option value="pnl">PnL</option><option value="title">Nama</option></select></label></div></div>
    <div className="table-scroll"><table><thead><tr><th>Pasar / Outcome</th><th>Shares</th><th>Avg. / Terakhir</th><th>Nilai posisi</th><th>Profit / Loss</th><th>Status</th><th><span className="sr-only">Detail</span></th></tr></thead><tbody>
      {!data && refreshing ? [0,1,2,3].map(row => <tr key={row}>{[0,1,2,3,4,5,6].map(cell => <td key={cell}><span className="skeleton block h-5 w-full"/></td>)}</tr>) : (compact ? positions.slice(0,5) : positions).map(position => <tr key={position.id}>
        <td><button className="market-cell" onClick={() => setSelected(position)}><span className={`market-avatar ${position.category.toLowerCase().includes("crypto") ? "crypto" : ""}`}><Globe2 size={19}/></span><span><strong>{position.title}</strong><span className="market-outcome"><span className={`outcome-dot ${position.outcome.toLowerCase() === "no" || position.outcome.toLowerCase() === "down" ? "red" : ""}`}/>{position.outcome}<span className="inline-dot">·</span>{position.category.toLowerCase() === "unknown" ? "Pasar" : position.category}</span></span></button></td>
        <td className="numeric">{hidden ? "•••" : numeral(position.size)}</td><td className="numeric price-pair">{usd(position.avgPrice, hidden)}<span>{usd(position.currentPrice, hidden)}</span></td>
        <td className="numeric emphasis">{usd(position.value, hidden)}</td><td className={`numeric ${position.pnl != null && position.pnl < 0 ? "negative" : "positive"}`}>{usd(position.pnl, hidden)}<span className="pnl-percent">{hidden ? "•••" : pct(position.pnlPercent)}</span></td>
        <td><span className={`status-tag ${position.status === "OPEN" ? "active" : ""}`}><i/>{stateLabel(position.status)}</span></td><td><button className="icon-button" aria-label={`Detail ${position.title}`} onClick={() => setSelected(position)}><ChevronRight size={17}/></button></td>
      </tr>)}
    </tbody></table></div>
    {data && !positions.length && <Empty icon={Layers3} title={query ? "Pasar tidak ditemukan" : filter === "active" ? "Belum ada posisi aktif" : "Belum ada posisi"} description={query ? "Coba kata kunci atau filter yang lain." : "Posisi akan muncul di sini ketika tersedia pada sumber data yang dipilih."}/>}
    <div className="table-foot"><span>Menampilkan {Math.min(positions.length, compact ? 5 : positions.length)} dari {positions.length} posisi</span><span><span className="tiny-dot"/> Sumber: {mode === "live" ? "Polymarket" : "ledger simulasi"}</span></div>
  </section>;

  return <div className="app-shell">
    {mobileNav && <button className="nav-backdrop" aria-label="Tutup menu" onClick={() => setMobileNav(false)}/>}
    <aside className={`sidebar ${mobileNav ? "mobile-open" : ""}`}>
      <a className="brand" href="#" onClick={e => {e.preventDefault();navigate("overview");}}><span className="brand-symbol"><span/><span/><span/></span><span>poly<span className="brand-weight">bot</span><sup>®</sup></span></a>
      <div className="workspace"><span className="workspace-icon"><Wallet size={17}/></span><div><strong>Personal workspace</strong><span>Polymarket account</span></div><ChevronDown size={14} className="muted"/></div>
      <div className="nav-label">WORKSPACE</div>
      <nav>{navigation.map(({ id, label, icon: Icon }) => <button key={id} className={`nav-link ${tab === id ? "active" : ""}`} onClick={() => navigate(id)} aria-current={tab === id ? "page" : undefined}><Icon size={18}/><span>{label}</span>{id === "positions" && !!account?.positionsCount && <small>{account.positionsCount}</small>}</button>)}</nav>
      <div className="sidebar-divider"/><div className="nav-label">PERSONALISASI</div>
      <button className={`nav-link ${tab === "settings" ? "active" : ""}`} onClick={() => navigate("settings")}><Settings2 size={18}/><span>Preferensi</span></button>
      <div className="sidebar-bottom"><div className="observer-card"><span className="observer-icon"><ShieldCheck size={19}/></span><h3>Anda memegang kendali.</h3><p>Dashboard ini hanya memantau. Tidak ada transaksi dari halaman ini.</p><span className="observer-label"><span/> MODE PEMANTAUAN</span></div>
      <button className="profile" onClick={() => navigate("settings")}><span className="profile-avatar">PB</span><span><strong>Akun personal</strong><small>Local workspace</small></span><MoreHorizontal size={18}/></button></div>
    </aside>

    <div className="main-shell"><header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" aria-label="Buka menu" onClick={() => setMobileNav(true)}><Menu size={20}/></button><span className="desktop-only">Workspace</span><ChevronRight size={13} className="desktop-only"/><strong>{navigation.find(n => n.id === tab)?.label ?? "Preferensi"}</strong></div>
      <div className="topbar-right"><span className="local-badge"><span className="tiny-dot"/> Lokal</span><span className="topbar-divider"/><button className="icon-button" aria-label={hidden ? "Tampilkan saldo" : "Sembunyikan saldo"} onClick={() => setHidden(!hidden)}>{hidden ? <EyeOff size={18}/> : <Eye size={18}/>}</button><div className="notification-wrap"><button className={`icon-button ${notifications ? "is-active" : ""}`} aria-label="Notifikasi" aria-expanded={notifications} onClick={() => setNotifications(!notifications)}><Bell size={18}/>{alerts.length > 0 && <i className="notification-dot"/>}</button>
      {notifications && <div className="notification-popover"><h3>Notifikasi <span>{alerts.length}</span></h3>{alerts.length ? alerts.map((a,i) => <p key={`${a.code}-${i}`}><span className="warning-dot"/>{a.message}</p>) : <p><CheckCheck size={18} className="positive"/>Tidak ada pemberitahuan baru.</p>}<small>Terakhir diperbarui: {timeLabel(data?.generatedAt)}</small></div>}</div><button className="top-avatar" aria-label="Preferensi akun" onClick={() => navigate("settings")}>PB</button></div>
    </header>

    <main><div className="page-heading"><div><div className="eyebrow"><span/> PORTFOLIO MONITOR</div><h1>{title}<span className="heading-period">.</span></h1><p>{subtitle}</p></div><div className="page-actions"><div className="mode-switch" aria-label="Sumber akun"><button className={mode === "live" ? "selected" : ""} onClick={() => setMode("live")}><span/>Akun asli</button><button className={mode === "paper" ? "selected" : ""} onClick={() => setMode("paper")}>Paper</button></div><button className="button refresh-button" disabled={refreshing} onClick={() => void fetchData(true)}><RefreshCw size={15} className={refreshing ? "animate-spin" : ""}/><span>{refreshing ? "Memuat" : "Perbarui"}</span></button></div></div>
    <div className="connection-strip"><div><span className={`status-dot ${connected ? "online" : refreshing ? "loading" : "offline"}`}/><strong>{connected ? (mode === "live" ? "Akun terhubung" : "Ledger simulasi terhubung") : refreshing ? "Menghubungkan data akun..." : "Data belum terhubung"}</strong><span className="connection-separator">/</span><button className="wallet-address" onClick={copyWallet} disabled={!account?.wallet}>{mode === "live" ? shortWallet(account?.wallet) : "Paper trading"}{account?.wallet && (copied ? <Check size={12}/> : <Copy size={12}/>)}</button></div><span className="updated-label">{refreshing ? "Sinkronisasi berlangsung" : `Diperbarui ${timeLabel(data?.generatedAt)}`}</span></div>
    {alerts.length > 0 && <div className="notice" role="status"><CircleHelp size={17}/><div><strong>{data?.stale ? "Menampilkan data terakhir" : "Ada data yang belum tersedia"}</strong><span>{alerts.map(a => a.message).join(" ")}</span></div><button className="text-button" onClick={() => void fetchData(true)} disabled={refreshing}>Coba lagi <ArrowRight size={14}/></button></div>}
    {mode === "paper" && <div className="paper-notice"><Bot size={15}/>Mode simulasi — saldo dan transaksi berikut tidak menggunakan uang asli.</div>}

    {(tab === "overview" || tab === "positions") && <div className="stats-grid">
      <Metric title="Total ekuitas" value={usd(totalEquity, hidden)} icon={Wallet} primary note="Cash + nilai posisi saat ini" loading={!data && refreshing}/>
      <Metric title="Saldo tersedia" value={usd(account?.cash, hidden, 4)} icon={Gauge} note={`${account?.openOrdersCount ?? "—"} order terbuka`} loading={!data && refreshing}/>
      <Metric title="PnL posisi terlapor" value={usd(account?.totalPnl, hidden)} icon={TrendingUp} negative={(account?.totalPnl ?? 0) < 0} note="Realized + unrealized pada data terlapor" loading={!data && refreshing}/>
      <Metric title="Posisi aktif" value={account?.positionsComplete ? String(account.activePositionsCount) : "—"} icon={Layers3} note={`${account?.positionsComplete ? account.redeemablePositionsCount : "—"} posisi berstatus redeemable`} loading={!data && refreshing}/>
    </div>}

    {tab === "overview" && <>
      <div className="overview-grid"><EquityChart points={data?.equityHistory ?? []} hidden={hidden} loading={!data && refreshing}/><section className="panel capital-panel"><div className="panel-heading"><div><span className="mini-label">RENCANA MODAL</span><h2>Bertumbuh, terukur<span className="positive">.</span></h2></div><span className="panel-icon"><Target size={19}/></span></div><div className="capital-number">{usd(totalEquity, hidden)}<span>/ {usd(riskValue("growthBalance", 30), hidden)}</span></div><div className="progress-track"><span style={{width:`${progress}%`}}/></div><div className="progress-labels"><span>Equity saat ini</span><strong>{hidden ? "•••" : `${progress.toFixed(1)}%`}</strong></div>
      <div className="capital-phases"><div className="phase"><span className="phase-number current">01</span><div><strong>Bangun fondasi</strong><small>Entry awal {usd(riskValue("entryDollars", 1), hidden)}</small></div><span className="phase-current">Saat ini</span></div><div className="phase"><span className="phase-number">02</span><div><strong>Tingkatkan ukuran</strong><small>Entry {usd(riskValue("growthEntryDollars", 5), hidden)} dengan syarat lebih ketat</small></div><ShieldCheck size={15} className="muted"/></div></div><button className="capital-link" onClick={() => navigate("strategy")}>Lihat aturan strategi <ArrowUpRight size={16}/></button></section></div>
      {positionTable(true)}
      <div className="bottom-grid"><section className="panel"><div className="panel-heading"><div className="heading-line"><span className="panel-icon small"><Bot size={17}/></span><h2>Monitor bot</h2></div><span className={`status-tag ${data?.bot.stopped ? "warning" : ""}`}><i/>{data?.bot.stopped ? "Dijeda" : data?.bot.status === "halted" ? "Dibatasi" : data?.bot.status === "recent_scan" ? "Scan terbaru" : "Menunggu"}</span></div><div className="bot-summary"><div><small>Outcome dipindai</small><strong>{numeral(data?.bot.lastScan?.scanned)}</strong></div><div><small>Dianalisis</small><strong>{numeral(data?.bot.lastScan?.analyzed)}</strong></div><div><small>Order pending</small><strong>{numeral(data?.bot.pendingCount)}</strong></div></div><div className="panel-bottom"><span><span className="tiny-dot"/> Scan terakhir: {timeLabel(data?.bot.lastScanAt)}</span><button className="text-button" onClick={() => navigate("strategy")}>Detail <ArrowRight size={14}/></button></div></section>
      <section className="panel risk-overview"><div className="panel-heading"><div className="heading-line"><span className="panel-icon small"><ShieldCheck size={17}/></span><h2>Eksposur akun</h2></div><span className="mini-label">NILAI PASAR</span></div><div className="exposure-value"><strong>{hidden ? "•••" : `${exposure.toFixed(1)}%`}</strong><span>dari ekuitas berada dalam posisi</span></div><div className="progress-track exposure"><span style={{width:`${Math.max(0,Math.min(100,exposure))}%`}}/></div><div className="panel-bottom"><span>Batas entry bot {Math.round(riskValue("maxExposureFraction", .4) * 100)}% terhadap biaya posisi</span><ShieldCheck size={15} className="positive"/></div></section></div>
    </>}
    {tab === "positions" && positionTable()}

    {tab === "activity" && <section className="panel"><div className="panel-heading"><div><h2>Riwayat transaksi bot</h2><p>Order yang sudah dikonfirmasi dan dicatat pada ledger {mode}.</p></div><span className="count-badge">{data?.trades.length ?? 0}</span></div>
      {data?.trades.length ? <div className="activity-list">{data.trades.map(trade => <div className="activity-row" key={trade.id}><span className={`activity-symbol ${trade.side === "BUY" ? "buy" : "sell"}`}>{trade.side === "BUY" ? <ArrowDownLeft size={21}/> : <ArrowUpRight size={21}/>}</span><div className="activity-info"><strong>{trade.title || "Transaksi posisi"}</strong><span>{trade.side === "BUY" ? "Beli" : "Jual"} · {numeral(trade.size)} shares · {timeLabel(trade.time)}</span></div><div className="activity-amount"><strong>{usd(trade.cash, hidden)}</strong><span className={(trade.pnl ?? 0) < 0 ? "negative" : "positive"}>PnL {usd(trade.pnl, hidden)}</span></div><span className="status-tag active"><i/>Terkonfirmasi</span></div>)}</div> : <Empty icon={Activity} title="Riwayat bot masih kosong" description="Transaksi manual di Polymarket tidak masuk ke ledger bot. Halaman ini akan terisi setelah bot mencatat transaksi terkonfirmasi."/>}
      <div className="table-foot"><span>Sumber: ledger lokal · bukan seluruh riwayat akun Polymarket</span><ShieldCheck size={14}/></div></section>}

    {tab === "strategy" && <><section className="strategy-banner"><span className="strategy-banner-icon"><ShieldCheck size={28}/></span><div><span className="mini-label">STRATEGI AKTIF PADA KONFIGURASI</span><h2>Peluang dipilih. Risiko dibatasi.</h2><p>Pasar non-crypto, ukuran posisi terukur, dan exit berdasarkan harga yang dapat dieksekusi.</p></div><span className="status-tag"><i/>Hanya pantau</span></section>
      <div className="risk-grid">{[
        ["Entry awal",usd(riskValue("entryDollars",1),hidden),"Batas biaya masuk per posisi",Wallet],
        ["Target profit",`${Math.round(riskValue("takeProfitRoi",.5)*100)}%`, `Minimum ${usd(riskValue("minimumProfit",.5),hidden)} setelah biaya`,TrendingUp],
        ["Stop-loss",`${Math.round(riskValue("stopLossFraction",.2)*100)}%`,"Trigger exit; harga jual tidak dijamin",ArrowDownRight],
        ["Batas rugi harian",usd(riskValue("dailyLossLimit",1),hidden),"Menghentikan entry baru",ShieldCheck],
        ["Posisi maksimum",String(riskValue("maxOpenPositions",3)),"Maksimum satu posisi per event",Layers3],
        ["Cadangan cash",usd(riskValue("reserveCash",2),hidden),"Saldo minimum yang dipertahankan",Gauge],
      ].map(([label,value,note,Icon]) => <Metric key={String(label)} title={String(label)} value={String(value)} note={String(note)} icon={Icon as LucideIcon}/>)}</div>
      <div className="bottom-grid"><section className="panel"><div className="panel-heading"><div><h2>Keputusan scan terakhir</h2><p>Alasan peluang belum memenuhi syarat entry.</p></div><Search size={18} className="muted"/></div><div className="rejections">{data?.bot.rejections.length ? [...data.bot.rejections].sort((a,b) => b.count-a.count).slice(0,8).map(item => <div key={item.reason}><span>{reasonLabels[item.reason] ?? item.reason.replaceAll("_"," ")}</span><strong>{numeral(item.count)}</strong></div>) : <Empty icon={Search} title="Belum ada hasil scan" description={`Belum ada catatan pemindaian pada ledger ${mode}.`}/>}</div></section>
      <section className="panel"><div className="panel-heading"><h2>Status operasional</h2><Bot size={18} className="muted"/></div><dl className="detail-list"><div><dt>Ledger lokal</dt><dd>{data?.bot.ledgerAvailable ? "Tersedia" : "Belum tersedia"}</dd></div><div><dt>Penghentian manual</dt><dd>{data?.bot.stopped ? "Aktif" : "Tidak aktif"}</dd></div><div><dt>Order belum selesai</dt><dd>{data?.bot.pendingCount ?? "—"}</dd></div><div><dt>Scan terakhir</dt><dd>{timeLabel(data?.bot.lastScanAt)}</dd></div><div><dt>Batas drawdown</dt><dd>{Math.round(riskValue("maxDrawdownFraction",.25)*100)}%</dd></div><div><dt>Horizon maksimum</dt><dd>{riskValue("maxHorizonHours",720)/24} hari</dd></div></dl><p className="panel-disclaimer">Status pemantauan tidak membuktikan proses trading sedang berjalan. Aturan strategi dibaca dari config.toml.</p></section></div></>}

    {tab === "settings" && <div className="settings-grid"><section className="panel"><div className="panel-heading"><div><h2>Tampilan & pembaruan</h2><p>Preferensi tersimpan di browser ini.</p></div><Settings2 size={20} className="muted"/></div><div className="setting-row"><div><strong>Pembaruan otomatis</strong><p>Data diperbarui saat tab browser aktif.</p></div><select className="select-input" aria-label="Interval pembaruan" value={interval} onChange={e => setIntervalSeconds(Number(e.target.value))}><option value={30}>30 detik</option><option value={60}>1 menit</option><option value={120}>2 menit</option><option value={0}>Manual</option></select></div><div className="setting-row"><div><strong>Sembunyikan nilai uang</strong><p>Samarkan saldo, harga, dan profit/loss.</p></div><button role="switch" aria-label="Sembunyikan nilai uang" aria-checked={hidden} className={`toggle ${hidden ? "on" : ""}`} onClick={() => setHidden(!hidden)}><span/></button></div><div className="setting-row"><div><strong>Sumber data</strong><p>Akun asli dan ledger paper disimpan terpisah.</p></div><select className="select-input" aria-label="Sumber data" value={mode} onChange={e => setMode(e.target.value as Mode)}><option value="live">Akun asli</option><option value="paper">Paper trading</option></select></div></section>
      <section className="panel account-panel"><span className="large-panel-icon"><ShieldCheck size={26}/></span><h2>Privat di komputer Anda.</h2><p>Kredensial dibaca oleh layanan lokal. Private key tidak dikirim ke halaman web dan tidak disimpan di browser.</p><div className="account-address"><small>WALLET AKUN</small><button onClick={copyWallet} disabled={!account?.wallet}>{shortWallet(account?.wallet)}{copied ? <Check size={15}/> : <Copy size={15}/>}</button></div><a className="button button-subtle full-width" href="https://polymarket.com" target="_blank" rel="noopener noreferrer">Buka Polymarket <ExternalLink size={15}/></a><p className="small-note">Dashboard ini tidak memiliki tombol beli, jual, deposit, atau pengubahan konfigurasi strategi.</p></section>
      <section className="panel info-panel"><div className="heading-line"><CircleHelp size={20} className="positive"/><h2>Tentang data Anda</h2></div><p>Grafik ekuitas mulai direkam saat dashboard mengambil snapshot. Nilai posisi mengikuti sumber data dan bukan jaminan harga eksekusi. PnL posisi terlapor tidak selalu sama dengan laba/rugi seluruh riwayat akun.</p><p>Untuk pengaturan bot dan panduan menjalankan, buka README.md di folder PolyBOT. Riwayat pemantauan berada di data/monitor.sqlite3.</p></section></div>}

    <footer className="page-footer"><span><ShieldCheck size={13}/> Pemantauan saja. Setiap keputusan trading tetap memiliki risiko.</span><span>PolyBOT <span className="inline-dot">·</span> v1.0 <span className="footer-spark">✳</span></span></footer></main>
    </div>

    <dialog ref={dialog} className="position-dialog" onCancel={() => setSelected(null)} onClick={e => {if(e.target === dialog.current) setSelected(null);}}>
      {selected && <div><div className="dialog-top"><span className="mini-label">DETAIL POSISI</span><button className="icon-button" aria-label="Tutup detail posisi" onClick={() => setSelected(null)} autoFocus><X size={20}/></button></div><span className="market-avatar large"><Globe2 size={25}/></span><h2>{selected.title}</h2><div className="dialog-badges"><span className="status-tag active">{selected.outcome}</span><span className="status-tag">{stateLabel(selected.status)}</span></div><dl className="detail-list"><div><dt>Jumlah shares</dt><dd>{hidden ? "•••" : numeral(selected.size)}</dd></div><div><dt>Harga rata-rata</dt><dd>{usd(selected.avgPrice,hidden,4)}</dd></div><div><dt>Harga terakhir</dt><dd>{usd(selected.currentPrice,hidden,4)}</dd></div><div><dt>Biaya posisi</dt><dd>{usd(selected.cost,hidden)}</dd></div><div><dt>Nilai saat ini</dt><dd>{usd(selected.value,hidden)}</dd></div><div><dt>Profit / Loss</dt><dd className={(selected.pnl ?? 0) < 0 ? "negative" : "positive"}>{usd(selected.pnl,hidden)} ({hidden ? "•••" : pct(selected.pnlPercent)})</dd></div></dl>{selected.redeemable && <p className="dialog-note">Sumber data menandai posisi ini redeemable. Status ini tidak menjamin ada pembayaran; hasil kalah dapat bernilai $0.</p>}{(selected.eventSlug || selected.slug) && <a className="button button-accent full-width" href={`https://polymarket.com/event/${encodeURIComponent(selected.eventSlug || selected.slug || "")}`} target="_blank" rel="noopener noreferrer">Lihat pasar di Polymarket <ExternalLink size={15}/></a>}</div>}
    </dialog>
    {toast && <div className="toast" role="status"><Check size={16}/>{toast}</div>}
  </div>;
}

function Metric({title,value,icon:Icon,note,primary=false,negative=false,loading=false}: {title:string;value:string;icon:LucideIcon;note:string;primary?:boolean;negative?:boolean;loading?:boolean}) {
  return <section className={`metric ${primary ? "metric-primary" : ""}`}><div className="metric-heading"><span>{title}</span><Icon size={17}/></div>{loading ? <div className="skeleton metric-skeleton"/> : <strong className={`metric-value ${negative ? "negative" : ""}`}>{value}</strong>}<div className="metric-note">{primary && <span className="tiny-dot"/>}{note}</div>{primary && <span className="metric-orbit" aria-hidden="true"/>}</section>;
}

function Empty({icon:Icon,title,description}: {icon:LucideIcon;title:string;description:string}) {
  return <div className="empty-state"><span><Icon size={24}/></span><h3>{title}</h3><p>{description}</p></div>;
}
