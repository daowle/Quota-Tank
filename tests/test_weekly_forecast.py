"""실제 계정 없이 웹 공통 로직의 주간 예측 경계값을 확인한다."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "웹 예측 검증에는 Node.js가 필요하다")
class WeeklyForecastTest(unittest.TestCase):
    def forecast(self, used, elapsed, reset=True):
        script = "const matchMedia = () => ({matches: true});\n"
        script += (ROOT / "web/common.js").read_text(encoding="utf-8")
        script += "\nconst t = 1800000000; const s = " + json.dumps({
            "uw": used, "r5": None, "rw": 1800000000 + 604800 - elapsed if reset else None,
        }) + "; console.log(JSON.stringify(weekly(s, view(s, t), t)));"
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                text=True, encoding="utf-8", check=True)
        return json.loads(result.stdout)

    def test_fast_week_shows_exhaustion_time_and_daily_budget(self):
        result = self.forecast(80, 2 * 86400)
        self.assertEqual(result["eta"], 1800000000 + 43200)
        self.assertIn("소진 예상", result["txt"])
        self.assertIn("12시간 0분 후", result["detail"])
        self.assertIn("하루 4.0%p 이내 권장", result["detail"])
        self.assertIn("이번 주 평균 속도 기준", result["tip"])
        self.assertTrue(result["warn"])

    def test_safe_week_projects_usage_at_reset(self):
        result = self.forecast(20, 2 * 86400)
        self.assertIn("리셋 때 약 70% 예상", result["txt"])
        self.assertNotIn("eta", result)
        self.assertFalse(result.get("warn", False))

    def test_first_twelve_hours_wait_even_at_high_usage(self):
        for elapsed in (0, 3600, 43200):
            with self.subTest(elapsed=elapsed):
                result = self.forecast(90, elapsed)
                self.assertIn("예측 준비 중", result["txt"])
                self.assertNotIn("eta", result)
        self.assertIn("소진 예상", self.forecast(90, 43201)["txt"])

    def test_reached_limit_shows_reset_even_in_first_hours(self):
        result = self.forecast(100, 3600)
        self.assertIn("한도 도달", result["txt"])
        self.assertIn("후 리셋", result["detail"])
        self.assertNotIn("eta", result)

    def test_zero_usage_and_exact_pace_are_safe(self):
        self.assertIn("약 0% 예상", self.forecast(0, 86400)["txt"])
        self.assertIn("약 100% 예상", self.forecast(50, 302400)["txt"])

    def test_missing_and_expired_windows_have_no_prediction(self):
        for result in (self.forecast(80, 86400, reset=False), self.forecast(80, 604800)):
            self.assertEqual(result["txt"], "주간 창 정보 없음")
            self.assertNotIn("eta", result)


if __name__ == "__main__":
    unittest.main()
