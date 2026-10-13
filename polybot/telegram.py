"""Private Telegram dashboard and explicitly controlled paper/live workers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from .__main__ import load_env
from .config import load_config
from .state import process_lock
from .telegram_api import TelegramAPI, TelegramError
from .telegram_views import chart_png, format_fresh_scan, format_snapshot, format_status, format_trade_action
from .telegram_worker import TradingWorkers


HELP = """POLYBOT — pusat kontrol pribadi

Dashboard (tambahkan paper atau live):
/ringkasan paper — saldo, ekuitas, dan PnL
/saldo live — saldo akun real
/posisi live 1 — posisi, 10 per halaman
/aktivitas paper 1 — transaksi ledger bot
/scan paper — scan pasar sekarang, tanpa order
/scan live — scan dengan syarat forecast live
/scan_last paper — laporan siklus bot terakhir
/risiko — batas strategi
/grafik live — grafik snapshot ekuitas
/status — proses yang dikelola Telegram

Kontrol simulasi:
/paper_once — satu siklus paper
/paper_on — paper berulang
/paper_off — hentikan paper
Paper eksplorasi memakai harga/depth nyata tanpa estimasi probabilitas ketika forecast belum tersedia; tidak berlaku untuk live.

Kontrol uang asli:
/live_on — mulai trading uang asli sesuai strategi
/live_off — hentikan proses live

/notifikasi_on atau /notifikasi_off

Arti posisi: ENTRY YES = BUY Yes (peristiwa terjadi), ENTRY NO = BUY No (peristiwa tidak terjadi). SELL menutup saham Yes/No yang sudah dimiliki; ditampilkan sebagai EXIT. Pemilihan outcome tetap mengikuti analisis, bukan jaminan hasil.

Mode tanpa argumen adalah paper. Scan memakai modal analisis dari konfigurasi, bukan saldo aktual akun. Stop menghentikan pengiriman berikutnya; order yang sudah dikirim diselesaikan dahulu. Posisi tidak dijual otomatis. Live tetap mengikuti pemeriksaan saldo, wilayah, forecast, dan batas risiko."""

SECTIONS = {"ringkasan": "summary", "saldo": "summary", "posisi": "positions",
            "aktivitas": "activity", "scan_last": "scan", "risiko": "risk", "grafik": "chart"}
START_COMMANDS = {"paper_on", "paper_once", "live_on"}
PAIRING_HELP = """PolyBOT aktif dan menunggu pairing pemilik.

Kirim /pair diikuti kode pairing dari komputer atau percakapan penyiapan Anda.
Contoh: /pair KODE_PAIRING

Anda juga dapat membuka tautan pairing yang diberikan lalu menekan Start. Menu akun real/paper akan muncul setelah pairing berhasil."""


def _positive_id(value) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,17}", value):
        value = int(value)
    return value if type(value) is int and 0 < value < 10**18 else None


def private_sender(update: dict) -> tuple[dict, int] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender, chat = message.get("from", {}), message.get("chat", {})
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    owner = _positive_id(sender.get("id"))
    if (not owner or chat.get("type") != "private" or chat.get("id") != owner
            or sender.get("is_bot") is not False or message.get("sender_chat")
            or any(key in message for key in ("forward_origin", "forward_from", "forward_date"))):
        return None
    return message, owner


def read_snapshot(root: Path, config_path: Path, mode: str) -> dict:
    if mode not in ("paper", "live"):
        raise ValueError("Mode tidak valid")
    process = subprocess.run(
        [sys.executable, "-m", "polybot.monitor", "--mode", mode,
         "--config", str(config_path), "--output-json"],
        cwd=str(root), capture_output=False, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=75, shell=False,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode or len(process.stdout) > 5_000_000:
        raise RuntimeError("Pemantauan belum tersedia")
    result = json.loads(process.stdout)
    if not isinstance(result, dict) or result.get("mode") != mode or not isinstance(result.get("account"), dict):
        raise RuntimeError("Format pemantauan tidak valid")
    return result


def read_fresh_scan(root: Path, config_path: Path, mode: str) -> dict:
    """Bound public analysis independently of trading workers and their ledgers."""
    if mode not in ("paper", "live"):
        raise ValueError("Mode tidak valid")
    process = subprocess.run(
        [sys.executable, "-m", "polybot.scan", "--mode", mode,
         "--config", str(config_path)],
        cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=180, shell=False, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode or len(process.stdout) > 5_000_000:
        raise RuntimeError("Scan belum tersedia")
    result = json.loads(process.stdout)
    if (not isinstance(result, dict) or result.get("mode") != mode
            or result.get("scan_only") is not True or result.get("orders") != []
            or result.get("capital_basis") != "configured_starting_cash"
            or not isinstance(result.get("qualified"), list)):
        raise RuntimeError("Format scan tidak valid")
    return result


def save_owner(env_path: Path, owner: int):
    """Only replace the owner setting; never display or interpolate .env values."""
    if not _positive_id(owner):
        raise ValueError("ID Telegram tidak valid")
    existing = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else ""
    lines, replaced = [], False
    for line in existing.splitlines():
        key, sep, _ = line.partition("=")
        if sep and key.strip() == "TELEGRAM_ALLOWED_USER_ID":
            if not replaced:
                lines.append(f"TELEGRAM_ALLOWED_USER_ID={owner}")
                replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"TELEGRAM_ALLOWED_USER_ID={owner}")
    # A temporary file in the same directory permits an atomic replacement.
    temporary = env_path.with_name(env_path.name + ".telegram.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_path)
    finally:
        temporary.unlink(missing_ok=True)


def discard_backlog(api: TelegramAPI) -> int:
    # Old commands must never start live trading after a restart.
    rows = api.get_updates(offset=-1, timeout=0)
    ids = [row["update_id"] for row in rows if type(row.get("update_id")) is int]
    return max(ids) + 1 if ids else 0


def pair(api: TelegramAPI, env_path: Path, username: str, timeout: int = 300,
         *, stop_path: Path | None = None, challenge_path: Path | None = None) -> int:
    nonce = secrets.token_urlsafe(18)
    expires_at = time.time() + timeout
    resumed = False
    if challenge_path is not None:
        try:
            saved = json.loads(challenge_path.read_text(encoding="utf-8"))
            if (saved.get("username") == username and
                    isinstance(saved.get("nonce"), str) and
                    re.fullmatch(r"[A-Za-z0-9_-]{24}", saved["nonce"]) and
                    type(saved.get("expires_at")) in (int, float) and
                    0 < saved["expires_at"] - time.time() <= 86400):
                nonce, expires_at, resumed = saved["nonce"], saved["expires_at"], True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        challenge_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = challenge_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"nonce": nonce, "expires_at": expires_at,
                                         "username": username}), encoding="utf-8")
        os.replace(temporary, challenge_path)
    remaining = max(0, expires_at - time.time())
    deadline = time.monotonic() + remaining
    last_heartbeat = 0.0
    poll_count = update_count = 0
    prompted_senders = set()

    def poll(offset, wait):
        nonlocal poll_count, update_count, last_heartbeat
        backoff = 3
        while time.monotonic() < deadline:
            if stop_path is not None and stop_path.exists():
                raise RuntimeError("Pairing dihentikan oleh operator.")
            try:
                rows = api.get_updates(offset=offset, timeout=wait)
                poll_count += 1
                update_count += len(rows)
                if time.monotonic() - last_heartbeat >= 30:
                    print(json.dumps({"pairing_poll": poll_count, "updates_received": update_count}), flush=True)
                    last_heartbeat = time.monotonic()
                return rows
            except TelegramError as exc:
                if exc.code in (401, 404, 409):
                    raise
                print(json.dumps({"pairing_network_retry": exc.code}), flush=True)
                retry_until = min(deadline, time.monotonic() + max(1, exc.retry_after or backoff))
                backoff = min(30, backoff * 2)
                while time.monotonic() < retry_until:
                    if stop_path is not None and stop_path.exists():
                        raise RuntimeError("Pairing dihentikan oleh operator.")
                    time.sleep(0.25)
        raise RuntimeError("Pairing kedaluwarsa. Jalankan --pair kembali.")

    # A still-valid challenge can survive a process/network restart. Keep its
    # queued messages; replacing the nonce and dropping them strands the user.
    backlog = [] if resumed else poll(-1, 0)
    ids = [row["update_id"] for row in backlog if type(row.get("update_id")) is int]
    offset = max(ids) + 1 if ids else 0
    print(json.dumps({"pair_url": f"https://t.me/{username}?start=pair_{nonce}",
                      "command": f"/pair {nonce}", "expires_seconds": int(remaining),
                      "instruction": "Buka tautan di Telegram Anda lalu tekan Start."}), flush=True)
    while time.monotonic() < deadline:
        if stop_path is not None and stop_path.exists():
            raise RuntimeError("Pairing dihentikan oleh operator.")
        for update in poll(offset, 5):
            if stop_path is not None and stop_path.exists():
                raise RuntimeError("Pairing dihentikan oleh operator.")
            identifier = update.get("update_id")
            if type(identifier) is not int or identifier < offset:
                continue
            offset = identifier + 1
            identity = private_sender(update)
            if identity is None:
                print('{"pairing_rejected": "Bukan pesan privat langsung dari akun pengguna."}', flush=True)
                continue
            message, owner = identity
            text = message.get("text")
            if not isinstance(text, str):
                continue
            parts = text.strip().split()
            if len(parts) == 3 and parts[-1] == ".":
                parts.pop()
            command = parts[0].lower() if parts else ""
            if "@" in command:
                command, _, recipient = command.partition("@")
                if recipient != username.lower():
                    continue
            if len(parts) == 1 and command in ("/start", "/help", "/pair"):
                if owner not in prompted_senders and len(prompted_senders) < 100:
                    prompted_senders.add(owner)
                    try:
                        api.send_message(owner, PAIRING_HELP)
                    except TelegramError as exc:
                        print(json.dumps({"pairing_help_send_failed": exc.code}), flush=True)
                continue
            code = parts[1].removesuffix(".") if len(parts) == 2 else ""
            candidate = nonce if command == "/pair" else "pair_" + nonce if command == "/start" else ""
            if candidate and hmac.compare_digest(code.encode("utf-8"), candidate.encode("utf-8")):
                save_owner(env_path, owner)
                if challenge_path is not None:
                    challenge_path.unlink(missing_ok=True)
                # Acknowledge the pairing update so it cannot be read again.
                try:
                    api.get_updates(offset=offset, timeout=0)
                    api.send_message(owner, "Pairing berhasil. Hanya akun Telegram Anda yang dapat mengakses PolyBOT.\n"
                                     "Paper dan live belum dijalankan. Buka /help setelah layanan aktif.", keyboard=True)
                except TelegramError as exc:
                    # Binding is already durably saved; an ambiguous notification
                    # must not undo it or be blindly resent.
                    print(json.dumps({"pairing_notification_failed": exc.code}), flush=True)
                print(json.dumps({"paired": True, "owner_id": owner}), flush=True)
                return owner
            print('{"pairing_waiting": "Pesan diterima, tetapi kode pairing belum cocok."}', flush=True)
    raise RuntimeError("Pairing kedaluwarsa. Jalankan --pair kembali.")


class TelegramController:
    def __init__(self, api, owner: int, workers, root: Path, config_path: Path,
                 state_dir: Path, username: str, *, snapshot_reader=None, scan_reader=None):
        if not _positive_id(owner):
            raise ValueError("TELEGRAM_ALLOWED_USER_ID belum diisi. Jalankan --pair.")
        self.api, self.owner, self.workers = api, owner, workers
        self.root, self.config_path, self.state_dir = root, config_path, state_dir
        self.username = username.lower()
        self.snapshot_reader = snapshot_reader or (lambda mode: read_snapshot(root, config_path, mode))
        self.scan_reader = scan_reader or (lambda mode: read_fresh_scan(root, config_path, mode))
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-monitor")
        self.pending = None
        self.offset = 0
        self.notifications = True
        self.last_halt = {}
        self.once_modes = set()
        self.last_request = 0.0
        self.last_scan_request = float("-inf")
        self.preferences = state_dir / "telegram-preferences.json"
        try:
            saved = json.loads(self.preferences.read_text(encoding="utf-8"))
            if type(saved.get("notifications")) is bool:
                self.notifications = saved["notifications"]
        except (OSError, ValueError, AttributeError):
            pass

    def _send(self, text, *, keyboard=False):
        try:
            self.api.send_message(self.owner, text, keyboard=keyboard)
        except TelegramError as exc:
            # Do not retry an ambiguous send, which could duplicate a notice.
            print(json.dumps({"telegram_send_failed": exc.code}), flush=True)

    def _save_preferences(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.preferences.with_suffix(".tmp")
        temporary.write_text(json.dumps({"notifications": self.notifications}), encoding="utf-8")
        os.replace(temporary, self.preferences)

    def dispatch(self, update: dict):
        identifier = update.get("update_id")
        if type(identifier) is not int or identifier < self.offset:
            return
        self.offset = identifier + 1
        identity = private_sender(update)
        if identity is None or identity[1] != self.owner:
            return  # No response or account data to anyone else, including groups.
        message = identity[0]
        text = message.get("text")
        if not isinstance(text, str) or len(text) > 200 or not text.startswith("/"):
            return
        parts = text.split()
        if not parts:
            return
        command, _, target = parts[0][1:].partition("@")
        if target and target.lower() != self.username:
            return
        command, args = command.lower(), parts[1:]
        if command in START_COMMANDS:
            stamp = message.get("date")
            if type(stamp) is not int or not -30 <= time.time() - stamp <= 120:
                self._send("Perintah mulai sudah terlalu lama. Kirim ulang jika masih ingin menjalankan bot.")
                return
        if command in ("start", "help"):
            self._send(HELP, keyboard=True)
        elif command == "status" and not args:
            status = format_status(self.workers.statuses(), self.notifications)
            if self.pending is not None and self.pending[1] == "scan":
                status += f"\nScan pasar: sedang berjalan (aturan {self.pending[2]}), tanpa order."
            self._send(status)
        elif command in ("paper_on", "paper_once", "live_on") and not args:
            mode = "live" if command == "live_on" else "paper"
            if command == "paper_once" and not self.workers.statuses()["paper"]["running"]:
                self.once_modes.add("paper")
            self._send(self.workers.start(mode, once=command == "paper_once"))
        elif command in ("paper_off", "live_off") and not args:
            self._send(self.workers.stop("live" if command == "live_off" else "paper"))
        elif command in ("notifikasi_on", "notifikasi_off") and not args:
            self.notifications = command == "notifikasi_on"
            self._save_preferences()
            self._send("Notifikasi transaksi " + ("aktif." if self.notifications else "nonaktif.") +
                       " Status mulai, berhenti, dan kegagalan proses tetap dikirim.")
        elif command in SECTIONS or command == "scan":
            self._request_view(command, args)
        else:
            self._send("Perintah tidak dikenal atau argumennya tidak sesuai. Buka /help.")

    def _request_view(self, command, args):
        mode = args[0].lower() if args else "paper"
        mode = "live" if mode == "real" else mode
        paged = command in ("posisi", "aktivitas")
        page = args[1] if paged and len(args) > 1 else "1"
        if (mode not in ("paper", "live") or len(args) > (2 if paged else 1)
                or not re.fullmatch(r"[1-9][0-9]{0,3}", page)):
            self._send(f"Gunakan /{command} paper atau /{command} live" +
                       (" dengan nomor halaman, misalnya /posisi live 2." if paged else "."))
            return
        if self.pending is not None:
            self._send("Data sedang diambil. /status dan perintah berhenti tetap dapat digunakan.")
            return
        if command == "scan" and time.monotonic() - self.last_scan_request < 30:
            self._send("Tunggu 30 detik sejak scan sebelumnya dimulai sebelum meminta scan baru.")
            return
        if time.monotonic() - self.last_request < 2:
            return
        self.last_request = time.monotonic()
        if command == "scan":
            self.last_scan_request = self.last_request
        self.pending = (self.executor.submit(self._render, mode, command, int(page)), command, mode)
        if command == "scan":
            self._send(f"Memindai pasar non-crypto sekarang (aturan {mode}). Tidak membuat order.\n"
                       "Modal analisis mengikuti konfigurasi, bukan saldo akun. Hasil dikirim otomatis, "
                       "batas waktu 3 menit. /status dan perintah berhenti tetap dapat digunakan.")
        else:
            self._send(f"Mengambil {command} {'akun real' if mode == 'live' else 'paper'}...")

    def _render(self, mode, command, page):
        if command == "scan":
            return None, format_fresh_scan(self.scan_reader(mode))
        snapshot = self.snapshot_reader(mode)
        section = SECTIONS[command]
        if section == "chart":
            png = chart_png(snapshot)
            caption = ("AKUN REAL" if mode == "live" else "PAPER (simulasi)") + " — snapshot ekuitas.\n"
            caption += "Grafik dimulai sejak pemantauan; deposit/withdrawal juga mengubah nilainya."
            return (png, caption) if png else (None, "Grafik belum tersedia. Kumpulkan snapshot melalui /ringkasan " + mode + ".")
        return None, format_snapshot(snapshot, section, page=page)

    def tick(self):
        if self.pending is not None and self.pending[0].done():
            future, command, _ = self.pending
            self.pending = None
            try:
                png, text = future.result()
                if png:
                    self.api.send_photo(self.owner, png, caption=text)
                else:
                    self._send(text)
            except TelegramError as exc:
                print(json.dumps({"telegram_send_failed": exc.code}), flush=True)
            except subprocess.TimeoutExpired:
                self._send("Scan melewati batas waktu 3 menit dan dihentikan; hasil belum tersedia. "
                           "Coba /scan lagi atau kurangi scan_pages di konfigurasi lokal."
                           if command == "scan" else "Pemantauan melewati batas waktu. Coba lagi.")
            except Exception:
                self._send(("Scan gagal. Tidak ada order dari scan ini. " if command == "scan" else "Data belum tersedia. ") +
                           "Periksa koneksi atau konfigurasi lokal; detail kredensial tidak dikirim.")
        for event in self.workers.drain_events():
            mode, kind = event.get("mode"), event.get("type")
            label = "REAL (uang asli)" if mode == "live" else "PAPER (simulasi)"
            if kind == "cycle":
                report = event.get("report", {})
                halted = report.get("halted")
                notify = (mode in self.once_modes or bool(report.get("orders")) or
                          (halted and self.last_halt.get(mode) != halted))
                self.last_halt[mode] = halted
                if notify and (self.notifications or mode in self.once_modes or halted):
                    lines = [f"{label} — hasil siklus",
                             f"Dipindai: {report.get('scanned', 0)} | Dianalisis: {report.get('analyzed', 0)}",
                             f"Kandidat dengan forecast: {max(0, report.get('qualified_count', len(report.get('qualified', []))) - report.get('exploratory_count', 0))}",
                             f"Order: {report.get('orders_count', len(report.get('orders', [])))}"]
                    if mode == "paper" and report.get("exploratory_count"):
                        lines.append(f"Kandidat simulasi eksplorasi: {report['exploratory_count']} (probabilitas belum tersedia).")
                    for order in report.get("orders", [])[:8]:
                        suffix = " · simulasi eksplorasi" if mode == "paper" and order.get("entry_kind") == "paper_exploratory" else ""
                        action = format_trade_action(order.get("side"), order.get("outcome"))
                        lines.append(f"{action} · {order.get('status', '-')}{suffix}")
                    if halted:
                        lines.append(f"Alasan berhenti/tertahan: {halted}")
                    lines.append(f"Detail: /scan_last {mode} | /aktivitas {mode}")
                    self._send("\n".join(lines))
                self.once_modes.discard(mode)
            elif kind in ("started", "stopped", "error"):
                self._send(f"{label}\n{event.get('message', 'Status worker berubah.')}")
                if kind == "stopped":
                    self.once_modes.discard(mode)

    def close(self):
        self.workers.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def run(self, stop_path: Path):
        self.offset = discard_backlog(self.api)
        self.api.set_commands()
        self._send("PolyBOT tersambung. Paper dan live belum dijalankan.\n"
                   "Buka /help atau gunakan menu di bawah.", keyboard=True)
        retry = 1
        try:
            while not stop_path.exists():
                self.tick()
                try:
                    updates = self.api.get_updates(offset=self.offset, timeout=5)
                    for update in updates:
                        if stop_path.exists():
                            break
                        try:
                            self.dispatch(update)
                        except Exception:
                            # Fail closed on malformed inputs/storage errors; no raw traceback.
                            print('{"telegram_command_error": true}', flush=True)
                    retry = 1
                except TelegramError as exc:
                    print(json.dumps({"telegram_poll_error": exc.code}), flush=True)
                    if exc.code in (401, 404, 409):
                        raise
                    deadline = time.monotonic() + max(1, exc.retry_after or retry)
                    while not stop_path.exists() and time.monotonic() < deadline:
                        self.tick()
                        time.sleep(0.25)
                    retry = min(30, retry * 2)
        finally:
            self.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PolyBOT Telegram: dashboard pribadi dan kontrol paper/live")
    parser.add_argument("--config", default="config.toml", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--pair", action="store_true", help="Hubungkan satu akun Telegram lewat kode sekali pakai")
    action.add_argument("--setup", action="store_true", help="Pairing lalu langsung jalankan layanan Telegram")
    action.add_argument("--check", action="store_true", help="Periksa token dan konfigurasi tanpa menjalankan trading")
    action.add_argument("--stop", action="store_true", help="Minta layanan Telegram dan worker miliknya berhenti")
    parser.add_argument("--pair-timeout", type=int, default=300,
                        help="Masa berlaku pairing, 60 sampai 86400 detik (default 300)")
    args = parser.parse_args(argv)
    if not 60 <= args.pair_timeout <= 86400:
        parser.error("--pair-timeout harus antara 60 dan 86400")
    controller = None
    try:
        root = Path.cwd().resolve()
        config_path = (root / args.config).resolve()
        cfg = load_config(str(config_path))
        state_dir = (root / cfg.state_dir).resolve()
        stop_path = state_dir / "telegram-service.stop"
        if args.stop:
            state_dir.mkdir(parents=True, exist_ok=True)
            stop_path.write_text("Operator requested graceful service stop\n", encoding="utf-8")
            print("Permintaan berhenti dikirim. Order yang sedang diproses diselesaikan dahulu.", flush=True)
            return 0
        load_env(str(root / ".env"))
        api = TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        profile = api.get_me()
        username = profile.get("username", "")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,64}", username):
            raise RuntimeError("Username bot Telegram tidak valid.")
        owner = _positive_id(os.environ.get("TELEGRAM_ALLOWED_USER_ID", ""))
        if args.check:
            print(json.dumps({"connected": True, "bot": "@" + username,
                              "paired": owner is not None, "trading_started": False}), flush=True)
            return 0
        with process_lock(state_dir / "telegram.lock"):
            if api.call("getWebhookInfo").get("url"):
                raise RuntimeError("Bot memakai webhook. Gunakan bot Telegram khusus tanpa webhook untuk polling.")
            if args.pair or args.setup:
                stop_path.unlink(missing_ok=True)
                owner = pair(api, root / ".env", username, timeout=args.pair_timeout, stop_path=stop_path,
                             challenge_path=state_dir / "telegram-pairing.json")
                if args.pair or stop_path.exists():
                    return 0
            if owner is None:
                raise ValueError("TELEGRAM_ALLOWED_USER_ID belum diisi. Jalankan: python -m polybot.telegram --pair")
            stop_path.unlink(missing_ok=True)
            workers = TradingWorkers(config_path, root)
            controller = TelegramController(api, owner, workers, root, config_path, state_dir, username)
            print(json.dumps({"telegram": "running", "bot": "@" + username, "trading_started": False}), flush=True)
            controller.run(stop_path)
        return 0
    except KeyboardInterrupt:
        if controller:
            controller.close()
        print("Layanan Telegram berhenti; worker diminta berhenti dengan aman.", flush=True)
        return 0
    except TelegramError as exc:
        print(json.dumps({"error": str(exc), "code": exc.code}), flush=True)
        return 2
    except (ValueError, RuntimeError) as exc:
        # Only this module's own known messages are exposed.
        known = ("TELEGRAM_ALLOWED_USER_ID", "Pairing kedaluwarsa", "Pairing dihentikan", "Bot lain sedang",
                 "Username bot Telegram", "Bot memakai webhook")
        message = str(exc) if str(exc).startswith(known) else "Konfigurasi Telegram belum valid. Periksa file lokal."
        print(json.dumps({"error": message}), flush=True)
        return 2
    except Exception:
        print('{"error": "Layanan Telegram gagal. Periksa konfigurasi dan koneksi lokal."}', flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
