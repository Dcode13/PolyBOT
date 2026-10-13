import io
import json
import unittest
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from polybot.telegram_api import (
    COMMANDS, MAX_PHOTO_BYTES, MAX_RESPONSE_BYTES, TelegramAPI, TelegramError, _NoRedirect,
)


TOKEN = "123456:abcdefghijklmnopqrstuv_example_token"
PNG = b"\x89PNG\r\n\x1a\nexample-picture-data"


def response(result):
    return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())


class TelegramAPITests(unittest.TestCase):
    def make_api(self, result=None):
        opener = Mock(return_value=response(result if result is not None else {"message_id": 1}))
        return TelegramAPI(TOKEN, opener=opener), opener

    def test_get_me_posts_json_to_fixed_https_host(self):
        api, opener = self.make_api({"id": 123, "is_bot": True})
        self.assertEqual(api.get_me()["id"], 123)
        req = opener.call_args.args[0]
        self.assertEqual(req.full_url, f"https://api.telegram.org/bot{TOKEN}/getMe")
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.data), {})
        self.assertEqual(opener.call_args.kwargs["timeout"], 35)

    def test_bad_token_does_not_echo_value(self):
        invalid = ["", None, 123, "1:short", " 1:" + "a" * 30,
                   "1:" + "a" * 30 + "/", "https://evil.test/token",
                   "١:" + "a" * 30, "1:" + "a" * 30 + "\n"]
        for token in invalid:
            with self.subTest(token_type=type(token).__name__), self.assertRaises(TelegramError) as caught:
                TelegramAPI(token)
            self.assertEqual(str(caught.exception), "TELEGRAM_BOT_TOKEN belum diisi atau formatnya tidak valid.")

    def test_default_opener_blocks_redirects(self):
        with patch("polybot.telegram_api.build_opener") as factory:
            TelegramAPI(TOKEN)
        self.assertIsInstance(factory.call_args.args[0], _NoRedirect)
        handler = _NoRedirect()
        with self.assertRaises(HTTPError):
            handler.redirect_request(Request("https://api.telegram.org/botTOKEN/getMe"),
                                     io.BytesIO(), 302, "moved", {}, "https://evil.test")

    def test_unsafe_method_and_payload_never_make_request(self):
        api, opener = self.make_api()
        for method in ["../getMe", "https://evil.test", "getMe?token=x", "", "getMe\n"]:
            with self.assertRaises(TelegramError):
                api.call(method)
        for payload in [[], "value", {"value": float("nan")}, {"value": object()}]:
            with self.assertRaises(TelegramError):
                api.call("getMe", payload)
        for timeout in [True, -1, 0, float("nan"), float("inf"), "1"]:
            with self.assertRaises(TelegramError):
                api.call("getMe", timeout=timeout)
        opener.assert_not_called()

    def test_updates_passes_offset_and_long_poll_margin(self):
        api, opener = self.make_api([{"update_id": 51}])
        self.assertEqual(api.get_updates(51, timeout=25), [{"update_id": 51}])
        self.assertEqual(json.loads(opener.call_args.args[0].data), {
            "offset": 51, "timeout": 25, "limit": 100, "allowed_updates": ["message"],
        })
        self.assertEqual(opener.call_args.kwargs["timeout"], 35)

    def test_updates_omits_unset_offset_and_allows_negative_api_offset(self):
        api, opener = self.make_api([])
        api.get_updates(timeout=0)
        self.assertNotIn("offset", json.loads(opener.call_args.args[0].data))
        opener.return_value = response([])
        api.get_updates(-1)
        self.assertEqual(json.loads(opener.call_args.args[0].data)["offset"], -1)

    def test_updates_rejects_invalid_arguments_and_result(self):
        api, opener = self.make_api()
        for offset, timeout in [(True, 25), ("123", 25), (None, True), (None, -1), (None, 51)]:
            with self.assertRaises(TelegramError):
                api.get_updates(offset, timeout)
        opener.assert_not_called()
        for result in [{}, ["bad update"]]:
            opener.return_value = response(result)
            with self.assertRaises(TelegramError):
                api.get_updates()

    def test_send_plain_text_keyboard_without_parse_mode(self):
        api, opener = self.make_api()
        api.send_message(123, "<market>& [not a link]", keyboard=True)
        data = json.loads(opener.call_args.args[0].data)
        self.assertEqual(data["text"], "<market>& [not a link]")
        self.assertEqual(data["chat_id"], 123)
        self.assertNotIn("parse_mode", data)
        self.assertTrue(data["link_preview_options"]["is_disabled"])
        self.assertIn(["/live_on", "/live_off"], data["reply_markup"]["keyboard"])
        self.assertIn(["/ringkasan paper", "/ringkasan live"], data["reply_markup"]["keyboard"])
        opener.assert_called_once()

    def test_send_caps_unicode_without_splitting_character(self):
        for text in ["a" * 5000, "\U0001f4b0" * 3000, "a" * 3799 + "\U0001f4b0", "\ud800example"]:
            api, opener = self.make_api()
            api.send_message(123, text)
            result = json.loads(opener.call_args.args[0].data)["text"]
            self.assertLessEqual(len(result.encode("utf-16-le")) // 2, 3800)
            self.assertLessEqual(len(result), 3800)
            result.encode("utf-8", "strict")
            if len(text.encode("utf-16-le", "replace")) // 2 > 3800:
                self.assertTrue(result.endswith("\u2026"))

    def test_send_validation_does_not_hit_network(self):
        api, opener = self.make_api()
        for chat_id, text in [(True, "hello"), (0, "hello"), ("123", "hello"), (123, ""), (123, "  "), (123, None)]:
            with self.assertRaises(TelegramError):
                api.send_message(chat_id, text)
        opener.assert_not_called()

    def test_malformed_json_and_shapes_are_safe(self):
        bodies = [b"<html>" + TOKEN.encode(), b"\xff", b"[]", b'{}', b'{"ok":"true","result":1}',
                  b'{"ok":true}', b'{"ok":true,"result":[]}' + b"x", b"[" * 5000]
        for body in bodies:
            api = TelegramAPI(TOKEN, opener=Mock(return_value=io.BytesIO(body)))
            with self.assertRaises(TelegramError) as caught:
                api.call("getMe")
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_response_size_limit(self):
        api = TelegramAPI(TOKEN, opener=Mock(return_value=io.BytesIO(b"x" * (MAX_RESPONSE_BYTES + 1))))
        with self.assertRaisesRegex(TelegramError, "ukuran"):
            api.get_me()

    def test_server_errors_never_include_description_or_token(self):
        for code in [400, 401, 403, 404, 409, 429, 500, 503, 302]:
            raw = json.dumps({"ok": False, "error_code": code, "description": TOKEN,
                              "parameters": {"retry_after": 45}}).encode()
            opener = Mock(side_effect=HTTPError(f"https://api.telegram.org/bot{TOKEN}/getMe", code,
                                               TOKEN, {}, io.BytesIO(raw)))
            api = TelegramAPI(TOKEN, opener=opener)
            with self.assertRaises(TelegramError) as caught:
                api.get_me()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.retry_after, 45)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn("https:", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            opener.assert_called_once()

    def test_200_api_error_and_retry_after_validation(self):
        for retry in [True, "5", -10, {}, None]:
            raw = json.dumps({"ok": False, "error_code": 429, "description": TOKEN,
                              "parameters": {"retry_after": retry}}).encode()
            api = TelegramAPI(TOKEN, opener=Mock(return_value=io.BytesIO(raw)))
            with self.assertRaises(TelegramError) as caught:
                api.get_me()
            self.assertEqual(caught.exception.code, 429)
            self.assertIsNone(caught.exception.retry_after)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_non_json_http_error_still_retains_code(self):
        api = TelegramAPI(TOKEN, opener=Mock(side_effect=HTTPError("https://hidden", 401, TOKEN, {}, io.BytesIO(TOKEN.encode()))))
        with self.assertRaises(TelegramError) as caught:
            api.get_me()
        self.assertEqual(caught.exception.code, 401)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_network_timeout_and_partial_response_safe_without_send_retry(self):
        for error in [URLError(TOKEN), TimeoutError(TOKEN), OSError(TOKEN), IncompleteRead(TOKEN.encode())]:
            opener = Mock(side_effect=error)
            api = TelegramAPI(TOKEN, opener=opener)
            with self.assertRaises(TelegramError) as caught:
                api.send_message(123, "hello")
            self.assertIsNone(caught.exception.code)
            self.assertNotIn(TOKEN, str(caught.exception))
            opener.assert_called_once()

    def test_send_photo_uses_multipart_fixed_filename_and_cap_caption(self):
        api, opener = self.make_api()
        api.send_photo(123, PNG, "\U0001f4b0" * 900)
        request = opener.call_args.args[0]
        content_type = request.get_header("Content-type")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary=polybot-"))
        self.assertTrue(request.full_url.endswith("/sendPhoto"))
        self.assertIn(b'filename="equity.png"', request.data)
        self.assertIn(b"Content-Type: image/png", request.data)
        self.assertIn(PNG, request.data)
        self.assertNotIn(b"parse_mode", request.data)
        caption = request.data.split(b'name="caption"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0].decode()
        self.assertLessEqual(len(caption.encode("utf-16-le")) // 2, 1024)
        self.assertTrue(caption.endswith("\u2026"))
        opener.assert_called_once()

    def test_send_photo_validation_and_no_retry(self):
        api, opener = self.make_api()
        for chat_id, png, caption in [(True, PNG, ""), (123, b"not png", ""),
                                     (123, PNG + b"a" * MAX_PHOTO_BYTES, ""), (123, PNG, None)]:
            with self.assertRaises(TelegramError):
                api.send_photo(chat_id, png, caption)
        opener.assert_not_called()
        opener.side_effect = TimeoutError(TOKEN)
        with self.assertRaises(TelegramError):
            api.send_photo(123, PNG)
        opener.assert_called_once()

    def test_result_types_are_checked(self):
        for operation in [lambda api: api.get_me(), lambda api: api.send_message(123, "hello"),
                          lambda api: api.send_photo(123, PNG), lambda api: api.set_commands()]:
            api, _ = self.make_api([])
            with self.assertRaises(TelegramError):
                operation(api)

    def test_set_commands_lists_expected_actions(self):
        api, opener = self.make_api(True)
        self.assertTrue(api.set_commands())
        commands = json.loads(opener.call_args.args[0].data)["commands"]
        self.assertEqual({item["command"] for item in commands}, {name for name, _ in COMMANDS})
        self.assertTrue({"live_on", "live_off", "ringkasan", "grafik", "risiko"}.issubset({name for name, _ in COMMANDS}))


if __name__ == "__main__":
    unittest.main()
