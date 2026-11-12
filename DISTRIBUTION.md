# 배포

소스와 설치파일을 공개 저장소 하나(`daowle/Quota-Tank`)에서 배포한다. 라이선스는 [MIT](LICENSE).

## 릴리스 순서

1. `widget.py` 의 `VERSION` 을 새 버전으로 올리고(예: `0.2.1`) 전체 테스트를 통과시킨다.
2. 같은 번호의 태그를 올린다: `git tag v0.2.1` → `git push origin v0.2.1`.
3. `.github/workflows/release.yml` 이 윈도우 exe·MSIX와 맥(Apple Silicon) zip을 빌드하고, 같은 저장소에 **초안 릴리스**를 만든다.
   태그와 `VERSION` 이 다르면 빌드를 멈춘다.
4. 초안에 변경 내역을 적고 **Publish** 한다. 공개된 릴리스만 위젯의 새 버전 알림(GitHub 최신 릴리스 확인)에 잡힌다.

맥 한 줄 설치(`install.sh`)는 `main` 브랜치의 스크립트와 최신 릴리스의 zip·`.sha256` 을 받는다.

## 자동화 경계

- PR·수동 실행은 빌드 결과를 Actions 아티팩트로만 남긴다. 릴리스는 태그 push 에서만 만든다.
- 릴리스 작업은 저장소 기본 `GITHUB_TOKEN`(contents: write)만 쓴다. 다른 저장소에 쓰는 토큰은 없다.
- 서명은 [SIGNING.md](SIGNING.md)를 따른다. 서명을 설정하지 않으면 exe 는 서명 없이 올라가고, 설치가 안 되는 서명 없는 MSIX 는 릴리스에 넣지 않는다.
- 자동 변경 내역 생성은 끄고 검토한 문구만 넣는다.

## 배포 전 확인

전체 테스트와 번들 검사(`tools/check_bundle.py`)를 통과하고 Python 없는 PC에서 실행·종료·로그인·알림을 확인한다.
보안 범위는 [SECURITY.md](SECURITY.md)를 따른다.
외부 라이브러리·Python 런타임의 라이선스 고지를 확인하고, Pretendard 고지(`web/fonts/LICENSE.txt`)를 앱 번들에 포함한다.
Intel 맥 빌드와 Apple 서명·공증은 아직 없다.

개발자 API 키나 공용 인증정보를 저장소·실행파일에 넣지 않는다.
