"""서비스 하나 = 모듈 하나. 새 서비스는 아래 모양의 모듈을 만들고 meta.py 와 아래 __getattr__ 에 넣으면 위젯·트레이·장애 확인에 붙는다.

    KEY, NAME          카드 키(기록·설정에 저장되므로 바꾸지 않는다), 표시 이름 — meta.py 에서 가져온다
    STATUS_URL         Statuspage 상태 JSON 주소 또는 None
    LOGIN              계정 추가 정보 {"env": 설정 폴더 환경변수, "cmd": 로그인 명령, "file": 로그인 파일 이름,
                       "cli": 안내용 이름} 또는 None(계정 추가 미지원)
    fetch(path=None)   {"u5", "r5", "uw", "rw", "plan", "src", "extra"?} — path 는 추가 계정의 로그인 파일
    LOGIN 이 있으면:   DEFAULT_FILE, valid(cred_json), email(path=None), find_profiles()
    작업 계정 바꾸기:  swap_files(folder), switch_locks(folders), swap(folder, parked), switch_ready()?
                       — 기본 로그인 자리와 추가 계정 폴더의 로그인 맞바꾸기(common.switch_account 가 부른다)

화면 프로세스는 providers.meta(와 토큰과 무관한 codex_log)만 불러온다. ALL / BY_KEY 를 처음 읽을 때에야
토큰을 읽는 서비스 모듈을 불러오므로, 그건 자격 증명 브로커(provider_worker.py)에서만 일어난다.
"""


def __getattr__(name):
    if name not in ("ALL", "BY_KEY"):
        raise AttributeError(name)
    from . import claude, codex, gemini
    all_ = [claude, codex, gemini]
    globals().update(ALL=all_, BY_KEY={p.KEY: p for p in all_})
    return globals()[name]
