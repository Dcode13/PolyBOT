from __future__ import annotations

import unittest

from polybot.selection import next_scan_cursor, select_markets


def market(index, outcome="Yes", *, event=None, market_id=None):
    return {"token_id": str(index), "outcome": outcome,
            "market_id": str(index) if market_id is None else market_id,
            "event_id": str(index) if event is None else event}


def pair(index):
    return [market(f"{index}-yes", "Yes", event=str(index), market_id=str(index)),
            market(f"{index}-no", "No", event=str(index), market_id=str(index))]


def tokens(rows):
    return [row["token_id"] for row in rows]


class SelectionTests(unittest.TestCase):
    def test_pairs_nonadjacent_outcomes_without_assuming_yes_is_first(self):
        a_yes, a_no = pair("a")
        b_yes, b_no = pair("b")
        rows = [a_no, b_yes, a_yes, b_no]
        result = select_markets(rows, 0, 4, False)
        self.assertEqual(["a-no", "a-yes", "b-yes", "b-no"], tokens(result))
        self.assertIs(a_no, result[0])
        self.assertEqual(["a-no", "b-yes", "a-yes", "b-no"], tokens(rows))

    def test_sequential_cursor_resumes_second_outcome_after_short_cycle(self):
        rows = pair("a") + pair("b")
        self.assertEqual(["a-yes"], tokens(select_markets(rows, 0, 1, False)))
        self.assertEqual(["a-no"], tokens(select_markets(rows, 1, 1, False)))
        self.assertEqual(["a-no", "b-yes", "b-no"],
                         tokens(select_markets(rows, 1, 3, False)))
        self.assertEqual(["b-no", "a-yes", "a-no", "b-yes"],
                         tokens(select_markets(rows, 3, 4, False)))

    def test_odd_budget_only_truncates_the_last_pair(self):
        rows = pair("a") + pair("b") + pair("c")
        self.assertEqual(["a-yes", "a-no", "b-yes"],
                         tokens(select_markets(rows, 0, 3, False)))
        self.assertEqual(["a-yes", "a-no", "c-yes"],
                         tokens(select_markets(rows, 0, 3, True)))

    def test_spread_keeps_pairs_and_reaches_the_far_horizon(self):
        rows = [row for i in range(20) for row in pair(i)]
        result = select_markets(rows, 0, 6, True)
        self.assertEqual(["0-yes", "0-no", "1-yes", "1-no", "19-yes", "19-no"],
                         tokens(result))
        self.assertEqual(6, len({row["token_id"] for row in result}))

    def test_singleton_spread_preserves_previous_sampling(self):
        rows = [market(i) for i in range(20)]
        self.assertEqual(["0", "1", "2", "7", "13", "19"],
                         tokens(select_markets(rows, 0, 6, True)))
        self.assertEqual(["3", "4", "5", "10", "16", "2"],
                         tokens(select_markets(rows, 3, 6, True)))

    def test_invalid_pairs_remain_in_original_singleton_order(self):
        for outcomes in (["Yes", "No", "Maybe"], ["Yes", "yes"], ["Yes", ""]):
            with self.subTest(outcomes=outcomes):
                same_market = [market(f"a-{i}", value, event="a", market_id="a")
                               for i, value in enumerate(outcomes)]
                rows = [same_market[0], market("other"), *same_market[1:]]
                self.assertEqual(tokens(rows), tokens(select_markets(rows, 0, 20, False)))
        rows = [market("a", "Yes", event="1", market_id="shared"), market("middle"),
                market("b", "No", event="2", market_id="shared")]
        self.assertEqual(tokens(rows), tokens(select_markets(rows, 0, 20, False)))

    def test_named_binary_outcomes_are_paired(self):
        first = market("one", "Team A", event="game", market_id="game")
        second = market("two", "Team B", event="game", market_id="game")
        self.assertEqual(["one", "two", "other"],
                         tokens(select_markets([first, market("other"), second], 0, 3, False)))

    def test_duplicate_tokens_do_not_consume_budget_twice(self):
        rows = pair("a")
        self.assertEqual(["a-yes", "a-no"],
                         tokens(select_markets([rows[0], rows[0], rows[1]], 0, 5, True)))

    def test_mixed_groups_fill_budget_without_losing_far_sample(self):
        rows = pair("a") + [market("b"), market("c")] + pair("d") + [market("last")]
        result = select_markets(rows, 0, 6, True)
        self.assertEqual(6, len(result))
        self.assertEqual(6, len(set(tokens(result))))
        self.assertEqual(["a-yes", "a-no"], tokens(result[:2]))
        self.assertIn("last", tokens(result))

    def test_cursor_wrap_and_small_limits_are_bounded(self):
        rows = pair("a") + pair("b")
        for spread in (False, True):
            with self.subTest(spread=spread):
                self.assertEqual(["a-no"], tokens(select_markets(rows, 5, 1, spread)))
                self.assertEqual(["b-no"], tokens(select_markets(rows, -1, 1, spread)))
                self.assertEqual([], select_markets(rows, 0, 0, spread))
                self.assertEqual([], select_markets([], 0, 6, spread))

    def test_cursor_resumes_unprocessed_sibling_after_interrupted_spread(self):
        rows = [row for i in range(20) for row in pair(i)]
        selected = select_markets(rows, 0, 6, True)
        analyzed = selected[:5]
        cursor = next_scan_cursor(rows, 0, analyzed)
        self.assertEqual(39, cursor)
        self.assertEqual(["19-no"], tokens(select_markets(rows, cursor, 1, True)))

    def test_cursor_resumes_odd_limit_partial_pair_with_reversed_outcomes(self):
        rows = pair("a") + list(reversed(pair("b")))
        selected = select_markets(rows, 0, 3, False)
        cursor = next_scan_cursor(rows, 0, selected)
        self.assertEqual(3, cursor)
        self.assertEqual(["b-yes"], tokens(select_markets(rows, cursor, 1, False)))

    def test_one_token_cycles_finish_each_pair_then_reach_later_markets(self):
        rows = pair("a") + pair("b")
        cursor, examined = 0, []
        for _ in range(4):
            selected = select_markets(rows, cursor, 1, True)
            examined.extend(tokens(selected))
            cursor = next_scan_cursor(rows, cursor, selected)
        self.assertEqual(["a-yes", "a-no", "b-yes", "b-no"], examined)
        self.assertEqual(4, cursor)

    def test_completed_spread_and_previously_examined_sibling_keep_rotation(self):
        rows = [row for i in range(20) for row in pair(i)]
        selected = select_markets(rows, 0, 6, True)
        self.assertEqual(6, next_scan_cursor(rows, 0, selected))
        # The matching sibling may have appeared earlier in the analyzed list.
        self.assertEqual(9, next_scan_cursor(rows, 7, [rows[1], rows[0]]))

    def test_cursor_does_not_rewind_on_empty_singleton_or_malformed_groups(self):
        rows = [market(i, "Yes", event="shared", market_id="shared") for i in range(3)]
        self.assertEqual(8, next_scan_cursor(rows, 8, []))
        self.assertEqual(9, next_scan_cursor(rows, 8, rows[:1]))
        self.assertEqual(9, next_scan_cursor([market("single")], 8, [market("single")]))


if __name__ == "__main__":
    unittest.main()
