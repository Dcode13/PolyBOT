from __future__ import annotations

import unittest
from unittest.mock import patch

from polybot.telegram_views import chart_png, format_fresh_scan, format_snapshot, format_status, format_trade_action


def snapshot(mode="paper"):
    return {
        "mode": mode, "generatedAt": "2026-10-10T12:00:00+00:00",
        "account": {"connected": True, "cash": 8.5, "equity": 9.75, "positionsValue": 1.25,
                    "totalPnl": -1.25, "realizedPnl": None, "unrealizedPnl": -0.25,
                    "activePositionsCount": 1, "positionsComplete": True, "historyComplete": True,
                    "equityAsOf": "2026-10-10T11:00:00Z", "wallet": "0x" + "1" * 40},
        "bot": {"lastScanAt": "2026-10-09T10:00:00Z", "status": "recent_scan",
                "ledgerAvailable": True, "lastScan": {"scanned": 120}, "scanned": 120,
                "analyzed": 8, "qualifiedCount": 0, "pendingCount": 0, "rejections": []},
        "risk": {"pollSeconds": 60, "takeProfitRoi": 0.5, "growthMinProbability": 0.9},
        "positions": [], "trades": [], "equityHistory": [], "errors": [],
    }


class TelegramViewsTests(unittest.TestCase):
    def test_trade_action_distinguishes_no_entry_from_selling_no_inventory(self):
        self.assertEqual("ENTRY NO (BUY No)", format_trade_action("BUY", "No"))
        self.assertEqual("EXIT NO (SELL No)", format_trade_action("SELL", "No"))
        self.assertEqual("ENTRY YES (BUY Yes)", format_trade_action("BUY", "YES"))
        self.assertEqual("EXIT YES (SELL Yes)", format_trade_action("SELL", "Yes"))
        self.assertEqual("ENTRY Team B (BUY)", format_trade_action("BUY", "Team\nB\u202e"))
        self.assertEqual("BUY", format_trade_action("BUY", None))
        self.assertEqual("-", format_trade_action("secret", "No"))

    def test_activity_renders_buy_no_and_sell_no_as_separate_actions(self):
        data = snapshot()
        data["trades"] = [
            {"title": "Will the event happen?", "outcome": "No", "side": "BUY", "cash": -1},
            {"title": "Will the event happen?", "outcome": "No", "side": "SELL", "cash": 1.5},
        ]
        text = format_snapshot(data, "activity")
        self.assertIn("1. ENTRY NO (BUY No)", text)
        self.assertIn("2. EXIT NO (SELL No)", text)
        self.assertNotIn("ENTRY NO (SELL", text)
        self.assertIn("Kas -$1.00", text)
        self.assertIn("Kas $1.50", text)

    def test_fresh_scan_marks_assumed_capital_and_non_trading_scope(self):
        report = {"mode": "paper", "time": "2026-10-12T08:15:00Z", "scan_only": True,
                  "scanned": 240, "analyzed": 12, "qualified": [], "rejections": {}, "errors": [],
                  "capital_basis": "configured_starting_cash", "analysis_cash": 11, "analysis_entry": 1}
        text = format_fresh_scan(report)
        self.assertIn("SCAN PASAR TERBARU", text)
        self.assertIn("Aturan strategi: PAPER | Data pasar publik", text)
        self.assertIn("12 Oct 2026 08:15:00 UTC", text)
        self.assertIn("Outcome dipindai: 240", text)
        self.assertIn("Kandidat dianalisis: 12", text)
        self.assertIn("Kandidat lolos: 0", text)
        self.assertIn("Modal asumsi konfigurasi: $11.00", text)
        self.assertIn("Entry asumsi: $1.00", text)
        self.assertIn("Saldo dan posisi akun belum diperiksa", text)
        self.assertIn("tidak mengirim order", text)
        self.assertIn("Belum ada kandidat yang lolos", text)
        self.assertNotIn("Aturan LIVE memerlukan", text)

    def test_fresh_scan_candidates_show_probability_target_and_horizon(self):
        report = {"mode": "live", "qualified": [
            {"question": "Will the team win?", "outcome": "Yes", "budget": 1,
             "target_price": 0.45, "probability_lower": 0.825, "long_term": False},
            {"question": "Will it pass next month?", "outcome": "No", "budget": 2,
             "target_price": 0.65, "probability_lower": 0.915, "long_term": True},
        ]}
        text = format_fresh_scan(report)
        self.assertIn("Aturan strategi: LIVE", text)
        self.assertIn("memerlukan forecast eksternal terkalibrasi", text)
        self.assertIn("1. Will the team win?", text)
        self.assertIn("Pilihan: Yes | Budget: $1.00", text)
        self.assertIn("Target harga jual: $0.4500", text)
        self.assertIn("82.5% | Horizon: <=24 jam", text)
        self.assertIn("91.5% | Horizon: >24 jam", text)
        self.assertIn("bukan jaminan menang", text)

    def test_paper_exploration_has_no_invented_probability_or_forecast_target(self):
        text = format_fresh_scan({"mode": "paper", "qualified": [
            {"entry_kind": "paper_exploratory", "question": "Paper execution trial", "budget": 1,
             "outcome": "Yes", "target_price": .3, "probability_lower": .99, "edge": .8},
        ]})
        self.assertIn("Kandidat lolos: 0", text)
        self.assertIn("Kandidat eksplorasi paper: 1", text)
        self.assertIn("SIMULASI EKSPLORASI · probabilitas belum tersedia", text)
        self.assertIn("Target exit simulasi: $0.3000", text)
        self.assertIn("sasaran profit, bukan forecast harga", text)
        self.assertIn("belum menjadi sinyal entry uang asli", text)
        self.assertNotIn("Probabilitas model", text)
        self.assertNotIn("99.0%", text)
        self.assertNotIn("80.0%", text)

    def test_mixed_scan_counts_forecast_and_exploration_separately(self):
        text = format_fresh_scan({"mode": "paper", "qualified": [
            {"entry_kind": "forecast", "question": "Forecast candidate", "probability_lower": .8},
            {"entry_kind": "paper_exploratory", "question": "Exploratory candidate"},
        ]})
        self.assertIn("Kandidat lolos: 1", text)
        self.assertIn("Kandidat eksplorasi paper: 1", text)
        self.assertIn("80.0%", text)
        self.assertIn("Forecast candidate", text)
        self.assertIn("Exploratory candidate", text)

    def test_live_scan_does_not_render_paper_exploration_as_a_buy_signal(self):
        text = format_fresh_scan({"mode": "live", "qualified": [
            {"entry_kind": "paper_exploratory", "question": "Paper-only candidate", "probability_lower": .99},
        ]})
        self.assertIn("Kandidat lolos: 0", text)
        self.assertIn("Aturan LIVE memerlukan forecast eksternal terkalibrasi", text)
        self.assertNotIn("Paper-only candidate", text)
        self.assertNotIn("99.0%", text)
        self.assertNotIn("Kandidat eksplorasi paper", text)

    def test_exploratory_position_and_activity_labels_are_paper_ledger_only(self):
        for section, field in (("positions", "positions"), ("activity", "trades")):
            data = snapshot()
            data[field] = [{"title": "Execution trial", "entryKind": "paper_exploratory", "source": "ledger"}]
            self.assertIn("SIMULASI EKSPLORASI · probabilitas belum tersedia", format_snapshot(data, section))
            data["mode"] = "live"
            self.assertNotIn("SIMULASI EKSPLORASI", format_snapshot(data, section))
            data["mode"] = "paper"
            data[field][0]["source"] = "polymarket"
            self.assertNotIn("SIMULASI EKSPLORASI", format_snapshot(data, section))

    def test_exploration_messages_preserve_3800_utf16_limit(self):
        rows = [{"question": "\U0001f600" * 200, "outcome": "\U0001f600" * 100,
                 "entry_kind": "paper_exploratory", "entryKind": "paper_exploratory", "source": "ledger",
                 "title": "\U0001f600" * 200} for _ in range(12)]
        texts = [format_fresh_scan({"mode": "paper", "qualified": rows})]
        data = snapshot()
        data.update(positions=rows, trades=rows)
        texts.extend(format_snapshot(data, section) for section in ("positions", "activity"))
        for text in texts:
            self.assertLessEqual(len(text.encode("utf-16-le")), 7600)
            self.assertNotIn("\ufffd", text)

    def test_fresh_scan_limits_candidates_and_cleans_market_titles(self):
        report = {"qualified": [{"question": f"Market {i}\n\x00\u202e" + "x" * 200,
                                  "outcome": "YES\r\nFORGED"} for i in range(7)]}
        text = format_fresh_scan(report)
        self.assertIn("Kandidat lolos: 7", text)
        self.assertIn("Menampilkan 5 dari 7 kandidat", text)
        self.assertIn("Market 4", text)
        self.assertNotIn("Market 5", text)
        self.assertNotIn("x" * 101, text)
        self.assertNotIn("\x00", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\nFORGED", text)

    def test_fresh_scan_rejections_are_ranked_labeled_and_never_echo_unknown_codes(self):
        report = {"rejections": {"low_liquidity": 20, "no_forecast": 3,
                                  "entry_below_exchange_minimum": 10,
                                  "secret-private-token": 2, "another-private-error": 3,
                                  "crypto_excluded": 0, "invalid_market": -1,
                                  "unknown_fee": float("nan")}}
        text = format_fresh_scan(report)
        self.assertLess(text.index("Likuiditas terlalu rendah: 20"),
                        text.index("Budget di bawah minimum order: 10"))
        self.assertLess(text.index("Alasan lain: 5"), text.index("Forecast belum tersedia: 3"))
        self.assertNotIn("secret-private-token", text)
        self.assertNotIn("another-private-error", text)
        self.assertNotIn("Pasar crypto dikecualikan", text)
        self.assertNotIn("Data pasar tidak valid", text)
        self.assertNotIn("Biaya belum tersedia", text)

    def test_fresh_scan_partial_errors_and_halts_do_not_expose_diagnostics(self):
        report = {"analysis_budget_reached": True, "halted": "secret-diagnostic",
                  "errors": [{"error": "secret-diagnostic"}, "secret-diagnostic"],
                  "private_key": "secret-diagnostic", "orders": [{"detail": "secret-diagnostic"}]}
        text = format_fresh_scan(report)
        self.assertIn("Hasil parsial: batas waktu analisis tercapai", text)
        self.assertIn("tertahan pengaman", text)
        self.assertIn("Gangguan data: 2", text)
        self.assertNotIn("secret-diagnostic", text)

    def test_fresh_scan_unknown_or_invalid_values_are_not_invented(self):
        text = format_fresh_scan({"qualified": [{"probability_lower": 4.2, "long_term": None},
                                               {"probability_lower": float("nan")}],
                                  "analysis_cash": float("inf"), "analysis_entry": True})
        self.assertIn("Outcome dipindai: -", text)
        self.assertIn("Modal asumsi konfigurasi: -", text)
        self.assertIn("Entry asumsi: -", text)
        self.assertIn("Probabilitas model (batas bawah): - | Horizon: -", text)
        self.assertNotIn("420%", text)
        self.assertNotIn(": nan", text)
        self.assertIn("Belum ada kandidat", format_fresh_scan(None))

    def test_fresh_scan_uses_api_utf16_limit_including_truncation_suffix(self):
        report = {"analysis_cash": 1e308, "analysis_entry": 1e308,
                  "qualified": [{"question": "\U0001f600" * 200, "outcome": "\U0001f600" * 100,
                                  "budget": 1e308, "target_price": 1e308} for _ in range(10)],
                  "rejections": {"low_liquidity": 1e308, "no_forecast": 1e308},
                  "errors": [{"message": "secret"}]}
        text = format_fresh_scan(report)
        self.assertLessEqual(len(text.encode("utf-16-le")), 7600)
        self.assertTrue(text.endswith("Tampilan dipersingkat."))
        self.assertNotIn("\ufffd", text)

    def test_scan_watchlist_stays_unqualified_without_model_probability(self):
        text = format_fresh_scan({"qualified": [], "watchlist": [
            {"question": "Will the team win?", "outcome": "Yes", "ask": 0.23,
             "minimum_notional": 1.15, "reason": "entry_below_exchange_minimum",
             "probability_lower": 0.99, "target_price": 0.9},
        ]})
        self.assertIn("Kandidat lolos: 0", text)
        self.assertIn("Belum ada kandidat yang lolos", text)
        self.assertIn("DAFTAR PANTAUAN:", text)
        self.assertIn("BELUM LOLOS ENTRY", text)
        self.assertIn("Will the team win?", text)
        self.assertIn("Ask $0.2300 | Minimum nilai order $1.1500", text)
        self.assertIn("Belum entry: Budget di bawah minimum order", text)
        self.assertIn("harga bukan probabilitas model", text)
        self.assertNotIn("Probabilitas model (batas bawah):", text)
        self.assertNotIn("99.0%", text)
        self.assertNotIn("23.0%", text)
        self.assertNotIn("Target harga jual:", text)

    def test_scan_watchlist_hides_unknown_reasons_and_private_diagnostics(self):
        text = format_fresh_scan({"watchlist": [
            {"question": "Public market\n\u202e", "outcome": "No", "ask": 0.4,
             "minimum_notional": 2, "reason": "private-key-secret-diagnostic",
             "error": "private-key-secret-diagnostic", "token_id": "private-key-secret-diagnostic"},
        ]})
        self.assertIn("Syarat entry belum terpenuhi", text)
        self.assertNotIn("private-key-secret-diagnostic", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("Public market\n", text)

    def test_scan_profile_note_preserves_distinction_from_trading_settings(self):
        text = format_fresh_scan({"mode": "live", "scan_overrides": {"min_probability": 0.7}})
        self.assertIn("Profil scan diperluas", text)
        self.assertIn("syarat trading tetap mengikuti konfigurasi trading", text)
        self.assertIn("Aturan LIVE memerlukan forecast eksternal terkalibrasi", text)
        self.assertIn("Scan ini tidak mengirim order", text)

    def test_scan_combined_candidates_and_watchlist_fit_telegram_utf16_limit(self):
        report = {"mode": "live", "analysis_cash": 11, "analysis_entry": 1,
                  "scan_overrides": {"min_probability": 0.7}, "analysis_budget_reached": True,
                  "qualified": [{"question": "\U0001f600" * 200, "outcome": "\U0001f600" * 100,
                                  "budget": 1, "target_price": 0.5, "probability_lower": 0.8,
                                  "long_term": False} for _ in range(5)],
                  "watchlist": [{"question": "\U0001f600" * 200, "outcome": "\U0001f600" * 100,
                                 "ask": 0.25, "minimum_notional": 1.25,
                                 "reason": "entry_below_exchange_minimum"} for _ in range(10)],
                  "errors": [{"message": "secret"}], "rejections": {"low_liquidity": 120}}
        text = format_fresh_scan(report)
        self.assertLessEqual(len(text.encode("utf-16-le")), 7600)
        self.assertIn("Kandidat lolos: 5", text)
        self.assertIn("DAFTAR PANTAUAN:", text)
        self.assertIn("BELUM LOLOS ENTRY", text)
        self.assertTrue(text.endswith("Tampilan dipersingkat."))
        self.assertNotIn("\ufffd", text)

    def test_paper_summary_preserves_negative_and_missing_money(self):
        text = format_snapshot(snapshot())
        self.assertIn("PAPER · simulasi", text)
        self.assertIn("Ekuitas: $9.75", text)
        self.assertIn("PnL total paper: -$1.25", text)
        self.assertIn("PnL terealisasi: -\n", text)
        self.assertIn("09 Oct 2026 10:00:00 UTC (lama)", text)
        self.assertNotIn("0x" + "1" * 40, text)

    def test_real_account_explicitly_limits_pnl_scope(self):
        data = snapshot("live")
        data["account"]["historyComplete"] = False
        text = format_snapshot(data)
        self.assertIn("AKUN REAL", text)
        self.assertIn("PnL total posisi terlapor", text)
        self.assertIn("bukan seluruh arus kas wallet", text)
        self.assertIn("parsial", text)
        self.assertNotIn("PAPER", text)

    def test_missing_data_is_not_reported_as_zero(self):
        data = {"mode": "live", "account": {"positionsComplete": False, "activePositionsCount": 0}}
        text = format_snapshot(data)
        self.assertIn("Ekuitas: -\n", text)
        self.assertIn("Posisi aktif: -\n", text)
        self.assertNotIn("$0.00", text)
        self.assertIn("Data posisi belum tersedia", format_snapshot(data, "positions"))
        self.assertIn("Outcome dipindai: -", format_snapshot(data, "scan"))

    def test_whitelist_does_not_echo_errors_or_unknown_fields(self):
        data = snapshot()
        data["private_key"] = "secret123"
        data["account"]["credentials"] = "secret123"
        data["errors"] = [{"code": "connection_unavailable", "message": "secret123"},
                          {"code": "secret123", "message": "secret123"}]
        text = format_snapshot(data)
        self.assertNotIn("secret123", text)
        self.assertIn("Koneksi akun belum tersedia", text)

    def test_positions_truncate_titles_remove_controls_and_cap_rows(self):
        data = snapshot()
        data["positions"] = [{"title": "<b>market</b>\n\x00\u202e" + "x" * 200,
                              "outcome": "YES\r\nFORGED", "status": "OPEN", "value": None,
                              "pnl": -0.1, "pnlPercent": -10, "size": 3} for _ in range(14)]
        text = format_snapshot(data, "positions")
        self.assertIn("<b>market</b>", text)  # Plain text, caller does not set parse_mode.
        self.assertNotIn("\x00", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\nFORGED", text)
        self.assertNotIn("x" * 100, text)
        self.assertIn("10 dari 14", text)
        self.assertNotIn("11.", text)
        self.assertIn("Nilai - | PnL -$0.10 (-10%)", text)

    def test_telegram_utf16_message_limit_even_for_emoji(self):
        data = snapshot()
        data["positions"] = [{"title": "😀" * 200, "outcome": "😀" * 100,
                              "status": "😀" * 100} for _ in range(20)]
        data["errors"] = [{"code": code} for code in ["balance_unavailable", "orders_unavailable",
                          "connection_unavailable", "positions_unavailable", "closed_history_unavailable"]]
        self.assertLessEqual(len(format_snapshot(data, "positions").encode("utf-16-le")), 8192)

    def test_fractional_shares_are_not_rounded_to_whole_shares(self):
        data = snapshot()
        data["positions"] = [{"title": "Fractional", "size": 0.012345},
                             {"title": "Dust", "size": 0.0000001},
                             {"title": "Whole", "size": 1000}]
        text = format_snapshot(data, "positions")
        self.assertIn("Saham 0.012345", text)
        self.assertIn("Saham 1e-07", text)
        self.assertIn("Saham 1,000 |", text)

    def test_activity_scope_and_ten_row_cap(self):
        data = snapshot("live")
        data["trades"] = [{"title": f"Market {i}", "side": "BUY", "cash": 1,
                           "pnl": None, "time": "2026-10-10T00:01:00Z"} for i in range(15)]
        text = format_snapshot(data, "activity")
        self.assertIn("tidak mencakup seluruh transaksi manual", text)
        self.assertIn("10 aktivitas", text)
        self.assertIn("Market 9", text)
        self.assertNotIn("Market 10", text)
        self.assertIn("PnL -", text)

    def test_positions_pages_reach_last_position_without_duplicates(self):
        data = snapshot("live")
        data["positions"] = [{"title": f"Market {i}", "size": 1} for i in range(42)]
        first = format_snapshot(data, "positions")
        last = format_snapshot(data, "positions", page=5)
        self.assertIn("Halaman 1/5", first)
        self.assertIn("Berikutnya: /posisi live 2", first)
        self.assertNotIn("Market 10", first)
        self.assertIn("41. Market 40", last)
        self.assertIn("42. Market 41", last)
        self.assertIn("Sebelumnya: /posisi live 4", last)
        self.assertNotIn("Berikutnya:", last)
        self.assertIn("2 dari 42", last)

    def test_activity_pages_and_invalid_pages(self):
        data = snapshot()
        data["trades"] = [{"title": f"Trade {i}"} for i in range(21)]
        text = format_snapshot(data, "activity", page=2)
        self.assertIn("11. - · Trade 10", text)
        self.assertIn("20. - · Trade 19", text)
        self.assertIn("Berikutnya: /aktivitas paper 3", text)
        self.assertIn("Sebelumnya: /aktivitas paper 1", text)
        self.assertNotIn("Trade 20", text)
        out_of_range = format_snapshot(data, "activity", page=4)
        self.assertIn("Halaman 4 tidak tersedia", out_of_range)
        self.assertIn("/aktivitas paper 3", out_of_range)
        self.assertNotIn("Trade 0", out_of_range)
        for invalid in (0, -1, True, "2", None):
            with self.subTest(page=invalid), self.assertRaises(ValueError):
                format_snapshot(data, "activity", page=invalid)

    def test_risk_fraction_and_scan_status_are_not_misrepresented(self):
        data = snapshot()
        self.assertIn("Take profit ROI: 50%", format_snapshot(data, "risk"))
        self.assertIn("fase pertumbuhan: 90%", format_snapshot(data, "risk"))
        self.assertIn("bukan bukti proses masih berjalan", format_snapshot(data, "scan"))
        with self.assertRaises(ValueError):
            format_snapshot(data, "unknown")

    def test_status_only_uses_known_process_state(self):
        text = format_status({"paper": {"running": True, "once": True, "command": "secret"},
                              "live": {"running": True, "stopping": True, "error": "secret"}}, True)
        self.assertIn("PAPER: berjalan · satu siklus", text)
        self.assertIn("AKUN REAL: sedang berhenti", text)
        self.assertIn("Notifikasi: aktif", text)
        self.assertNotIn("secret", text)
        self.assertIn("PAPER: tidak berjalan", format_status({}, False))

    def test_chart_missing_invalid_and_single_observation(self):
        data = snapshot()
        self.assertIsNone(chart_png(data))
        data["equityHistory"] = [{"time": "2026-10-10T01:00:00Z", "equity": float("nan")},
                                 {"time": "unknown", "equity": 1}, {"time": "2026-10-10", "equity": 1}]
        self.assertIsNone(chart_png(data))
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("Optional chart dependency is not installed")
        data["equityHistory"].append({"time": "2026-10-10T01:00:00Z", "equity": 11})
        result = chart_png(data)
        self.assertIsInstance(result, bytes)
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(result), 1_000_000)

    def test_chart_duplicate_timestamps_keep_one_real_observation(self):
        try:
            from matplotlib.axes import Axes
        except ImportError:
            self.skipTest("Optional chart dependency is not installed")
        data = snapshot()
        data["equityHistory"] = [{"time": "2026-10-10T01:00:00Z", "equity": 11},
                                 {"time": "2026-10-10T01:00:00+00:00", "equity": 12}]
        with patch.object(Axes, "scatter", autospec=True, wraps=Axes.scatter) as scatter:
            self.assertIsNotNone(chart_png(data))
            self.assertEqual(scatter.call_count, 1)
            self.assertEqual(scatter.call_args.args[2], (12.0,))


if __name__ == "__main__":
    unittest.main()
