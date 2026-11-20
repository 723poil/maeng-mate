"""수집 연결·이동 주소·회사 규칙을 하나의 SQLite JSON 문서로 저장한다."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace

from src.application.category_upgrade import upgrade_category_preferences
from src.application.notion_page_links import page_url
from src.application.preferences import parse_preferences, preferences_payload
from src.application.work_settings import (
    Settings,
    WorkSettingsDocument,
    WorkSettingsError,
    is_initial_document,
    parse_settings,
    source_documents,
)
from src.application.work_settings_upgrade import settings_up_to_date, upgrade_work_settings
from src.domain.category_event_alerts import EVENT_ALERT_TRIGGERS, EventAlertSelection
from src.domain.preferences import (
    NOTIFICATION_TYPES,
)
from src.domain.source_display import SOURCE_CHANNELS
from src.infrastructure.sqlite.connection import Database
from src.infrastructure.sqlite.field_alerts import retire_changed_rules
from src.infrastructure.sqlite.preference_document import read_preferences, write_preferences
from src.infrastructure.sqlite.preferences import revoke_muted_deliveries


def read_work_settings_row(connection: sqlite3.Connection) -> tuple[int, str | bytes] | None:
    """같은 행에서 확인한 버전과 아직 해석하지 않은 저장 원문을 반환한다."""
    row = connection.execute("SELECT revision,payload FROM work_settings WHERE id=1").fetchone()
    if row is None:
        return None
    revision, encoded = row["revision"], row["payload"]
    if type(revision) is not int or revision < 1 or not isinstance(encoded, (str, bytes)):
        raise WorkSettingsError("invalid")
    return revision, encoded


def validate_work_document(
    revision: int, encoded: str | bytes
) -> tuple[WorkSettingsDocument, Settings]:
    """원본 문서와 매핑을 함께 검증하고 손상된 자료를 기본값으로 바꾸지 않는다."""
    raw: object = json.loads(encoded)
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise WorkSettingsError("invalid")
    payload: dict[str, object] = {key: value for key, value in raw.items()}
    settings = parse_settings(payload)
    return WorkSettingsDocument(revision, payload), settings


def _read_validated(connection: sqlite3.Connection) -> tuple[WorkSettingsDocument, Settings] | None:
    """저장된 행이 있을 때만 업무 설정을 검증한다."""
    row = read_work_settings_row(connection)
    return validate_work_document(*row) if row is not None else None


def read_document(connection: sqlite3.Connection) -> WorkSettingsDocument | None:
    """원본 문서만 필요한 호출자에게도 검증한 자료를 반환한다."""
    stored = _read_validated(connection)
    return stored[0] if stored is not None else None


def _write(connection: sqlite3.Connection, revision: int, payload: dict[str, object]) -> Settings:
    """저장할 문서를 검증하고 같은 트랜잭션의 후속 처리에 검증값을 돌려준다."""
    settings = parse_settings(payload)
    connection.execute(
        "INSERT INTO work_settings(id,revision,payload) VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
        (revision, json.dumps(payload, ensure_ascii=False)),
    )
    return settings


class SQLiteWorkSettings:
    """시작 설정과 링크 편집이 같은 문서·revision을 사용하게 한다."""

    def __init__(
        self, database: Database, *, notification_groups: Mapping[str, str] | None = None
    ) -> None:
        """DB와 현재 실행 중인 소스의 알림 그룹을 받는다."""
        self._database = database
        self._notification_groups = dict(notification_groups or {})

    def read(self) -> WorkSettingsDocument | None:
        """일반 실행은 설정 문서만 읽고 파일을 생성하거나 이전하지 않는다."""
        stored = self.read_validated()
        return stored[0] if stored is not None else None

    def read_validated(self) -> tuple[WorkSettingsDocument, Settings] | None:
        """편집기는 읽기 트랜잭션에서 검증한 매핑으로 같은 버전의 화면을 구성한다."""
        with self._database.transaction(read_only=True) as connection:
            return _read_validated(connection)

    def initialize(
        self, payload: dict[str, object], *, replace_initial: bool = False
    ) -> WorkSettingsDocument:
        """최초 한 번만 기존 JSON과 화면에서 수정한 링크를 함께 이전한다."""
        settings = parse_settings(payload)
        sources = source_documents(payload)
        with self._database.transaction() as connection:
            current = read_document(connection)
            if current is not None and not (replace_initial and is_initial_document(current)):
                return current
            legacy = connection.execute(
                "SELECT revision,payload FROM notion_page_link_preferences WHERE id=1"
            ).fetchone()
            revision = current.revision + 1 if current else 1
            if legacy is not None:
                links: object = json.loads(legacy["payload"])
                if (
                    not isinstance(links, dict)
                    or type(legacy["revision"]) is not int
                    or legacy["revision"] < 1
                    or any(not isinstance(key, str) or not key for key in links)
                ):
                    raise WorkSettingsError("invalid")
                for value in links.values():
                    page_url(value)
                # 파일보다 최근인 화면 저장값을 우선하고 이전 응답의 버전보다 높인다.
                revision = max(revision, legacy["revision"] + 1)
                for source, mapping in zip(sources, settings.sources, strict=True):
                    source["dataSourceId"] = mapping.source_id
                    if mapping.source_id in links:
                        url = page_url(links[mapping.source_id])
                        if url is None:
                            source.pop("pageUrl", None)
                        else:
                            source["pageUrl"] = url
            document = {**payload, "sources": sources}
            _write(connection, revision, document)
        return WorkSettingsDocument(revision, document)

    def prepare_categories(self) -> tuple[WorkSettingsDocument, Settings] | None:
        """이전 그룹·표시·알림 선택을 서버 시작 전에 원자적으로 이전한다."""
        stored = self.read_validated()
        if stored is None:
            return None
        observed, settings = stored
        if settings_up_to_date(observed, settings):
            return stored
        with self._database.transaction() as connection:
            latest = _read_validated(connection)
            if latest is None:
                return None
            # 읽기 잠금을 놓은 뒤 다른 창이 저장했을 수 있어 방금 읽은 버전을 이전한다.
            current, _ = latest
            upgrade = upgrade_work_settings(current)
            if upgrade is None:
                return latest
            document = upgrade.document
            settings = _write(connection, document.revision, document.payload)
            if upgrade.category_keys is not None:
                preferences = read_preferences(connection, SOURCE_CHANNELS)
                if upgrade.category_keys or preferences.revision > 0:
                    write_preferences(
                        connection,
                        upgrade_category_preferences(preferences, upgrade.category_keys, settings),
                    )
            return document, settings

    def save_links(
        self, revision: int, links: Mapping[str, str | None]
    ) -> tuple[WorkSettingsDocument, Settings]:
        """주소만 고치며 매핑·규칙을 보존하고 동시 수정은 덮어쓰지 않는다."""
        with self._database.transaction() as connection:
            stored = _read_validated(connection)
            if stored is not None:
                current, settings = stored
                if current.revision == revision and links.keys() <= {
                    source.source_id for source in settings.sources
                }:
                    sources = source_documents(current.payload)
                    for source, mapping in zip(sources, settings.sources, strict=True):
                        if mapping.source_id not in links:
                            continue
                        url = page_url(links[mapping.source_id])
                        if url is None:
                            source.pop("pageUrl", None)
                        else:
                            source["pageUrl"] = url
                    payload = {**current.payload, "sources": sources}
                    saved = _write(connection, revision + 1, payload)
                    return WorkSettingsDocument(revision + 1, payload), saved
        # 충돌은 손상된 저장 자료와 구분해 편집 화면에 전달한다.
        raise WorkSettingsError("conflict")

    def save_document(
        self,
        revision: int,
        payload: dict[str, object],
        *,
        event_selections: tuple[EventAlertSelection, ...] = (),
    ) -> tuple[WorkSettingsDocument, Settings]:
        """읽은 버전이 유지될 때만 소스와 카테고리 변경을 함께 확정한다."""
        with self._database.transaction() as connection:
            stored = _read_validated(connection)
            if stored is not None and stored[0].revision == revision:
                current, before = stored
                # 빈 초기 문서의 revision은 유지되므로 이전 직전에 읽은 편집도 완료 표식을 보존한다.
                if (
                    "mainListRulesVersion" not in payload
                    and "mainListRulesVersion" in current.payload
                ):
                    payload = {
                        **payload,
                        "mainListRulesVersion": current.payload["mainListRulesVersion"],
                    }
                after = _write(connection, revision + 1, payload)
                retire_changed_rules(connection, before, after)
                save_event_subscriptions(
                    connection, before, after, event_selections, self._notification_groups
                )
                return WorkSettingsDocument(revision + 1, payload), after
        raise WorkSettingsError("conflict")


def registered_events(settings: Settings) -> set[tuple[str, str]]:
    """사용자 이벤트는 카테고리와 조건으로 식별한다."""
    return {
        (item.id, rule.trigger)
        for item in settings.categories
        if item.configuration is not None
        for rule in item.configuration.event_alerts or ()
    }


def save_event_subscriptions(
    connection: sqlite3.Connection,
    before: Settings,
    after: Settings,
    selections: tuple[EventAlertSelection, ...],
    notification_groups: Mapping[str, str],
) -> None:
    """규칙 저장과 기존 알림 화면의 스위치 변경을 한 트랜잭션으로 끝낸다."""
    removed = registered_events(before) - registered_events(after)
    updates = [
        *selections,
        *(EventAlertSelection(category, trigger, False) for category, trigger in sorted(removed)),
    ]
    deleted_groups = {item.key for item in before.categories} - {
        item.key for item in after.categories
    }
    if not updates and not deleted_groups:
        return
    current = read_preferences(connection, SOURCE_CHANNELS)
    muted = dict(current.muted_source_notifications)
    default = {kind for kind in NOTIFICATION_TYPES if kind not in current.notification_types}
    for item in updates:
        key = f"category:{item.category_id}"
        previous = set(muted.get(key, default))
        kinds = set(EVENT_ALERT_TRIGGERS[item.trigger].kinds)
        selection = previous - kinds if item.enabled else previous | kinds
        if selection != default or key in muted:
            muted[key] = tuple(sorted(selection))
    # 아직 실행 중인 그룹을 취소한 뒤, 삭제한 카테고리의 설정 키를 정리한다.
    revoke_muted_deliveries(
        connection, replace(current, muted_source_notifications=muted), notification_groups
    )
    remaining = {key: kinds for key, kinds in muted.items() if key not in deleted_groups}
    if remaining != current.muted_source_notifications:
        saved = replace(
            current, revision=current.revision + 1, muted_source_notifications=remaining
        )
        write_preferences(
            connection, parse_preferences(preferences_payload(saved), SOURCE_CHANNELS)
        )
