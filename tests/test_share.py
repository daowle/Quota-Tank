"""share.py: 공유 사용량 나누기 계산(합성 데이터. 실제 기록·네트워크를 쓰지 않는다)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import share

NOW = 2_000_000.0
PRICE = lambda model: (2, 10, 0.2, 2.5, 4) if model.startswith("claude-sonnet") else None


def ev(*items):
    return [(t, usd, "opus") for t, usd in items]


def data(**kw):
    d = {"u5": 30.0, "r5": NOW + 3600, "uw": 20.0, "rw": NOW + 86400, "plan": "Max", "src": "live", "observed_at": NOW}
    d.update(kw)
    return d


class Weights(unittest.TestCase):
    def test_rows_become_dollars_by_price(self):
        rows = [["2026-10-08", "claude-sonnet-5-5", 1_000_000, 0, 0, 0, 0, 10],
                ["2026-10-08", "unknown-model", 1_000_000, 0, 0, 0, 0, 5],
                ["2026-10-08", "claude-opus-5-5", 0, 0, 0, 0, 0, 7]]
        out = share.weigh(rows, PRICE)
        self.assertEqual([(t, round(usd, 3), fam) for t, usd, fam in out],
                         [(5, 3.0, "other"), (7, 0.0, "opus"), (10, 2.0, "sonnet")])

    def test_bad_rows_are_skipped(self):
        rows = [["d", "m", 1, 1, 1, 1, 1], ["d", "m", -1, 0, 0, 0, 0, 1], ["d", "m", "x", 0, 0, 0, 0, 1],
                ["d", "m", 1, 0, 0, 0, 0, float("nan")], None]
        self.assertEqual(share.weigh(rows, PRICE), [])

    def test_spent_counts_only_the_window(self):
        events = [(1, 1.0, "opus"), (5, 2.0, "opus"), (6, 4.0, "fable"), (11, 8.0, "opus")]
        m, by = share.spent(events, 5, 10)
        self.assertEqual((m, by), (6.0, {"opus": 2.0, "fable": 4.0}))


class Coefficient(unittest.TestCase):
    def test_base_scales_with_plan(self):
        self.assertEqual(share.pick_k({}, "pro", "h5"), (2.47, "base"))
        k, src = share.pick_k({}, "max5", "h5")
        self.assertAlmostEqual(k, 0.494)
        self.assertEqual(src, "base")
        self.assertAlmostEqual(share.pick_k({}, "max20", "week")[0], 0.0095)

    def test_plan_setting(self):
        self.assertEqual(share.resolve_plan("auto", "Pro"), "pro")
        self.assertEqual(share.resolve_plan("auto", "Max"), "max5")
        self.assertEqual(share.resolve_plan("auto", ""), "max5")
        self.assertEqual(share.resolve_plan("max20", "Pro"), "max20")
        self.assertEqual(share.resolve_plan("junk", "Pro"), "pro")

    def test_manual_beats_learned_beats_base(self):
        state = {}
        for _ in range(share.LEARN_MIN):
            state = share.learn(state, "max5", "h5", 20, 20)  # 비율 1.0
        self.assertEqual(share.pick_k(state, "max5", "h5"), (1.0, "learned"))
        state, ok = share.calibrate(state, "max5", "h5", 30, 20)  # 비율 1.5
        self.assertTrue(ok)
        self.assertEqual(share.pick_k(state, "max5", "h5"), (1.5, "manual"))

    def test_learning_needs_enough_windows_and_uses_low_quantile(self):
        state = {}
        for u in (10, 20, 30):
            state = share.learn(state, "pro", "h5", u, 10)
        self.assertEqual(share.pick_k(state, "pro", "h5")[1], "base")
        state = share.learn(state, "pro", "h5", 100, 10)  # 비율 1, 2, 3, 10
        k, src = share.pick_k(state, "pro", "h5")
        self.assertEqual(src, "learned")
        self.assertAlmostEqual(k, 1.6)  # 아래쪽 20% — 상대가 쓴 큰 값에 끌려가지 않는다

    def test_learning_ignores_tiny_windows_and_caps_history(self):
        state = share.learn({}, "pro", "h5", 4, 10)
        state = share.learn(state, "pro", "h5", 50, 0.1)
        self.assertEqual(state, {})
        for i in range(share.LEARN_MAX + 5):
            state = share.learn(state, "pro", "h5", 10 + i, 10)
        pairs = state["learned"]["pro"]["h5"]
        self.assertEqual(len(pairs), share.LEARN_MAX)
        self.assertEqual(pairs[0][0], 15)

    def test_calibrate_rejects_small_windows_and_sums_repeats(self):
        state, ok = share.calibrate({}, "max5", "h5", 9, 50)
        self.assertEqual((state, ok), ({}, False))
        state, ok = share.calibrate({}, "max5", "h5", 10, 5)
        self.assertTrue(ok)
        state, ok = share.calibrate(state, "max5", "h5", 20, 5)
        self.assertEqual(state["manual"]["max5"]["h5"], [30, 10])
        self.assertEqual(share.pick_k(state, "max5", "h5")[0], 3.0)
        self.assertEqual(share.reset_manual(state), {})

    def test_other_plan_value_is_converted_by_multiplier(self):
        state, _ = share.calibrate({}, "pro", "h5", 26, 10)  # Pro 2.6
        k, src = share.pick_k(state, "max5", "h5")
        self.assertAlmostEqual(k, 0.52)
        self.assertEqual(src, "converted")
        self.assertEqual(share.pick_k(state, "max5", "week")[1], "base")  # 기간이 다르면 쓰지 않는다

    def test_edit_does_not_mutate_input(self):
        state, _ = share.calibrate({}, "pro", "h5", 20, 5)
        before = {"manual": {"pro": {"h5": list(state["manual"]["pro"]["h5"])}}}
        share.calibrate(state, "pro", "h5", 20, 5)
        share.learn(state, "pro", "h5", 20, 5)
        self.assertEqual(state, before)


class Split(unittest.TestCase):
    def test_alone_has_no_others(self):
        self.assertEqual(share.split(5, 5, 1), (5, 0))

    def test_others_is_the_rest(self):
        mine, others = share.split(35, 19.1, 0.494)
        self.assertAlmostEqual(mine, 9.44, places=1)
        self.assertAlmostEqual(others, 25.56, places=1)

    def test_never_negative(self):
        # 계수가 커서 내 몫이 사용률을 넘으면 사용률로 자른다
        self.assertEqual(share.split(10, 100, 5), (10, 0))
        self.assertEqual(share.split(0, 0, 2), (0, 0))


class Summary(unittest.TestCase):
    def test_splits_both_windows(self):
        events = ev((NOW - 600, 10.0), (NOW - 2 * 86400, 100.0))
        s = share.summary(data(), events, {}, "max5", NOW)
        self.assertEqual(s["plan"], "max5")
        self.assertAlmostEqual(s["h5"]["mine"], 4.9, places=1)   # 10 × 0.494
        self.assertAlmostEqual(s["h5"]["others"], 25.1, places=1)
        self.assertEqual(s["h5"]["src"], "base")
        self.assertEqual(s["h5"]["models"], {"opus": 10.0})
        self.assertAlmostEqual(s["week"]["m"], 110.0)            # 주간은 이틀 전 사용도 센다
        self.assertAlmostEqual(s["week"]["mine"], 110 * 0.038, places=1)

    def test_window_starts_at_reset_minus_length(self):
        r5 = NOW + 600
        events = ev((r5 - 5 * 3600 - 1, 50.0), (r5 - 5 * 3600 + 1, 1.0))
        self.assertEqual(share.summary(data(r5=r5), events, {}, "pro", NOW)["h5"]["m"], 1.0)

    def test_usage_after_the_observation_is_not_counted(self):
        events = ev((NOW - 60, 1.0), (NOW + 600, 99.0))
        self.assertEqual(share.summary(data(observed_at=NOW - 30), events, {}, "pro", NOW + 1000)["h5"]["m"], 1.0)

    def test_no_window_or_old_data_gives_none(self):
        events = ev((NOW - 60, 1.0))
        self.assertIsNone(share.summary(data(r5=None), events, {}, "pro", NOW)["h5"])
        self.assertIsNone(share.summary(data(r5=NOW - 1), events, {}, "pro", NOW)["h5"])  # 이미 끝난 창
        self.assertIsNone(share.summary(data(has5=False), events, {}, "pro", NOW)["h5"])
        old = share.summary(data(src="log"), events, {}, "pro", NOW)
        self.assertEqual((old["h5"], old["week"]), (None, None))
        self.assertIsNone(share.summary(None, events, {}, "auto", NOW)["h5"])


class MineSeries(unittest.TestCase):
    def rows(self, reset, points):
        key = int(round(reset / 60) * 60)
        return [{"t": t, "u5": u, "r5": key} for t, u in points]

    def test_curve_accumulates_spending_and_never_exceeds_usage(self):
        reset = NOW + 3600
        start = reset - 5 * 3600
        rows = self.rows(reset, [(start + 600, 5), (start + 1200, 10), (start + 1800, 12)])
        events = ev((start - 100, 99.0), (start + 300, 2.0), (start + 1000, 3.0), (start + 1700, 40.0))  # 첫 줄은 창 밖
        out = share.mine_series(rows, events, 2.0, reset)
        # 600초: 2달러×2=4, 1200초: 5달러×2=10, 1800초: 45달러×2=90 이지만 사용률 12 로 자른다
        self.assertEqual([p[1] for p in out], [4.0, 10.0, 12.0])
        self.assertEqual([p[0] for p in out], [start + 600, start + 1200, start + 1800])

    def test_other_windows_and_empty_input(self):
        reset = NOW + 3600
        rows = self.rows(reset, [(NOW - 100, 5)]) + [{"t": NOW - 50, "u5": 9, "r5": reset + 18000}, {"t": NOW, "u5": 1, "r5": None}]
        self.assertEqual(len(share.mine_series(rows, [], 1.0, reset)), 1)
        self.assertEqual(share.mine_series([], [], 1.0, reset), [])

    def test_long_history_is_downsampled_and_keeps_the_last_point(self):
        reset = NOW + 3600
        points = [(reset - 18000 + 10 * i, i / 10) for i in range(1, 500)]
        out = share.mine_series(self.rows(reset, points), [], 1.0, reset, limit=100)
        self.assertLessEqual(len(out), 101)
        self.assertEqual(out[-1][0], points[-1][0])

    def test_summary_adds_series_only_for_the_five_hour_window_when_rows_given(self):
        rows = self.rows(NOW + 3600, [(NOW - 300, 10), (NOW - 30, 30)])
        events = ev((NOW - 600, 10.0))
        s = share.summary(data(), events, {}, "pro", NOW, rows)
        self.assertEqual(s["h5"]["series"][-1], [round(NOW - 30), 24.7])
        self.assertNotIn("series", s["week"])
        self.assertNotIn("series", share.summary(data(), events, {}, "pro", NOW)["h5"])


class PastWindows(unittest.TestCase):
    def test_each_window_uses_its_last_seen_usage_and_own_spending(self):
        day = 86400
        rows = [{"t": NOW - 2 * day, "u5": 10, "r5": NOW - 2 * day + 600}, {"t": NOW - 2 * day + 300, "u5": 20, "r5": NOW - 2 * day + 600},
                {"t": NOW - 100, "u5": 40, "r5": NOW + 1000}, {"t": NOW - 50, "u5": 35, "r5": NOW + 1000},
                {"t": NOW - 10, "u5": 5, "r5": None}, {"t": NOW + 999, "u5": 99, "r5": NOW + 1000}]  # 미래 표본·창 없음은 뺀다
        events = ev((NOW - 2 * day, 4.0), (NOW - 60, 10.0), (NOW + 500, 99.0))
        out = share.past_windows(rows, events, {}, "pro", "Pro", NOW)
        self.assertEqual((out["plan"], out["src"], out["k"]), ("pro", "base", 2.47))
        old, cur = out["windows"]
        self.assertEqual((old["u"], old["m"], old["others"]), (20.0, 4.0, 10.1))   # 4달러 × 2.47 = 9.9%p
        self.assertEqual((cur["u"], cur["m"], cur["mine"], cur["others"]), (40.0, 10.0, 24.7, 15.3))

    def test_limit_keeps_the_newest_windows(self):
        rows = [{"t": NOW - 100 * i, "u5": 1, "r5": NOW + i} for i in range(1, 20)]
        out = share.past_windows(rows, [], {}, "auto", "Max", NOW, limit=3)
        self.assertEqual([w["reset"] for w in out["windows"]], [NOW + 17, NOW + 18, NOW + 19])
        self.assertEqual(share.past_windows([], [], {}, "auto", "Max", NOW)["windows"], [])


class StateFile(unittest.TestCase):
    def test_clean_state_keeps_only_valid_entries(self):
        raw = {"manual": {"max5": {"h5": [30, 10], "week": [1, 0], "bad": [1, 1]}, "weird": {"h5": [1, 1]}},
               "learned": {"pro": {"h5": [[10, 5], [1], ["x", 2], [float("nan"), 1]] + [[2, 2]] * 40}},
               "other": 1}
        out = share.clean_state(raw)
        self.assertEqual(out["manual"], {"max5": {"h5": [30.0, 10.0]}})
        self.assertEqual(len(out["learned"]["pro"]["h5"]), share.LEARN_MAX)
        self.assertEqual(set(out), {"manual", "learned"})

    def test_garbage_gives_empty_state(self):
        for raw in (None, [], "x", 5, {"manual": []}, {"learned": {"pro": 3}}):
            self.assertEqual(share.clean_state(raw), {})


if __name__ == "__main__":
    unittest.main()
