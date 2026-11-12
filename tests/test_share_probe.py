"""tools/share_probe.py: 합성 기록으로 환산 계수와 나누기를 확인한다(실제 기록·로그를 읽지 않는다)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import share_probe as sp

K = 2.0  # 1달러에 2%p


def scenario(others):
    """5시간 창 8개. 창마다 내가 10분에 1달러씩 2시간 쓰고, others[i] %p 를 다른 사용자가 고르게 쓴다."""
    samples, events = [], []
    for i, extra in enumerate(others):
        start = 1_000_000 + i * sp.FIVE_H
        for step in range(13):
            t = start + step * 600
            if step:
                events.append((t - 300, 1.0, "opus"))
            samples.append({"t": t, "s": "claude", "u5": round(K * step + extra * step / 12), "r5": start + sp.FIVE_H,
                            "uw": 0, "rw": None})
    return samples, events


class ShareProbe(unittest.TestCase):
    def test_alone_has_no_others(self):
        samples, events = scenario([0] * 8)
        wins = sp.windows(samples, events)
        k, n = sp.k_from_windows(wins)
        self.assertEqual(n, 8)
        self.assertAlmostEqual(k, K, delta=0.05)
        self.assertTrue(all(sp.split(w, k)[1] < 1 for w in wins))

    def test_shared_windows_show_others(self):
        samples, events = scenario([0, 0, 0, 30, 0, 40, 0, 0])
        wins = sp.windows(samples, events)
        for k, _ in (sp.k_from_windows(wins), sp.k_from_bins(wins, events)):
            self.assertAlmostEqual(k, K, delta=0.3)
            self.assertAlmostEqual(sp.split(wins[3], k)[1], 30, delta=4)
            self.assertAlmostEqual(sp.split(wins[5], k)[1], 40, delta=4)
            self.assertLess(sp.split(wins[0], k)[1], 4)

    def test_solo_calibration(self):
        samples, events = scenario([30, 0, 30, 30])
        wins = sp.windows(samples, events)
        k = sp.k_from_solo(wins, [1])
        self.assertAlmostEqual(k, K, delta=0.01)
        self.assertAlmostEqual(sp.split(wins[0], k)[1], 30, delta=1)
        self.assertEqual(sp.split(wins[0], None), (None, None))

    def test_too_little_data_gives_no_k(self):
        samples, events = scenario([0, 0])
        self.assertIsNone(sp.k_from_windows(sp.windows(samples, events))[0])

    def test_solo_counts_only_after_since(self):
        prices = {"claude-opus-5-5": (10, 10, 0, 0, 0)}
        start = 1_000_000
        samples = [{"t": start + i * 600, "u5": i * 2, "r5": start + sp.FIVE_H} for i in range(13)]
        rows = [(start + i * 600 - 300, "claude-opus-5-5", 60_000, 40_000, 500_000, 0, 0) for i in range(1, 13)]
        wins = sp.solo(samples, rows, prices, start + 3600, lag=0)  # 한 시간 뒤부터: 숫자가 처음 오른 14% 에서 24% 까지
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["du"], 10)
        self.assertAlmostEqual(wins[0]["m"], 5.0)
        self.assertEqual(wins[0]["tok"], [300_000, 200_000, 2_500_000, 0])
        self.assertEqual([round(s, 3) for s in wins[0]["steps"]], [0.5] * 5)
        usd, tokens, total = sp.per_percent(wins)
        self.assertAlmostEqual(usd, 0.5)
        self.assertAlmostEqual(tokens, 50_000)
        self.assertAlmostEqual(total, 300_000)
        self.assertIsNone(sp.per_percent(sp.solo(samples, rows, prices, start + 6900, lag=0)))  # 숫자가 바뀐 순간이 둘 미만이면 재지 않는다

    def test_prices_and_families(self):
        prices = sp.load_prices()
        self.assertIn("claude-opus-5-5", prices)
        self.assertEqual(sp.price_of("claude-opus-5-5-20260901", prices), prices["claude-opus-5-5"])
        self.assertEqual(sp.price_of("unknown-model", prices), sp.FALLBACK_PRICE)
        rows = [(10, "claude-sonnet-5-5", 1_000_000, 0, 0, 0, 0)]
        self.assertEqual(sp.weigh(rows, prices), [(10, 2.0, "sonnet")])


if __name__ == "__main__":
    unittest.main()
