<img src="src/assets/app-icon.svg" width="80" height="80" alt="Catch 앱 아이콘">

# Catch

Mac 메뉴바에서 직접 연결한 Notion 표에서 보고 싶은 항목을 확인하고 출퇴근을 기록하는 앱입니다. 여러 표에 흩어진 마감일과 회의를 달력 하나에 모아 보고, 항목을 누르면 Notion 원본으로 이동합니다.

[공식 사이트](https://maeng-mate.lucidalpaca32.chatgpt.site) · [최신 버전 다운로드](https://github.com/723poil/maeng-mate/releases/latest) · [사용 가이드](docs/usage.md) · [릴리스 노트](https://github.com/723poil/maeng-mate/releases)

## 주요 기능

- **내 목록** — 카테고리를 직접 만들고 메뉴바에 표시할 카드를 선택합니다. 자주 보는 카드를 위에 두거나 마감일과 우선순위로 목록을 정렬할 수 있습니다.
- **출퇴근 기록** — 출근 시각을 입력하면 휴게 시간과 휴가를 반영해 퇴근 예정 시각을 계산합니다. 메뉴바에서 퇴근 시각이나 퇴근까지 남은 시간을 보고, 실제 퇴근 시각은 월별 기록에서 확인하고 고칩니다.
- **알림** — 카테고리의 저장 칼럼과 직접 입력한 값으로 규칙을 만들고 macOS 알림이나 Slack DM으로 받습니다. 퇴근 알림은 받을 시각을 분 단위로 직접 정합니다.
- **AI에서 조회** — Codex나 Claude Code를 읽기 전용 MCP에 연결하면 “오늘 내 회의가 뭐야?”처럼 물어볼 수 있습니다. 앱 설정에서 두 클라이언트의 업무 조회 스킬을 설치하고 업데이트할 수 있습니다.

## 앱 설치

**Apple Silicon Mac과 macOS 26 이상**이 필요합니다. 배포 앱에 Python과 의존성이 포함되어 있습니다.

1. [최신 릴리스](https://github.com/723poil/maeng-mate/releases/latest)의 **Assets**에서 `.dmg` 파일을 내려받아 엽니다.
2. **Catch.app**을 **응용 프로그램(Applications)** 폴더로 옮긴 뒤 실행합니다. 이전 이름의 **Maeng Mate.app**이 있으면 지웁니다. 기록과 설정은 그대로 이어집니다.
3. 메뉴바에서 Catch 아이콘을 누릅니다. 첫 실행 때 다음 로그인부터 자동으로 시작하도록 등록됩니다.

현재 릴리스는 Apple 공증 전이라 처음 열 때 macOS가 차단할 수 있습니다. 이때는 **시스템 설정 → 개인정보 보호 및 보안**에서 **그래도 열기**를 누릅니다.

새 버전은 메뉴바 앱이 하루 간격으로 확인하고, 변경 내용을 보여 준 뒤 지금 설치할지 묻습니다. **설정 → 앱 정보 → 업데이트 확인…**으로 바로 확인할 수도 있습니다. 이 버튼이 없는 이전 버전(맹메이트)은 앱을 종료하고 최신 DMG로 한 번 교체하세요. 기존 기록과 설정은 백업한 뒤 그대로 이어서 사용합니다.

## 처음 Notion 연결하기

1. 메뉴바에서 **Notion 연결…**을 누릅니다. 브라우저에 Notion 토큰 페이지가 열리면 새 토큰을 만들고 **Copy**를 누릅니다. Catch가 복사한 토큰을 확인해 키체인에 저장하고, 내 Notion 계정을 지정한 뒤 다시 시작합니다.
2. 메뉴바의 **표 연결…**을 누릅니다. **카테고리**에서 ‘업무’·‘회의’처럼 메뉴바에 보일 카드를 만들고, **수집 소스**에서 Notion 표를 고른 뒤 카테고리를 지정하고 저장합니다.
3. **다시 시작**하면 첫 수집 뒤 메뉴바에 항목이 나타납니다.

회사 워크스페이스에서 개인 토큰을 만들 수 없으면 버튼 옆 메뉴의 **통합 연결로 만들기…**를 쓰고, 연결할 표를 Notion에서 그 통합에 공유합니다.

수집한 업무와 출퇴근 기록은 Mac의 SQLite에 저장하고, Notion·Slack 토큰은 macOS 키체인에 보관합니다. Slack DM과 AI 조회는 필요할 때 각각 연결할 수 있습니다. [알림과 Slack 설정](docs/usage.md#설정) · [MCP 연결](docs/usage.md#mcp)

## 개발 환경

지원되는 Mac에서 저장소를 복제한 뒤, 프로젝트 루트에서 실행하세요. mise가 저장소에 고정된 Python 3.12와 uv를 설치합니다.

```sh
git clone https://github.com/723poil/maeng-mate.git
cd maeng-mate
mise trust
mise install
mise exec -- uv sync --locked --extra menubar
mise run check
```

개발용과 배포용 메뉴바를 같은 Mac에서 함께 실행할 수 있습니다. 개발용은 전용 DB와 키체인을 쓰므로 배포용의 기록과 연동에 영향을 주지 않습니다. 기존 설치와 호환되도록 저장소·명령·번들 식별자는 `maeng-mate` 이름을 유지합니다.

| 작업 | 프로젝트 루트에서 실행할 명령 |
| --- | --- |
| 로컬 DB 준비 | `mise exec -- uv run maeng-mate migrate` |
| 메뉴바 앱과 백그라운드 서비스 설치·갱신 | `mise exec -- uv run maeng-mate service install` |
| 서비스 상태 확인 | `mise exec -- uv run maeng-mate service status` |
| 앱과 설치 파일 빌드 | `mise run package` |

`mise run check`는 Ruff·포맷·mypy를 검사합니다. 패키지 빌드는 앱, DMG, SHA-256 파일을 `dist/`에 만듭니다. `CODESIGN_IDENTITY`에 Developer ID 인증서 이름을, `NOTARY_PROFILE`에 `notarytool` 자격 프로필 이름을 주면 서명과 Apple 공증까지 진행합니다.

## 프로젝트 문서

- [사용 및 운영 가이드](docs/usage.md) — 메뉴바·캘린더 설정, 알림, 서비스 명령, MCP 도구
- [아키텍처 결정 기록](docs/adr/README.md) — Notion 수집, 로컬 저장소, API와 메뉴바 앱의 역할
- [개발 작업 지침](AGENTS.md) · [릴리스 절차](docs/adr/0010-macos-distribution.md#릴리스-브랜치와-github-actions)

## 라이선스

Catch의 코드와 문서는 [MIT 라이선스](LICENSE)로 배포합니다. 복사본이나 코드의 상당 부분을 재배포할 때는 저작권 표시와 라이선스 문구를 함께 제공하세요.

외부 구성요소에는 각각의 라이선스가 적용됩니다. 배포 앱의 Sparkle 고지는 앱 번들의 `Contents/Resources/Sparkle-LICENSE`에, 웹사이트의 Pretendard 글꼴 고지는 [OFL.txt](site/assets/fonts/OFL.txt)에 있습니다.
