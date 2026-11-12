"""Real WebView smoke check with fake data; never reads user credentials.
Windows: WebView2 (+ navigation guard). macOS: WKWebView — catches CSP blocking the bridge's return values."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import webview
import qr
from hardening import cycle_times, page_app, protect_window, serve_local

MAC = sys.platform == "darwin"
# 진단용: SMOKE_ALLOW_EVAL=0/1, SMOKE_HIDDEN=0/1 로 맥 CSP·숨긴 창 영향을 따로 본다
ALLOW_EVAL = os.environ.get("SMOKE_ALLOW_EVAL", "1" if MAC else "0") == "1"
HIDDEN = os.environ.get("SMOKE_HIDDEN", "1") == "1"
T0 = time.time()


def log(*a):
    print(f"[smoke {time.time() - T0:5.1f}s]", *a, file=sys.stderr, flush=True)

results = []
page = sys.argv[1] if len(sys.argv) > 1 else "main.html"
smoke_accounts = []
smoke_gemini = False
check_started = False


smoke_order = []
smoke_names = {}


def login_options():
    return {"providers": [{"key": "claude", "name": "Claude", "ok": True, "why": ""},
                          {"key": "gpt", "name": "Codex", "ok": True, "why": ""}], "found": []}


def start_login(provider, name):
    if provider != "claude":  # 브라우저 로그인이 안 되는 경우: 화면이 다른 방법을 내놓는지 본다
        return {"error": "Codex 가 설치돼 있지 않아요"}
    smoke_accounts.append({"id": "account_smoke", "provider": "claude", "name": name or "smoke@example.test", "email": "smoke@example.test"})
    return {"ok": True}


def cancel_login():
    return {"ok": True}


def rename_account(account_id, name):
    if account_id in ("claude", "gpt", "gemini"):
        smoke_names[account_id] = name
    for account in smoke_accounts:
        if account["id"] == account_id:
            account["name"] = name
    return {"ok": True}


def remove_account(account_id):
    if account_id in smoke_work.values():
        return {"error": "작업 계정은 삭제할 수 없어요"}
    smoke_accounts[:] = [a for a in smoke_accounts if a["id"] != account_id]
    return {"ok": True}


smoke_work = {"claude": "claude", "gpt": "gpt"}


def switch_account(key):
    provider = next((a["provider"] for a in smoke_accounts if a["id"] == key), key)
    smoke_work[provider] = key
    return {"ok": True, "msg": "열려 있는 Claude Code도 다음 메시지부터 이 계정을 써요"}


def card_order():
    keys = ["claude", "gpt"] + (["gemini"] if smoke_gemini else []) + [a["id"] for a in smoke_accounts]
    return list(dict.fromkeys([k for k in smoke_order if k in keys] + keys))


def move_account(key, step):
    order = card_order()
    i = order.index(key)
    if 0 <= i + step < len(order):
        order[i], order[i + step] = order[i + step], order[i]
    smoke_order[:] = order
    return {"ok": True}


def set_gemini_enabled(enabled):
    global smoke_gemini
    smoke_gemini = enabled
    return {"ok": True}


smoke_share = {"on": False, "plan": "auto", "solo": []}


def toggle_shared_split():
    smoke_share["on"] = not smoke_share["on"]
    return smoke_share["on"]


def set_share_plan(plan):
    smoke_share["plan"] = plan


def calibrate_share(span):
    smoke_share["solo"].append(span)
    return {"ok": span == "h5", "msg": "모의 응답"}


def reset_share_calibration():
    smoke_share["solo"].append("reset")


smoke_remote = {"on": False}
smoke_cycle = {"on": False, "times": []}


def toggle_cycle_start():
    smoke_cycle["on"] = not smoke_cycle["on"]
    return smoke_cycle["on"]


def set_cycle_times(text):
    smoke_cycle["times"] = cycle_times(text)
    return smoke_cycle["times"]



def toggle_remote_view():
    smoke_remote["on"] = not smoke_remote["on"]
    url = "http://100.64.0.1:47113/abcdefghjkmn/" if smoke_remote["on"] else ""
    return dict(on=smoke_remote["on"], url=url, qr=qr.rows(url) if url else [], msg="")


NO5 = os.environ.get("SMOKE_NO5") == "1"


def state():
    now = time.time()
    sample = dict(u5=23, uw=41, r5=now + 3600, rw=now + 86400, plan="SmokePro",
                  src="live", observed_at=now, hist=[], starts=[], extra=[])
    if NO5:  # 5시간 창이 없는 계정(Claude Max): 주간·오늘 쓸 몫 화면
        sample.update(u5=0.0, r5=None, has5=False, hasw=True, day0=35)
    if smoke_share["on"]:  # 공유 사용량 나누기: 5시간은 나눠졌고 주간은 아직 창이 없다
        sample["share"] = dict(plan="max5", week=None, h5=dict(mine=14.0, others=9.0, m=28.3, k=0.494, src="base", models={"opus": 28.3},
                                                              series=[[now - 1200, 4], [now - 600, 9], [now, 14.0]]))
    return dict(svc={"claude": sample}, err={}, fetched_at=now, status={}, accounts=list(smoke_accounts),
                order=card_order(), names=dict(smoke_names), login=None, work=dict(smoke_work),
                cfg=dict(compact=False, topmost=False, startup=False, day_reset_hour=6,
                         theme="dark", mac=False, compact_hidden=[], gemini_enabled=smoke_gemini,
                         shared_split=smoke_share["on"], share_plan=smoke_share["plan"]))


def fit(*args):
    pass


def detail_size():
    return [300, 560]


def theme():
    return "dark"


def insights():
    now = time.time()
    return dict(svc={}, samples=0, estimated=False, cost=None, days=30,
                share=dict(plan="max5", src="base", k=0.494, windows=[dict(reset=now + 600, u=35, mine=9.4, others=25.6, m=19.1)]))


def fit_insights(*args):
    pass


def logged(app):
    """진단: 웹뷰가 로컬 서버에 실제로 요청했는지, 무엇을 돌려줬는지."""
    def wrapped(env, start_response):
        def sr(status, headers):
            log("http", env.get("REQUEST_METHOD"), env.get("PATH_INFO"), "host=" + env.get("HTTP_HOST", ""),
                "fetch-site=" + env.get("HTTP_SEC_FETCH_SITE", "-"), "->", status)
            return start_response(status, headers)
        return app(env, sr)
    return wrapped


def trace(stage):
    log("js:", str(stage)[:300])


def report(data):
    log("report", data)
    results.append(data)
    # Let the bridge deliver the return value before disposing its native control.
    close = threading.Timer(.3, win.destroy)
    close.daemon = True
    close.start()


def check():
    global check_started
    if check_started:
        return  # 차단한 외부 이동이 loaded를 다시 보내도 조작 검증을 중복 실행하지 않는다.
    check_started = True
    log("loaded event; url =", win.real_url)
    win.run_js("""
    window.pywebview.api.trace('run_js ok, api keys=' + Object.keys(window.pywebview.api).join(','));
    window.addEventListener('unhandledrejection', e => window.pywebview.api.trace('unhandled ' + (e.reason && e.reason.stack || e.reason)));
    setTimeout(async () => {
      try {
        window.pywebview.api.trace('calling state()');
        const data = await window.pywebview.api.state();
        window.pywebview.api.trace('state() returned');
        const injected = document.createElement('script');
        injected.textContent = 'window.UNTRUSTED_EXECUTED = true';
        document.head.appendChild(injected);
        let networkBlocked = false;
        try { await fetch('/common.js'); } catch (_) { networkBlocked = true; }
        if (TRY_NAVIGATION) location.href = 'https://example.invalid/';
        setTimeout(async () => {
          const renderedData = document.body.innerText.includes('SmokePro') || document.body.innerText.includes('30');
          let compactLayout = true;
          let accountManagement = true;
          const compact = document.getElementById('compact');
          if (compact) {
            // 좁은 초기 창에서도 기본 링 두 개만 한 줄로 표시한다.
            document.body.classList.add('compact');
            for (const width of [160, 184, 198, 300]) {
              document.body.style.width = width + 'px';
              const rings = [...compact.querySelectorAll('.cmp:not(.hid) .ring')].map(el => el.getBoundingClientRect());
              compactLayout = compactLayout && rings.length === 2 &&
                rings.every(r => Math.abs(r.top - rings[0].top) < 1) &&
                Math.abs(compact.getBoundingClientRect().width - 136) < 1;
            }
            document.body.style.width = '';
            document.body.classList.remove('compact');
            // 실제 버튼을 눌러 계정 추가(Gemini·브라우저 로그인)·이름 수정·순서 변경·삭제를 확인한다.
            const ids = () => [...document.querySelectorAll('#account-list .acct')].map(row => row.dataset.id).join(',');
            const waitFor = async predicate => {
              for (let i = 0; i < 40; i++) {
                if (predicate()) return;
                await new Promise(resolve => setTimeout(resolve, 100));
              }
              throw new Error('화면 갱신 시간 초과: ' + ids());
            };
            const find = (id, act) => document.querySelector('#account-list [data-id="' + id + '"] [data-act="' + act + '"]');
            const press = (id, act) => {
              const button = find(id, act);
              if (!button || button.disabled) throw new Error('버튼 없음: ' + id + ' ' + act);
              button.click();
            };
            const nameOf = id => document.querySelector('#account-list [data-id="' + id + '"] b').textContent;
            const cards = () => [...document.querySelectorAll('#compact .cmp:not(.hid)')].map(el => el.title.split(' · 20')[0]).join(',');
            const check = (label, ok) => { if (!ok) throw new Error('확인 실패: ' + label); };
            try {
              document.querySelector('#menu [data-act="accounts"]').click();
              await waitFor(() => ids() === 'claude,gpt');
              check('기본 계정은 삭제 버튼이 없다', find('claude', 'edit') && !find('claude', 'del') && find('claude', 'up').disabled && !find('claude', 'down').disabled);
              check('설명 문장 없음', !document.querySelector('#accounts-panel p:not(:empty)'));
              document.getElementById('add-open').click();
              check('서비스 세 개', document.querySelectorAll('#provs button').length === 3 && !document.getElementById('add-box').hidden);
              document.querySelector('#provs [data-k="gemini"]').click();
              await waitFor(() => ids() === 'claude,gpt,gemini');
              check('Gemini 링 표시·추가 칸 닫힘', cards().split(',').length === 3 && document.getElementById('add-box').hidden);
              check('Gemini 도 이름 수정·삭제', find('gemini', 'edit') && find('gemini', 'del'));
              press('gemini', 'edit');
              document.querySelector('#account-list input').value = '제미나이';
              press('gemini', 'save');
              await waitFor(() => !document.querySelector('#account-list input') && nameOf('gemini') === '제미나이');
              document.getElementById('add-open').click();
              check('켠 Gemini 는 다시 고를 수 없다', document.querySelector('#provs [data-k="gemini"]').disabled);
              check('처음엔 다른 방법을 보이지 않는다', document.getElementById('ways').hidden);
              document.querySelector('#provs [data-k="gpt"]').click();  // 로그인 실패 → 파일에서 선택
              await waitFor(() => !document.getElementById('ways').hidden);
              check('실패 이유와 파일에서 선택', document.getElementById('account-message').textContent.includes('Codex') &&
                [...document.querySelectorAll('#ways button')].some(b => b.textContent === '파일에서 선택'));
              document.querySelector('#provs [data-k="claude"]').click();  // 고르면 바로 브라우저 로그인
              await waitFor(() => ids() === 'claude,gpt,gemini,account_smoke');
              check('성공하면 다른 방법을 감춘다', document.getElementById('ways').hidden);
              check('이름은 로그인 이메일', nameOf('account_smoke') === 'smoke@example.test');
              press('account_smoke', 'edit');
              document.querySelector('#account-list input').value = '취소할 이름';
              press('account_smoke', 'cancel');
              check('수정 취소', !document.querySelector('#account-list input') && nameOf('account_smoke') === 'smoke@example.test');
              press('account_smoke', 'edit');
              document.querySelector('#account-list input').value = '업무용';
              press('account_smoke', 'save');
              await waitFor(() => !document.querySelector('#account-list input') && nameOf('account_smoke') === '업무용');
              for (const expected of ['claude,gpt,account_smoke,gemini', 'claude,account_smoke,gpt,gemini', 'account_smoke,claude,gpt,gemini']) {
                press('account_smoke', 'up');
                await waitFor(() => ids() === expected);
              }
              check('맨 위에서는 위로 버튼이 꺼진다', find('account_smoke', 'up').disabled);
              check('카드도 같은 순서: ' + cards(), cards().startsWith('Claude · 업무용'));
              press('account_smoke', 'down');
              await waitFor(() => ids() === 'claude,account_smoke,gpt,gemini');
              const pressed = id => find(id, 'work') && find(id, 'work').getAttribute('aria-pressed') === 'true';
              check('작업 계정: 기본 Claude·Codex', pressed('claude') && pressed('gpt') && !pressed('account_smoke') && !find('gemini', 'work'));
              press('account_smoke', 'work');
              await waitFor(() => pressed('account_smoke') && !pressed('claude'));
              check('작업 계정 바꾼 안내', pressed('gpt') && document.getElementById('account-message').textContent.includes('다음 메시지부터'));
              press('claude', 'work');
              await waitFor(() => pressed('claude') && !pressed('account_smoke'));
              press('account_smoke', 'del');
              check('삭제는 한 번 더 확인', ids().includes('account_smoke') && find('account_smoke', 'remove'));
              press('account_smoke', 'cancel');
              check('삭제 취소', !find('account_smoke', 'remove') && find('account_smoke', 'del'));
              press('account_smoke', 'del'); press('account_smoke', 'remove');
              await waitFor(() => ids() === 'claude,gpt,gemini');
              press('gemini', 'del'); press('gemini', 'remove');
              await waitFor(() => ids() === 'claude,gpt' && cards().split(',').length === 2);
              document.getElementById('accounts-close').click();
              check('닫힘', document.getElementById('accounts-panel').hidden);
            } catch (error) {
              accountManagement = false;
              window.pywebview.api.trace(String(error));
            }
          }
          let shareSplit = true;
          if (NO5 && compact) {
            // 5시간 창이 없는 계정: 링 숫자는 주간(41), 큰 글자는 오늘 쓸 몫, 5시간 줄은 숨김. 공유 나누기(5시간 기준) 확인은 건너뛴다
            const ring = document.querySelector('#compact .cmp .p').textContent, big = document.querySelector('.svc .cdt').textContent;
            shareSplit = ring === '41' && /^오늘 /.test(big) && document.querySelector('.svc .b5').parentElement.hidden;
            if (!shareSplit) window.pywebview.api.trace('no5 view: ' + ring + ' / ' + big);
          } else if (compact) {
            // 공유 사용량: 메뉴로 켜면 Claude 카드에 막대·수치가 나오고, 요금제와 '나만 썼어요'가 api 로 간다
            try {
              const menu = a => document.querySelector('#menu [data-act="' + a + '"]');
              const waitUntil = async predicate => {
                for (let i = 0; i < 40; i++) {
                  if (predicate()) return;
                  await new Promise(resolve => setTimeout(resolve, 100));
                }
                throw new Error('공유 화면 갱신 시간 초과');
              };
              const ok = (label, cond) => { if (!cond) throw new Error('확인 실패: ' + label); };
              const hiddenItems = () => [...document.querySelectorAll('#menu [data-share-only]')].map(e => e.hidden);
              const box = document.querySelector('.shr');
              ok('처음엔 꺼져 있어 줄도 메뉴 항목도 숨김', box.hidden && hiddenItems().every(Boolean));
              menu('share').click();
              const b5 = document.querySelector('.svc .b5'), bw = document.querySelector('.svc .bw');
              await waitUntil(() => !b5.querySelector('.mk').hidden);
              ok('나누는 중 문구는 숨김', box.hidden);
              ok('5시간 막대에 내 몫 색과 ▼', b5.querySelector('.mine').style.width === '14%' && b5.querySelector('.mk').style.left === '14%');
              ok('▼ 양 옆 퍼센트', b5.querySelector('.nl .nm').textContent === '14%' && b5.querySelector('.nr .no').textContent === '9%' && !b5.querySelector('.nl').hidden && !b5.querySelector('.nr').hidden);
              ok('화면에 나·다른 곳 글자 없음', !/다른 곳/.test(document.querySelector('.svc').innerText) && !b5.innerText.includes('나'));
              ok('주간 막대는 표시 없음', bw.querySelector('.mk').hidden && bw.querySelector('.nl').hidden && bw.querySelector('.nr').hidden && (bw.querySelector('.mine').style.width === '0px' || bw.querySelector('.mine').style.width === '0%'));
              ok('설명 툴팁에 출처와 요금제', b5.title.includes('기본 추정 · Max 5배') && b5.title.includes('이 PC'));
              await waitUntil(() => document.querySelector('.svc .spark .mline'));
              ok('그래프에 내 몫 곡선', document.querySelector('.svc .spark .marea') && document.querySelector('.svc .spark .mline').getAttribute('d').startsWith('M'));
              document.dispatchEvent(new MouseEvent('contextmenu', { clientX: 20, clientY: 20, bubbles: true }));
              ok('켜면 메뉴 항목이 보임', hiddenItems().every(h => !h));
              menu('share-plan').click();
              document.querySelector('#menu [data-act="share-plan"][data-v="max20"]').click();
              ok('요금제 선택 표시', document.querySelector('#menu [data-act="share-plan"][data-v="max20"]').getAttribute('aria-checked') === 'true');
              document.querySelector('#menu [data-act="share-solo"][data-v="week"]').click();
              await waitUntil(() => document.getElementById('solo-l').textContent === '맞출 수 없어요');
              document.querySelector('#menu [data-act="share-solo"][data-v="h5"]').click();
              await waitUntil(() => document.getElementById('solo-l').textContent === '맞췄어요');
              document.querySelector('#menu [data-act="share-reset"]').click();
              await new Promise(resolve => setTimeout(resolve, 200));
              ok('사이클 예약은 꺼져 있고 시각 칸 숨김', document.getElementById('cycle-row').hidden);
              document.querySelector('#menu [data-act="cycle"]').click();
              await waitUntil(() => !document.getElementById('cycle-row').hidden);
              const times = document.getElementById('cycle-times');
              times.value = '8:00, 25:00, 13:30 8:00'; times.dispatchEvent(new Event('change'));
              await waitUntil(() => times.value === '08:00, 13:30');
              ok('켜면 메뉴가 열린 채 받아들인 시각만 남음', document.body.classList.contains('menu') && document.querySelector('#menu [data-act="cycle"]').getAttribute('aria-checked') === 'true');
              document.querySelector('#menu [data-act="cycle"]').click();
              await waitUntil(() => document.getElementById('cycle-row').hidden);
              ok('폰에서 보기는 꺼져 있고 주소 줄 숨김', document.getElementById('remote-l').hidden && document.querySelector('#menu [data-act="remote-copy"]').hidden);
              document.querySelector('#menu [data-act="remote"]').click();
              await waitUntil(() => document.getElementById('remote-l').textContent === 'http://100.64.0.1:47113/abcdefghjkmn/');
              ok('켜면 메뉴가 열린 채 주소와 복사 단추가 보임', document.body.classList.contains('menu') && !document.getElementById('remote-l').hidden
                 && !document.querySelector('#menu [data-act="remote-copy"]').hidden && document.querySelectorAll('#remote-qr svg path').length === 1 && document.querySelector('#remote-qr svg').getAttribute('viewBox') === '0 0 37 37' && document.querySelector('#menu [data-act="remote"]').getAttribute('aria-checked') === 'true');
              document.querySelector('#menu [data-act="remote"]').click();
              await waitUntil(() => document.getElementById('remote-l').hidden);
              document.querySelector('#scrim').click();
              menu('share');
            } catch (error) {
              shareSplit = false;
              window.pywebview.api.trace(String(error));
            }
          }
          window.pywebview.api.report({bridge: !!data.cfg,
          csp: !window.UNTRUSTED_EXECUTED, networkBlocked,
          navigationBlocked: location.hostname === '127.0.0.1',
          renderedData, compactLayout, accountManagement, shareSplit,
          shareBox: compact ? true : !!document.querySelector('#shbox .shw') && document.querySelector('#shbox .shw .v').textContent === '나 9% · 다른 곳 26%',
          content: document.body.innerText.length,
          errors: window.__smokeErrors || []});
        }, 300);
      } catch (e) { window.pywebview.api.trace('error ' + e); window.pywebview.api.report({error: String(e)}); }
    }, 1500);
    """.replace("TRY_NAVIGATION", "false" if MAC else "true").replace("NO5", "true" if NO5 else "false"))  # protect_window 는 WebView2 전용
    # pywebview 가 반환값을 돌려줄 때 쓰는 경로(eval 로 감싼 evaluate_js)를 직접 시험한다
    threading.Thread(target=lambda: log("evaluate_js('1+1') =", win.evaluate_js("1+1")), daemon=True).start()


with tempfile.TemporaryDirectory() as temp:
    if os.environ.get("SMOKE_PLAIN") == "1":  # 진단: 우리 서버·CSP 없이 pywebview 자체가 이 환경에서 뜨는지
        win = webview.create_window("plain", html="<p>plain</p>", hidden=HIDDEN)
        win.events.loaded += lambda: (log("plain loaded"), threading.Timer(.5, win.destroy).start())
        threading.Timer(15, win.destroy).start()
        webview.start(debug=False)
        sys.exit(0)
    win = webview.create_window("Widget security smoke", serve_local(logged(page_app(ROOT / "web", page, allow_eval=ALLOW_EVAL))),
                                hidden=HIDDEN, width=136 if page == "main.html" else 300,
                                height=70 if page == "main.html" else 560, min_size=(60, 40), frameless=True)
    win.events.before_show += lambda: log("before_show")
    win.events.shown += lambda: log("shown")

    def probe():
        """진단: 서버 주소, 파이썬에서 직접 요청한 결과, (맥) 화면 캡처."""
        time.sleep(4)
        log("real_url =", win.real_url)
        try:
            import urllib.request
            with urllib.request.urlopen(win.real_url, timeout=5) as r:
                log("python GET", r.status, len(r.read()))
        except Exception as e:
            log("python GET failed:", repr(e)[:200])
            import faulthandler
            log("threads:", [t.name for t in threading.enumerate()])
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)  # 서버 스레드가 어디서 멈췄는지
            # 같은 환경에서 표준 라이브러리 서버는 응답하는지(러너 환경 문제와 구분)
            import http.server
            import urllib.request
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5) as r:
                    log("stdlib server GET", r.status)
            except Exception as e2:
                log("stdlib server GET failed:", repr(e2)[:200])
        shot = os.environ.get("SMOKE_SHOT")
        if MAC and shot:
            import subprocess
            log("screencapture", subprocess.run(["screencapture", "-x", shot], capture_output=True, text=True).returncode)
    threading.Thread(target=probe, daemon=True).start()
    win.expose(state, fit, detail_size, theme, insights, fit_insights, report, trace,
               login_options, start_login, cancel_login, rename_account, remove_account, move_account, switch_account, set_gemini_enabled,
               toggle_shared_split, set_share_plan, calibrate_share, reset_share_calibration, toggle_remote_view,
               toggle_cycle_start, set_cycle_times)
    log("start", page, "allow_eval=", ALLOW_EVAL, "hidden=", HIDDEN)
    if not MAC:
        protect_window(win)
    win.events.loaded += check
    timer = threading.Timer(40, win.destroy)
    timer.daemon = True
    timer.start()
    webview.settings.update(ALLOW_DOWNLOADS=False, ALLOW_FILE_URLS=False, IGNORE_SSL_ERRORS=False,
                            REMOTE_DEBUGGING_PORT=None, OPEN_DEVTOOLS_IN_DEBUG=False)
    webview.start(gui=None if MAC else "edgechromium", debug=False, private_mode=True, storage_path=temp)
    timer.cancel()
print(json.dumps(results))
sys.exit(0 if results and results[0].get("bridge") and results[0].get("csp")
         and results[0].get("networkBlocked") and (MAC or results[0].get("navigationBlocked"))
         and results[0].get("renderedData") and results[0].get("compactLayout") and results[0].get("accountManagement")
         and results[0].get("shareSplit") and results[0].get("shareBox") and (page != "main.html" or NO5 or smoke_share["solo"] == ["week", "h5", "reset"] and smoke_share["plan"] == "max20")
         and results[0].get("content", 0) > 0 else 1)
