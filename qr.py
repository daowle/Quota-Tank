"""QR 코드 만들기(폰에서 보기 주소용). 표준 라이브러리만 쓰는 순수 함수이고 파일·네트워크·토큰을 만지지 않는다.
바이트 모드 · 오류 정정 L · 버전 1~5(블록 하나)만 만든다. 106바이트까지 담는다 — 폰에서 보기 주소는 50바이트가 안 된다."""

# 버전: (데이터 코드워드, 오류 정정 코드워드). 오류 정정 L 은 버전 5 까지 블록이 하나다
VERSIONS = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}
MASKS = (lambda i, j: (i + j) % 2 == 0, lambda i, j: i % 2 == 0, lambda i, j: j % 3 == 0,
         lambda i, j: (i + j) % 3 == 0, lambda i, j: (i // 2 + j // 3) % 2 == 0,
         lambda i, j: i * j % 2 + i * j % 3 == 0, lambda i, j: (i * j % 2 + i * j % 3) % 2 == 0,
         lambda i, j: ((i + j) % 2 + i * j % 3) % 2 == 0)

EXP, LOG = [0] * 512, [0] * 256  # GF(256), 원시 다항식 0x11d
_x = 1
for _i in range(255):
    EXP[_i], LOG[_x] = _x, _i
    _x = _x << 1 ^ (0x11d if _x & 0x80 else 0)
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def ec_codewords(data, n):
    """리드 솔로몬 오류 정정 코드워드 n 개."""
    mul = lambda a, b: EXP[LOG[a] + LOG[b]] if a and b else 0
    gen = [1]  # (x - a^0)(x - a^1)… 높은 차수부터
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            nxt[j + 1] ^= mul(c, EXP[i])
        gen = nxt
    rem = [0] * n
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= mul(gen[j + 1], factor)
    return rem


def codewords(payload):
    """(버전, 데이터 + 오류 정정 코드워드). 너무 길면 ValueError."""
    version = next((v for v, (cap, _) in VERSIONS.items() if len(payload) <= cap - 2), None)
    if version is None:
        raise ValueError("Too long for a QR code")
    cap, ec = VERSIONS[version]
    bits = "0100" + format(len(payload), "08b") + "".join(format(b, "08b") for b in payload)
    bits += "0" * min(4, cap * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    data += [(0xEC, 0x11)[i % 2] for i in range(cap - len(data))]
    return version, data + ec_codewords(data, ec)


def format_bits(mask):
    """오류 정정 L(01) + 마스크 번호의 15비트 형식 정보."""
    data = 0b01 << 3 | mask
    rem = data
    for _ in range(10):
        rem = rem << 1 ^ (0x537 if rem >> 9 & 1 else 0)
    return (data << 10 | rem) ^ 0x5412


def penalty(m):
    """마스크를 고르는 벌점(표준의 네 규칙). 낮을수록 읽기 쉽다."""
    n, score = len(m), 0
    lines = ["".join("1" if c else "0" for c in row) for row in m]
    lines += ["".join("1" if m[i][j] else "0" for i in range(n)) for j in range(n)]
    for line in lines:
        run = 1
        for a, b in zip(line, line[1:] + "x"):
            if a == b:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += 40 * (line.count("10111010000") + line.count("00001011101"))
    for i in range(n - 1):
        for j in range(n - 1):
            if m[i][j] == m[i][j + 1] == m[i + 1][j] == m[i + 1][j + 1]:
                score += 3
    dark = sum(map(sum, m))
    return score + 10 * (abs(dark * 20 - n * n * 10) // (n * n))


def matrix(text, mask=None):
    """text 를 담은 QR 코드: 행마다 True(검은 칸)·False 인 정사각 목록. 여백(quiet zone)은 넣지 않는다.
    mask 를 주지 않으면 벌점이 가장 낮은 마스크를 고른다."""
    version, words = codewords(text.encode("utf-8"))
    n = 17 + 4 * version
    m = [[False] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]  # 기능 무늬 자리: 데이터를 놓지 않고 마스크도 씌우지 않는다

    def put(y, x, dark):
        if 0 <= y < n and 0 <= x < n:
            m[y][x], fixed[y][x] = dark, True

    for cy, cx in ((3, 3), (3, n - 4), (n - 4, 3)):  # 위치 찾기 무늬 + 흰 테두리
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                put(cy + dy, cx + dx, max(abs(dy), abs(dx)) in (0, 1, 3))
    if version > 1:  # 정렬 무늬
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(n - 7 + dy, n - 7 + dx, max(abs(dy), abs(dx)) != 1)
    for i in range(8, n - 8):  # 타이밍 무늬
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)

    def put_format(mask):
        bits = format_bits(mask)
        bit = lambda i: bool(bits >> i & 1)
        for i in range(6):
            put(i, 8, bit(i))
        put(7, 8, bit(6))
        put(8, 8, bit(7))
        put(8, 7, bit(8))
        for i in range(9, 15):
            put(8, 14 - i, bit(i))
        for i in range(8):
            put(8, n - 1 - i, bit(i))
        for i in range(8, 15):
            put(n - 15 + i, 8, bit(i))
        put(n - 8, 8, True)  # 항상 검은 칸

    put_format(0)  # 자리만 잡아 둔다
    stream = "".join(format(w, "08b") for w in words)
    k, up, x = 0, True, n - 1
    while x > 0:  # 오른쪽 아래부터 두 열씩 지그재그로 채운다
        if x == 6:
            x -= 1
        for y in (range(n - 1, -1, -1) if up else range(n)):
            for cx in (x, x - 1):
                if not fixed[y][cx]:
                    m[y][cx] = k < len(stream) and stream[k] == "1"
                    k += 1
        up, x = not up, x - 2

    def masked(mask):
        out = [[m[y][x] != (not fixed[y][x] and MASKS[mask](y, x)) for x in range(n)] for y in range(n)]
        return out

    best = None
    for candidate in (range(8) if mask is None else (mask,)):
        base, m = m, masked(candidate)
        put_format(candidate)
        score, result, m = penalty(m), m, base
        if best is None or score < best[0]:
            best = (score, result)
    return best[1]


def rows(text):
    """화면에 넘기는 모양: 행마다 '1'(검은 칸)·'0' 문자열."""
    return ["".join("1" if c else "0" for c in row) for row in matrix(text)]
