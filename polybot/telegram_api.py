"""Small Telegram Bot API client with credential-safe failures.

Only outbound HTTPS is needed; no web server or additional dependency is used.
Requests are never retried here because retrying sendMessage after a network
timeout could deliver the same message twice. Bot API reference:
https://core.telegram.org/bots/api
"""
from __future__ import annotations

import json
import math
import re
import secrets
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_TEXT_UNITS = 3800
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PHOTO_BYTES = 5 * 1024 * 1024
_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,200}\Z", re.ASCII)
_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\Z", re.ASCII)

COMMANDS = (
    ("start", "Buka menu pemantauan"),
    ("help", "Panduan perintah"),
    ("status", "Status bot dan akun"),
    ("ringkasan", "Ringkasan akun paper atau live"),
    ("saldo", "Saldo dan nilai akun"),
    ("posisi", "Posisi pasar"),
    ("aktivitas", "Aktivitas trading bot"),
    ("scan", "Scan pasar sekarang tanpa order"),
    ("scan_last", "Laporan siklus bot terakhir"),
    ("risiko", "Batas risiko strategi"),
    ("grafik", "Grafik ekuitas paper atau live"),
    ("paper_once", "Jalankan satu siklus simulasi"),
    ("paper_on", "Aktifkan simulasi berkala"),
    ("paper_off", "Hentikan simulasi berkala"),
    ("live_on", "Aktifkan trading uang asli"),
    ("live_off", "Hentikan trading live"),
    ("notifikasi_on", "Aktifkan notifikasi"),
    ("notifikasi_off", "Matikan notifikasi"),
)


class TelegramError(RuntimeError):
    """A safe message without API URLs, credentials, or upstream descriptions."""

    def __init__(self, message: str, *, code: int | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Raising stops urllib before it can forward a URL containing the token.
        raise HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def _error(code: int | None, body: Any = None) -> TelegramError:
    messages = {
        400: "Telegram menolak parameter permintaan.",
        401: "Token Telegram tidak valid atau sudah dicabut.",
        403: "Telegram menolak akses ke chat; periksa /start dan pemblokiran bot.",
        404: "Endpoint atau token Telegram tidak ditemukan.",
        409: "Polling Telegram bentrok; hentikan proses bot lain atau webhook.",
        429: "Batas permintaan Telegram tercapai; tunggu sebelum mencoba kembali.",
    }
    message = messages.get(code, "Telegram tidak dapat memproses permintaan.")
    if code is not None and 300 <= code < 400:
        message = "Pengalihan alamat Telegram ditolak."
    elif code is not None and code >= 500:
        message = "Layanan Telegram sedang tidak tersedia."
    retry_after = None
    parameters = body.get("parameters") if isinstance(body, dict) else None
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if type(value) is int and value >= 0:
        retry_after = value
    return TelegramError(message, code=code, retry_after=retry_after)


def _decode(raw: bytes) -> dict:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TelegramError("Respons Telegram melebihi batas ukuran.")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise TelegramError("Format respons Telegram tidak valid.") from None
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise TelegramError("Format respons Telegram tidak valid.")
    return result


def _truncate_text(text: str, max_units: int = MAX_TEXT_UNITS) -> str:
    # Count UTF-16 units conservatively; never split a supplementary character.
    result: list[str] = []
    units = 0
    for char in text:
        if 0xD800 <= ord(char) <= 0xDFFF:
            char = "\ufffd"
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > max_units:
            while units > max_units - 1:
                units -= 2 if ord(result.pop()) > 0xFFFF else 1
            return "".join(result) + "\u2026"
        result.append(char)
        units += width
    return "".join(result)


class TelegramAPI:
    def __init__(self, token: str, *, opener: Callable | None = None):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise TelegramError("TELEGRAM_BOT_TOKEN belum diisi atau formatnya tidak valid.")
        self._token = token
        self._open = opener if opener is not None else build_opener(_NoRedirect()).open

    def call(self, method: str, payload: dict | None = None, timeout: float = 35) -> Any:
        if not isinstance(method, str) or not _METHOD.fullmatch(method):
            raise TelegramError("Nama metode Telegram tidak valid.")
        if payload is not None and not isinstance(payload, dict):
            raise TelegramError("Parameter Telegram harus berupa objek.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise TelegramError("Batas waktu Telegram tidak valid.")
        try:
            body = json.dumps(payload or {}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise TelegramError("Parameter Telegram tidak dapat diserialisasi.") from None
        return self._request(method, body, "application/json; charset=utf-8", timeout)

    def _request(self, method: str, body: bytes, content_type: str, timeout: float) -> Any:
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": content_type, "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._open(request, timeout=timeout) as response:
                response_body = _decode(response.read(MAX_RESPONSE_BYTES + 1))
        except HTTPError as exc:
            code = exc.code if type(exc.code) is int else None
            try:
                error_body = _decode(exc.read(MAX_RESPONSE_BYTES + 1))
            except (TelegramError, OSError, ValueError, HTTPException):
                error_body = None
            finally:
                exc.close()
            raise _error(code, error_body) from None
        except (URLError, OSError, HTTPException):
            raise TelegramError("Koneksi Telegram tidak tersedia; periksa koneksi internet.") from None
        if response_body["ok"] is not True:
            code = response_body.get("error_code")
            raise _error(code if type(code) is int else None, response_body)
        if "result" not in response_body:
            raise TelegramError("Hasil respons Telegram tidak tersedia.")
        return response_body["result"]

    def get_me(self) -> dict:
        result = self.call("getMe")
        if not isinstance(result, dict):
            raise TelegramError("Format profil bot Telegram tidak valid.")
        return result

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list:
        if offset is not None and type(offset) is not int:
            raise TelegramError("Offset Telegram tidak valid.")
        if type(timeout) is not int or not 0 <= timeout <= 50:
            raise TelegramError("Batas waktu polling Telegram harus 0 sampai 50 detik.")
        payload = {"timeout": timeout, "limit": 100, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout=timeout + 10)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramError("Format update Telegram tidak valid.")
        return result

    def send_message(self, chat_id: int, text: str, keyboard: bool = False) -> dict:
        if type(chat_id) is not int or chat_id == 0:
            raise TelegramError("ID chat Telegram tidak valid.")
        if not isinstance(text, str) or not text.strip():
            raise TelegramError("Pesan Telegram tidak boleh kosong.")
        payload = {
            "chat_id": chat_id,
            "text": _truncate_text(text),
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard:
            payload["reply_markup"] = {
                "keyboard": [["/ringkasan paper", "/ringkasan live"],
                             ["/posisi paper", "/posisi live"],
                             ["/scan paper", "/scan live"],
                             ["/paper_on", "/paper_off"], ["/live_on", "/live_off"],
                             ["/status", "/help"]],
                "resize_keyboard": True,
                "is_persistent": True,
            }
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramError("Format hasil pengiriman Telegram tidak valid.")
        return result

    def send_photo(self, chat_id: int, png: bytes, caption: str = "") -> dict:
        if type(chat_id) is not int or chat_id == 0:
            raise TelegramError("ID chat Telegram tidak valid.")
        if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise TelegramError("Grafik Telegram harus berupa gambar PNG.")
        if len(png) > MAX_PHOTO_BYTES:
            raise TelegramError("Ukuran grafik Telegram melebihi batas 5 MB.")
        if not isinstance(caption, str):
            raise TelegramError("Keterangan grafik Telegram tidak valid.")
        boundary = "polybot-" + secrets.token_hex(16)
        parts = []
        for name, value in (("chat_id", str(chat_id)), ("caption", _truncate_text(caption, 1024))):
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                          f'\r\n\r\n{value}\r\n').encode("utf-8"))
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
                      'filename="equity.png"\r\nContent-Type: image/png\r\n\r\n').encode("ascii"))
        parts.extend((png, f"\r\n--{boundary}--\r\n".encode("ascii")))
        result = self._request("sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}", 35)
        if not isinstance(result, dict):
            raise TelegramError("Format hasil pengiriman grafik Telegram tidak valid.")
        return result

    def set_commands(self) -> bool:
        result = self.call("setMyCommands", {"commands": [
            {"command": command, "description": description} for command, description in COMMANDS
        ]})
        if result is not True:
            raise TelegramError("Daftar perintah Telegram tidak berhasil disimpan.")
        return True
