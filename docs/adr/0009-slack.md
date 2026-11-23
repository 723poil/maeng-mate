# ADR 0009. Slack 연동과 DM

상태: 채택

## 맥락

기존 알림 종류를 본인의 Slack DM으로도 받고, 자격 정보와 발송 권한을 설정에서 확인한다.

## 결정

- 본인 멤버 ID는 `providers.slack.userId`, 봇·앱 토큰은 Keychain의 개별 항목에 둔다. 각 카드는 독립 저장하고 토큰 원문을 다시 채우지 않는다. 앱 토큰은 형식을 검증해 보관하며 Socket Mode를 연결하지 않는다.
- 봇 확인은 `auth.test`로 자격 정보를 확인한다. 발송 권한과 수신자 확인은 별개다. 저장된 멤버 ID와 확인된 봇 토큰이 준비되고 편집·요청이 진행 중이 아닐 때 테스트를 사용할 수 있다. 버튼을 누르면 저장 버전을 다시 확인하고 고정 문구를 본인에게 한 건 보낸다.
- 관리 HTTP의 `CredentialError`는 [공통 자격 정보 정책](0006-settings-and-notion.md)을 따른다. 발송 전용 `SlackTestError`의 `recipient`·`unverified`·`delivery_unknown`은 Slack 경로에서 별도로 기존 503과 공개 코드를 유지한다.
- 자동 DM은 기본 꺼짐이며 알림 설정의 채널·종류 선택을 공유한다. 메뉴바는 실행권 ID·토큰을 로컬 관리자 API에 전달한다. 서버는 저장된 알림 본문·멤버 ID·Keychain 봇 토큰으로 `chat.postMessage`를 호출한다. 앱 토큰은 발송에 필요 없다.
- 서버는 실행권에 요청 시작을 한 번 기록하고 전송 결과를 먼저 확정한다. 늦은 일반 ACK가 서버 결과를 덮지 못한다. 명시적 거부와 결과 미확인을 구별하며 자동 재시도하지 않는다.
- 테스트와 자동 DM은 같은 발송 어댑터를 쓴다. 인증 확인과 발송은 HTTP 요청·응답 검증과 공통 거부 코드를 공유한다. 인증 확인의 타임아웃은 `timeout`, DM의 응답 유실은 `delivery_unknown`으로 구분한다. 본문은 일반 텍스트, 검증된 Notion 제목만 구조화 링크로 전달한다. 미리보기와 원문 멘션 해석은 끈다.

## 결과와 검증

macOS와 Slack DM을 함께 켤 수 있다. 봇은 `chat:write`가 필요하다. `src/application/slack_settings.py`, `slack_notifications.py`, `src/infrastructure/slack_messages.py`에서 구현한다. 가짜 Slack 응답으로 중복·취소·권한 오류·응답 유실을 확인한다.
