"""QR 코드 만들기. 아래 해시는 만든 무늬를 독립된 해독기(zxing-cpp)로 읽어 원문이 나오는 것을 확인한 뒤 적은 값이다."""
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import qr  # noqa: E402

VERIFIED = {  # 원문: (한 변의 칸 수, 무늬 해시 앞 16자)
    "a": (21, "56bb324cb2e86a13"),
    "http://100.64.0.1:47113/abcdefghjkmn/": (29, "efd5dc2b61831c2b"),
    "http://100.101.102.103:47113/zyxwvutsrqpn/": (29, "ae6109de287a6407"),
    "x" * 17: (21, "714ce3d4a6c70750"),
    "x" * 18: (25, "c55aa81be6fbc2ab"),
    "z" * 53: (29, "b35ef16f3d2481e3"),
    "z" * 54: (33, "9ca545bc5fb0662d"),
    "v" * 106: (37, "2f7437c9b7970e5b"),
    "한글 QR 시험": (21, "28ddc4c4a5f224c2"),
}


class QrTest(unittest.TestCase):
    def test_matches_patterns_checked_with_a_decoder(self):
        for text, (size, digest) in VERIFIED.items():
            rows = qr.rows(text)
            self.assertEqual((len(rows), {len(r) for r in rows}), (size, {size}), text[:20])
            self.assertEqual(hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16], digest, text[:20])

    def test_fixed_patterns(self):
        m = qr.matrix("http://100.64.0.1:47113/abcdefghjkmn/")
        n = len(m)
        finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
        for top, left in ((0, 0), (0, n - 7), (n - 7, 0)):  # 세 모서리의 위치 찾기 무늬
            self.assertEqual(["".join("1" if m[top + y][left + x] else "0" for x in range(7)) for y in range(7)], finder)
        self.assertTrue(all(m[6][i] == (i % 2 == 0) for i in range(8, n - 8)))  # 타이밍 무늬
        self.assertTrue(m[n - 8][8])  # 항상 검은 칸
        for mask in range(8):  # 마스크를 바꿔도 기능 무늬는 그대로다
            other = qr.matrix("http://100.64.0.1:47113/abcdefghjkmn/", mask)
            self.assertEqual([row[:7] for row in other[:7]], [row[:7] for row in m[:7]])

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            qr.matrix("v" * 107)

    def test_error_correction_codewords(self):
        # ISO/IEC 18004 의 버전 1-M 예제 데이터와 그 오류 정정 코드워드
        data = [0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11]
        self.assertEqual(qr.ec_codewords(data, 10), [0xA5, 0x24, 0xD4, 0xC1, 0xED, 0x36, 0xC7, 0x87, 0x2C, 0x55])


if __name__ == "__main__":
    unittest.main()
