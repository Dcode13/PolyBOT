"""Plain-text Telegram views for the monitoring snapshot, with no network I/O.

The caller must send these messages without Telegram's HTML/Markdown parse mode.
Only explicitly selected monitoring fields are rendered; SDK exceptions, worker
arguments, environment variables, and arbitrary error messages are never shown.
"""
from __future__ import annotations

import math
import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any


_CHART_LOCK = threading.Lock()
_EXPLORATORY_LABEL = "SIMULASI EKSPLORASI · probabilitas belum tersedia"
_ERROR_MESSAGES = {
    "paper_ledger_missing": "Ledger paper belum tersedia. Jalankan paper untuk memulai.",
    "positions_unavailable": "Posisi akun belum dapat dibaca secara lengkap.",
    "closed_history_unavailable": "Riwayat posisi tertutup belum lengkap; PnL bersifat parsial.",
    "balance_unavailable": "Saldo akun belum dapat dibaca.",
    "orders_unavailable": "Order terbuka belum dapat dibaca.",
    "connection_unavailable": "Koneksi akun belum tersedia. Periksa konfigurasi lokal.",
    "ledger_unavailable": "Ledger bot belum dapat dibaca.",
    "history_unavailable": "Riwayat pengamatan ekuitas belum tersedia.",
    "monitor_unavailable": "Pemantauan belum tersedia. Periksa proses lokal.",
}
_REJECTION_LABELS = {
    "entry_below_exchange_minimum": "Budget di bawah minimum order",
    "insufficient_evidence": "Bukti peluang belum cukup",
    "missing_forecast": "Forecast belum tersedia",
    "expired_forecast": "Forecast kedaluwarsa",
    "stale_orderbook": "Orderbook kedaluwarsa",
    "stale_book": "Orderbook kedaluwarsa",
    "data_error": "Data pasar belum tersedia",
    "crypto_excluded": "Pasar crypto dikecualikan",
    "no_forecast": "Forecast belum tersedia",
    "live_requires_external_calibrated_forecast": "Forecast eksternal terkalibrasi belum tersedia",
    "market_not_tradable": "Pasar belum dapat diperdagangkan",
    "missing_market_identity": "Identitas pasar belum lengkap",
    "missing_end_date": "Jadwal pasar belum tersedia",
    "outside_time_window": "Di luar horizon strategi",
    "low_liquidity": "Likuiditas terlalu rendah",
    "low_volume": "Volume terlalu rendah",
    "invalid_market": "Data pasar tidak valid",
    "empty_book": "Orderbook kosong",
    "wide_or_crossed_spread": "Selisih harga tidak memenuhi batas",
    "invalid_order_rules": "Aturan order tidak valid",
    "invalid_book": "Orderbook tidak valid",
    "insufficient_capital": "Modal tidak mencukupi",
    "invalid_inventory": "Data posisi tidak valid",
    "position_limit": "Batas jumlah posisi",
    "event_already_held": "Event sudah dimiliki",
    "unknown_fee": "Biaya belum tersedia",
    "forecast_horizon_invalid": "Horizon forecast tidak valid",
    "intraday_requires_target_touch_evidence": "Bukti mencapai target harian belum cukup",
    "long_position_limit": "Batas jumlah posisi jangka panjang",
    "position_risk_limit": "Batas risiko per posisi",
    "exposure_limit": "Batas total eksposur",
    "cash_reserve": "Cadangan kas harus dipertahankan",
    "long_capital_limit": "Batas modal jangka panjang",
    "invalid_tick": "Ukuran perubahan harga tidak valid",
    "insufficient_ask_depth": "Likuiditas beli belum cukup",
    "insufficient_bid_depth": "Likuiditas jual belum cukup",
    "profit_target_not_supported": "Bukti belum mendukung target profit",
    "edge_too_small": "Keunggulan peluang terlalu kecil",
}


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value: Any, limit: int = 100) -> str:
    if not isinstance(value, str):
        return "-"
    # Remove bidi/control characters and keep market titles on a single line.
    clean = " ".join("".join(" " if unicodedata.category(char).startswith("C") else char
                             for char in value).split())
    if not clean:
        return "-"
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


def format_trade_action(side: Any, outcome: Any) -> str:
    """Separate the selected outcome from the exchange's BUY/SELL operation."""
    if side not in ("BUY", "SELL"):
        return "-"
    label = _text(outcome, 28)
    if label == "-":
        return side
    action = "ENTRY" if side == "BUY" else "EXIT"
    canonical = {"yes": "Yes", "no": "No"}.get(label.casefold())
    if canonical:
        return f"{action} {canonical.upper()} ({side} {canonical})"
    return f"{action} {label} ({side})"


def _money(value: Any, *, signed: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "-"
    sign = "-" if number < 0 else ("+" if signed and number > 0 else "")
    return f"{sign}${abs(number):,.2f}"


def _count(value: Any) -> str:
    number = _number(value)
    return f"{number:,.0f}" if number is not None else "-"


def _shares(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "-"
    if 0 < abs(number) < 0.000001:
        return f"{number:.6g}"
    return f"{number:,.6f}".rstrip("0").rstrip(".")


def _percent(value: Any, *, fraction: bool = False) -> str:
    number = _number(value)
    return f"{number * (100 if fraction else 1):g}%" if number is not None else "-"


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _time(value: Any) -> str:
    stamp = _date(value)
    return stamp.strftime("%d %b %Y %H:%M:%S UTC") if stamp else "-"


def _mode(snapshot: dict) -> str:
    return "PAPER · simulasi" if snapshot.get("mode") == "paper" else "AKUN REAL"


def _scan_time(snapshot: dict) -> str:
    bot = _object(snapshot.get("bot"))
    raw = bot.get("lastScanAt")
    stamp, generated = _date(raw), _date(snapshot.get("generatedAt"))
    poll = _number(_object(snapshot.get("risk")).get("pollSeconds")) or 60
    stale = bool(stamp and generated and (generated - stamp).total_seconds() > max(180, poll * 3))
    return _time(raw) + (" (lama)" if stale else "")


def _errors(snapshot: dict) -> list[str]:
    errors = _rows(snapshot.get("errors"))
    if not errors:
        return []
    messages = []
    for row in errors:
        message = _ERROR_MESSAGES.get(str(row.get("code")), "Sebagian data pemantauan belum tersedia.")
        if message not in messages:
            messages.append(message)
    return ["", "Catatan data:", *[f"• {message}" for message in messages[:5]]]


def _summary(snapshot: dict) -> list[str]:
    account = _object(snapshot.get("account"))
    complete = account.get("positionsComplete") is True
    lines = [
        "RINGKASAN AKUN",
        f"Koneksi: {'tersedia' if account.get('connected') is True else 'belum tersedia'}",
        f"Ekuitas: {_money(account.get('equity'))}",
        f"Saldo tersedia: {_money(account.get('cash'))}",
        f"Nilai posisi: {_money(account.get('positionsValue'))}",
        f"PnL total {'paper' if snapshot.get('mode') == 'paper' else 'posisi terlapor'}: {_money(account.get('totalPnl'), signed=True)}",
        f"PnL terealisasi: {_money(account.get('realizedPnl'), signed=True)}",
        f"PnL belum terealisasi: {_money(account.get('unrealizedPnl'), signed=True)}",
        f"Posisi aktif: {_count(account.get('activePositionsCount')) if complete else '-'}",
        f"Posisi dapat diklaim: {_count(account.get('redeemablePositionsCount')) if complete else '-'}",
        f"Order terbuka: {_count(account.get('openOrdersCount'))}",
        f"Waktu valuasi: {_time(account.get('equityAsOf'))}",
        f"Scan terakhir: {_scan_time(snapshot)}",
    ]
    wallet = account.get("wallet")
    if isinstance(wallet, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        lines.append(f"Wallet: {wallet[:6]}…{wallet[-4:]}")
    if snapshot.get("mode") == "paper":
        lines.append("PnL paper dihitung sejak ledger simulasi dibuat; tidak memakai uang asli.")
    else:
        lines.append("PnL berasal dari posisi yang dilaporkan Polymarket, bukan seluruh arus kas wallet.")
        if account.get("historyComplete") is not True:
            lines.append("Riwayat posisi belum lengkap; PnL dapat bersifat parsial.")
    return lines


def _pagination(rows: list[dict], page: int, command: str, mode: str) -> tuple[list[dict], int, list[str]]:
    pages = max(1, math.ceil(len(rows) / 10))
    start = (page - 1) * 10
    if page > pages:
        return [], start, [f"Halaman {page} tidak tersedia. Buka /{command} {mode} {pages}."]
    links = [f"Halaman {page}/{pages}"]
    if page > 1:
        links.append(f"Sebelumnya: /{command} {mode} {page - 1}")
    if page < pages:
        links.append(f"Berikutnya: /{command} {mode} {page + 1}")
    return rows[start:start + 10], start, links


def _positions(snapshot: dict, page: int = 1) -> list[str]:
    rows = _rows(snapshot.get("positions"))
    account = _object(snapshot.get("account"))
    mode = "paper" if snapshot.get("mode") == "paper" else "live"
    shown, start, navigation = _pagination(rows, page, "posisi", mode)
    lines = [f"POSISI PASAR · {len(rows)} terlapor"]
    lines.extend(navigation)
    if not rows:
        lines.append("Tidak ada posisi." if account.get("positionsComplete") is True else "Data posisi belum tersedia.")
    for index, row in enumerate(shown, start=start + 1):
        lines.extend([
            f"\n{index}. {_text(row.get('title'), 92)}",
            f"{_text(row.get('outcome'), 28)} · {_text(row.get('status'), 16)}"
            + (" · dapat diklaim" if row.get("redeemable") is True else ""),
            f"Nilai {_money(row.get('value'))} | PnL {_money(row.get('pnl'), signed=True)} ({_percent(row.get('pnlPercent'))})",
            f"Saham {_shares(row.get('size'))} | Harga {_price(row.get('currentPrice'))}",
        ])
        if mode == "paper" and row.get("entryKind") == "paper_exploratory" and row.get("source") == "ledger":
            lines.append(_EXPLORATORY_LABEL)
    if len(rows) > 10 and shown:
        lines.append(f"\nMenampilkan {len(shown)} dari {len(rows)} posisi; urut nilai pada snapshot.")
    lines.append("\nNilai posisi mengikuti snapshot; harga jual aktual dapat berbeda.")
    return lines


def _price(value: Any) -> str:
    number = _number(value)
    return f"${number:.4f}" if number is not None else "-"


def _activity(snapshot: dict, page: int = 1) -> list[str]:
    rows = _rows(snapshot.get("trades"))
    mode = "paper" if snapshot.get("mode") == "paper" else "live"
    shown, start, navigation = _pagination(rows, page, "aktivitas", mode)
    lines = ["AKTIVITAS BOT", "Sumber: ledger bot lokal; tidak mencakup seluruh transaksi manual akun."]
    lines.extend(navigation)
    if not rows:
        lines.append("\nBelum ada transaksi pada ledger." if _object(snapshot.get("bot")).get("ledgerAvailable") is True
                     else "\nLedger bot belum tersedia.")
    for index, row in enumerate(shown, start=start + 1):
        lines.extend([
            f"\n{index}. {format_trade_action(row.get('side'), row.get('outcome'))} · {_text(row.get('title'), 85)}",
            f"Kas {_money(row.get('cash'))} | PnL {_money(row.get('pnl'), signed=True)}",
            _time(row.get("time")),
        ])
        if mode == "paper" and row.get("entryKind") == "paper_exploratory" and row.get("source") == "ledger":
            lines.append(_EXPLORATORY_LABEL)
    if len(rows) > 10 and shown:
        lines.append(f"\nMenampilkan {len(shown)} aktivitas dari {len(rows)} terbaru pada snapshot.")
    return lines


def _scan(snapshot: dict) -> list[str]:
    bot = _object(snapshot.get("bot"))
    has_scan = bool(_object(bot.get("lastScan")))
    status = {"recent_scan": "ada scan terbaru", "unknown": "belum diketahui", "halted": "tertahan pengaman",
              "stopped": "STOP aktif"}.get(bot.get("status"), "belum diketahui")
    lines = [
        "PEMINDAIAN PASAR", f"Status ledger: {status}",
        f"Scan terakhir: {_scan_time(snapshot)}",
        f"Outcome dipindai: {_count(bot.get('scanned')) if has_scan else '-'}",
        f"Kandidat dianalisis: {_count(bot.get('analyzed')) if has_scan else '-'}",
        f"Kandidat lolos: {_count(bot.get('qualifiedCount')) if has_scan else '-'}",
        f"Intent belum selesai: {_count(bot.get('pendingCount')) if bot.get('ledgerAvailable') is True else '-'}",
        "Status ledger bukan bukti proses masih berjalan. Lihat /status untuk proses Telegram.",
    ]
    if snapshot.get("mode") == "paper" and (_number(bot.get("exploratoryCount")) or 0) > 0:
        lines.insert(6, f"Kandidat eksplorasi paper: {_count(bot.get('exploratoryCount'))} · probabilitas belum tersedia")
    rejects = _rows(bot.get("rejections"))
    if rejects:
        lines.extend(["", "Alasan penolakan:"])
        for row in rejects[:10]:
            reason = _text(row.get("reason"), 65)
            reason = _REJECTION_LABELS.get(reason, reason.replace("_", " "))
            lines.append(f"• {reason}: {_count(row.get('count'))}")
    return lines


def _risk(snapshot: dict) -> list[str]:
    risk = _object(snapshot.get("risk"))
    return [
        "STRATEGI & BATAS RISIKO",
        f"Entry dasar: {_money(risk.get('entryDollars'))}",
        f"Target minimum profit per entry: {_money(risk.get('minimumProfit'))}",
        f"Take profit ROI: {_percent(risk.get('takeProfitRoi'), fraction=True)}",
        f"Stop loss: {_percent(risk.get('stopLossFraction'), fraction=True)}",
        f"Probabilitas minimum model: {_percent(risk.get('minimumProbability'), fraction=True)}",
        f"Maksimum porsi per posisi: {_percent(risk.get('maxPositionFraction'), fraction=True)}",
        f"Maksimum eksposur: {_percent(risk.get('maxExposureFraction'), fraction=True)}",
        f"Cadangan kas: {_money(risk.get('reserveCash'))}",
        f"Maksimum posisi terbuka: {_count(risk.get('maxOpenPositions'))}",
        f"Batas rugi harian: {_money(risk.get('dailyLossLimit'))}",
        f"Batas drawdown: {_percent(risk.get('maxDrawdownFraction'), fraction=True)}",
        f"Saldo fase pertumbuhan: {_money(risk.get('growthBalance'))}",
        f"Entry fase pertumbuhan: {_money(risk.get('growthEntryDollars'))}",
        f"Probabilitas minimum fase pertumbuhan: {_percent(risk.get('growthMinProbability'), fraction=True)}",
        f"Horizon maksimum: {_count(risk.get('maxHorizonHours'))} jam",
        f"Interval scan: {_count(risk.get('pollSeconds'))} detik",
        "",
        "Target dan estimasi probabilitas bukan jaminan profit. Eksekusi mengikuti likuiditas dan pengaman bot.",
    ]


def format_snapshot(snapshot: dict, section: str = "summary", page: int = 1) -> str:
    """Format one monitoring view as plain text (comfortably below 4,096 chars)."""
    snapshot = _object(snapshot)
    views = {"summary": _summary, "positions": _positions, "activity": _activity, "scan": _scan, "risk": _risk}
    if section not in views:
        raise ValueError("Unknown dashboard section")
    if type(page) is not int or page < 1:
        raise ValueError("Page must be a positive integer")
    content = views[section](snapshot, page) if section in ("positions", "activity") else views[section](snapshot)
    lines = [f"POLYBOT · {_mode(snapshot)}", *content, *_errors(snapshot),
             "", f"Snapshot: {_time(snapshot.get('generatedAt'))}"]
    message = "\n".join(lines)
    # Telegram counts UTF-16 code units, so account for non-BMP market characters.
    if len(message.encode("utf-16-le")) > 7600:
        suffix = "\n… Tampilan dipersingkat."
        budget = 7600 - len(suffix.encode("utf-16-le"))
        message = message.encode("utf-16-le")[:budget].decode("utf-16-le", errors="ignore") + suffix
    return message


def format_fresh_scan(report: dict) -> str:
    """Render a public, non-trading scan without implying account eligibility.

    Only known report fields are displayed. Exceptions, raw halt/rejection codes,
    wallet details and execution data are intentionally excluded.
    """
    report = _object(report)
    live = report.get("mode") == "live"
    qualified = _rows(report.get("qualified"))
    candidates = [row for row in qualified if row.get("entry_kind") != "paper_exploratory"]
    exploratory = ([row for row in qualified if row.get("entry_kind") == "paper_exploratory"]
                   if not live else [])
    lines = [
        "POLYBOT | SCAN PASAR TERBARU",
        f"Aturan strategi: {'LIVE' if live else 'PAPER'} | Data pasar publik",
        f"Waktu scan: {_time(report.get('time'))}",
        f"Outcome dipindai: {_count(report.get('scanned'))}",
        f"Kandidat dianalisis: {_count(report.get('analyzed'))}",
        f"Kandidat lolos: {len(candidates)}",
        f"Modal asumsi konfigurasi: {_money(report.get('analysis_cash'))}",
        f"Entry asumsi: {_money(report.get('analysis_entry'))}",
        "Saldo dan posisi akun belum diperiksa; kelayakan entry akun dapat berbeda.",
        "Scan ini tidak mengirim order.",
    ]
    if exploratory:
        lines.insert(6, f"Kandidat eksplorasi paper: {len(exploratory)} (terpisah dari kandidat forecast)")
    if live:
        lines.append("Aturan LIVE memerlukan forecast eksternal terkalibrasi.")
    if _object(report.get("scan_overrides")):
        lines.append("Profil scan diperluas; syarat trading tetap mengikuti konfigurasi trading.")
    if report.get("halted"):
        lines.append("Pemindaian tertahan pengaman; hasil belum lengkap.")
    if report.get("analysis_budget_reached") is True:
        lines.append("Hasil parsial: batas waktu analisis tercapai.")
    if not candidates:
        lines.extend(["", "Belum ada kandidat yang lolos aturan strategi pada scan ini."])
    else:
        lines.extend(["", "Kandidat berdasarkan urutan skor analisis:",
                      "Probabilitas adalah batas bawah estimasi model, bukan jaminan menang."])
        for index, row in enumerate(candidates[:5], start=1):
            probability = _number(row.get("probability_lower"))
            probability_text = (f"{probability * 100:.1f}%"
                                if probability is not None and 0 <= probability <= 1 else "-")
            horizon = (">24 jam" if row.get("long_term") is True else
                       "<=24 jam" if row.get("long_term") is False else "-")
            lines.extend([
                f"\n{index}. {_text(row.get('question'), 100)}",
                f"Pilihan: {_text(row.get('outcome'), 28)} | Budget: {_money(row.get('budget'))}",
                f"Target harga jual: {_price(row.get('target_price'))}",
                f"Probabilitas model (batas bawah): {probability_text} | Horizon: {horizon}",
            ])
        if len(candidates) > 5:
            lines.append(f"\nMenampilkan 5 dari {len(candidates)} kandidat.")
    if exploratory:
        lines.extend(["", _EXPLORATORY_LABEL,
                      "Hanya untuk menguji eksekusi paper; belum menjadi sinyal entry uang asli.",
                      "Target exit adalah sasaran profit, bukan forecast harga atau peluang tercapai."])
        for index, row in enumerate(exploratory[:5], start=1):
            lines.extend([
                f"\n{index}. {_text(row.get('question'), 100)}",
                f"Pilihan: {_text(row.get('outcome'), 28)} | Budget paper: {_money(row.get('budget'))}",
                f"Target exit simulasi: {_price(row.get('target_price'))}",
            ])
        if len(exploratory) > 5:
            lines.append(f"\nMenampilkan 5 dari {len(exploratory)} kandidat eksplorasi.")
    watchlist = _rows(report.get("watchlist"))
    if watchlist:
        lines.extend(["", f"DAFTAR PANTAUAN: {min(5, len(watchlist))} dari {len(watchlist)} | BELUM LOLOS ENTRY",
                      "Perlu pemeriksaan lanjutan; belum menjadi sinyal beli."])
        for index, row in enumerate(watchlist[:5], start=1):
            reason = _REJECTION_LABELS.get(row.get("reason"), "Syarat entry belum terpenuhi")
            lines.extend([
                f"{index}. {_text(row.get('question'), 76)} · {_text(row.get('outcome'), 20)}",
                f"Ask {_price(row.get('ask'))} | Minimum nilai order {_price(row.get('minimum_notional'))}",
                f"Belum entry: {reason}",
            ])
        lines.append("Minimum nilai order di atas belum termasuk biaya dan slippage; harga bukan probabilitas model.")
    rejection_counts: dict[str, float] = {}
    for reason, raw_count in _object(report.get("rejections")).items():
        count = _number(raw_count)
        if count is None or count <= 0:
            continue
        label = _REJECTION_LABELS.get(reason, "Alasan lain")
        rejection_counts[label] = rejection_counts.get(label, 0) + count
    if rejection_counts:
        lines.extend(["", "Alasan penolakan terbanyak:"])
        for label, count in sorted(rejection_counts.items(), key=lambda item: item[1], reverse=True)[:8]:
            lines.append(f"- {label}: {_count(count)}")
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        lines.append(f"\nGangguan data: {len(errors)}; sebagian analisis tidak tersedia.")
    message = "\n".join(lines)
    # The API accepts at most 3,800 UTF-16 code units, including our suffix.
    encoded = message.encode("utf-16-le")
    if len(encoded) > 7600:
        suffix = "\n... Tampilan dipersingkat."
        budget = 7600 - len(suffix.encode("utf-16-le"))
        message = encoded[:budget].decode("utf-16-le", errors="ignore") + suffix
    return message


def format_status(workers: dict, notifications: bool) -> str:
    """Render only process states, never process commands or diagnostic output."""
    lines = ["POLYBOT · STATUS PROSES TELEGRAM"]
    for mode, label in (("paper", "PAPER"), ("live", "AKUN REAL")):
        item = _object(workers).get(mode)
        worker = _object(item)
        state = item if isinstance(item, str) else worker.get("status", worker.get("state"))
        if worker.get("stopping") is True or state == "stopping":
            display = "sedang berhenti setelah operasi aktif selesai"
        elif worker.get("running") is True or state in ("running", "once"):
            display = "berjalan · satu siklus" if worker.get("once") is True or state == "once" else "berjalan · berulang"
        else:
            display = "tidak berjalan"
        lines.append(f"{label}: {display}")
    lines.extend([f"Notifikasi: {'aktif' if notifications else 'nonaktif'}", "",
                  "Status ini hanya mencakup proses yang dikelola layanan Telegram ini.",
                  "Menghentikan proses tidak menjual posisi atau membatalkan order yang sudah dikirim."])
    return "\n".join(lines)


def chart_png(snapshot: dict) -> bytes | None:
    """Render actual equity observations to PNG bytes; never synthesize history.

    Duplicate timestamps keep their last observation. The single-point case is a
    dot with a value, not a fabricated trend. Matplotlib is optional and lazily
    loaded so text monitoring continues to work when it is not installed.
    """
    points: dict[datetime, float] = {}
    for row in _rows(_object(snapshot).get("equityHistory")):
        stamp, equity = _date(row.get("time")), _number(row.get("equity"))
        if stamp is not None and equity is not None:
            points[stamp] = equity
    if not points:
        return None
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
        from matplotlib.figure import Figure
        from matplotlib.ticker import FuncFormatter
    except ImportError:
        return None
    rows = sorted(points.items())
    if len(rows) > 200:
        rows = [rows[round(i * (len(rows) - 1) / 199)] for i in range(200)]
    times, values = zip(*rows)
    with _CHART_LOCK:
        figure = Figure(figsize=(10, 4.6), dpi=110, facecolor="#0c1214")
        FigureCanvasAgg(figure)
        ax = figure.add_subplot(111, facecolor="#11191b")
        if len(rows) == 1:
            ax.scatter(times, values, color="#b4f36d", s=60, zorder=4)
            ax.set_xlim(times[0] - timedelta(minutes=30), times[0] + timedelta(minutes=30))
        else:
            ax.plot(times, values, color="#b4f36d", linewidth=2.4, marker="o" if len(rows) < 15 else None,
                    markersize=4)
        low, high = min(values), max(values)
        pad = max((high - low) * 0.25, max(abs(low), abs(high)) * 0.02, 0.02)
        ax.set_ylim(low - pad, high + pad)
        ax.annotate(_money(values[-1]), (times[-1], values[-1]), xytext=(-5, 10),
                    textcoords="offset points", color="#b4f36d", fontsize=11,
                    fontweight="bold", ha="right")
        locator = AutoDateLocator(minticks=3, maxticks=8, interval_multiples=False)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(ConciseDateFormatter(locator, tz=timezone.utc))
        decimals = 4 if high - low + 2 * pad < 0.1 else 2
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"${value:,.{decimals}f}"))
        ax.tick_params(colors="#a8b5b3", labelsize=9, length=0, pad=9)
        ax.xaxis.get_offset_text().set_color("#a8b5b3")
        ax.grid(axis="y", color="#263330", linewidth=0.8)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)
        figure.suptitle(f"POLYBOT | {'PAPER (simulasi)' if snapshot.get('mode') == 'paper' else 'AKUN REAL'}",
                        color="#ecf1ea", fontsize=16, fontweight="bold", x=0.1, ha="left", y=0.95)
        ax.set_title("Ekuitas dari pengamatan tersimpan", color="#a8b5b3", fontsize=10, loc="left", pad=16)
        figure.text(0.1, 0.04, f"{len(points)} pengamatan | UTC | Perubahan ekuitas termasuk arus kas, bukan profit saja.",
                    color="#a8b5b3", fontsize=8)
        figure.subplots_adjust(left=0.1, right=0.97, bottom=0.2, top=0.79)
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=110, facecolor=figure.get_facecolor())
        result = buffer.getvalue()
        figure.clear()
        return result if len(result) <= 1_000_000 else None
