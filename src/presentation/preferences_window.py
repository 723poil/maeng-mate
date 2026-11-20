"""좌측 기능 탐색과 우측 편집을 분리한 네이티브 설정 창이다."""

from collections.abc import Callable
from dataclasses import replace
from importlib.metadata import version
from typing import Literal

from AppKit import (
    NSApplication,
    NSBox,
    NSBoxSeparator,
    NSNoBorder,
    NSScreen,
    NSScrollView,
    NSTextAlignmentRight,
    NSView,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorMoveToActiveSpace,
)
from Foundation import NSNotification, NSObject

from src.application.credentials import CredentialError, validated_secret
from src.application.data_backup import DataBackupActions
from src.application.notifications import NotificationChannel
from src.application.source_settings import SourceSettingsClient, SourceSettingsError
from src.application.work_rules_settings import WorkRulesDocument
from src.domain.preferences import NOTIFICATION_TYPES, SECTIONS, SectionPreference
from src.presentation.category_settings_form import CategorySettingsForm
from src.presentation.data_backup_view import DataBackupView
from src.presentation.glass_panel import auxiliary_window
from src.presentation.mcp_settings_controller import McpSettingsController
from src.presentation.mcp_settings_view import McpSettingsView
from src.presentation.native_views import button, card, frame, glass_container, label, place_label
from src.presentation.notification_identity_controller import NotificationIdentityController
from src.presentation.notification_identity_view import NotificationIdentityView
from src.presentation.notification_preferences import NotificationPreferences
from src.presentation.notion_credentials_controller import NotionCredentialsController
from src.presentation.notion_credentials_view import NotionCredentialsView
from src.presentation.notion_page_links_controller import NotionPageLinksController
from src.presentation.notion_page_links_view import NotionPageLinksView
from src.presentation.preferences_controller import PreferencesController
from src.presentation.preferences_sidebar import PreferencesSidebar, SettingsPage
from src.presentation.runtime_settings_controller import RuntimeSettingsController
from src.presentation.runtime_settings_form import RuntimeSettingsForm
from src.presentation.section_order import SectionOrderList
from src.presentation.slack_settings_controller import SlackSettingsController
from src.presentation.slack_settings_view import SlackSettingsView
from src.presentation.source_sections import source_sections
from src.presentation.source_settings_form import SourceEditor, SourceSettingsForm
from src.presentation.update_button import UpdateChecks
from src.presentation.work_rules_form import WorkRulesEditor, WorkRulesForm


class PreferencesActions(NSObject):
    """설정 창의 네이티브 입력을 화면 상태로 연결한다."""

    owner: "PreferencesWindow"

    def ruleSources_(self, sender: object) -> None:
        """규칙과 전달 채널을 편집하는 카테고리로 이동한다."""
        self.owner._sidebar.select(SettingsPage.CATEGORIES)

    def applyDeparture_(self, sender: object) -> None:
        """직접 입력한 퇴근 알림 시각만 저장한다."""
        self.owner.apply_departure_offsets()

    def changed_(self, sender: object) -> None:
        """선택 변경을 자동 저장 초안으로 전달한다."""
        self.owner.changed()

    def windowWillClose_(self, notification: NSNotification) -> None:
        """기본 닫기 버튼과 단축키 모두에서 임시 인증 표시를 지운다."""
        self.owner.clear_private_fields()

    def reload_(self, sender: object) -> None:
        """확정 설정을 다시 읽어 충돌·연결 실패를 복구한다."""
        if (
            self.owner._selected_page in (SettingsPage.SOURCES, SettingsPage.CATEGORIES)
            and self.owner._source_form is not None
        ):
            if not self.owner._source_form.busy:
                self.owner._source_form.editor.reload()
        elif (
            self.owner._selected_page == SettingsPage.WORK_RULES
            and self.owner._work_rules is not None
        ):
            self.owner._work_rules.reload()
        elif (
            self.owner._selected_page == SettingsPage.SLACK
            and self.owner._slack_controller is not None
        ):
            self.owner._slack_controller.reload(allow_prompt=True)
        elif (
            self.owner._selected_page == SettingsPage.MCP and self.owner._mcp_controller is not None
        ):
            self.owner._mcp_controller.reload()
        elif (
            self.owner._selected_page == SettingsPage.NOTION
            and self.owner._runtime_controller is not None
        ):
            self.owner._runtime_controller.reload()
        else:
            self.owner.controller.reload()
        if (
            self.owner._selected_page == SettingsPage.NOTION
            and self.owner._credentials_controller is not None
        ):
            self.owner._credentials_controller.reload(allow_prompt=True)
        if (
            self.owner._selected_page == SettingsPage.NOTION
            and self.owner._page_links_controller is not None
        ):
            self.owner._page_links_controller.reload()
        self.owner.render()

    def runtimeEdited_(self, sender: object) -> None:
        """입력은 즉시 저장하지 않고 초안 변경으로 표시한다."""
        if self.owner._runtime_controller is not None:
            self.owner._runtime_controller.edited()
        self.owner.render()

    def controlTextDidChange_(self, notification: NSNotification) -> None:
        """문자 입력도 페이지 이동 전에 초안 상태로 남긴다."""
        if (
            self.owner._credentials_view is not None
            and notification.object() == self.owner._credentials_view.token
        ):
            self.owner._credentials_controller_input()
        else:
            self.runtimeEdited_(notification)

    def saveRuntime_(self, sender: object) -> None:
        """검증된 폼을 명시적으로 저장한다."""
        if self.owner._selected_page == SettingsPage.SOURCES:
            self.owner.save_sources()
        elif self.owner._selected_page == SettingsPage.WORK_RULES:
            self.owner.save_work_rules()
        else:
            self.owner.save_runtime()

    def restartRuntime_(self, sender: object) -> None:
        """저장 완료 뒤 관리 서비스로 다시 시작한다."""
        if (
            self.owner._selected_page in (SettingsPage.SOURCES, SettingsPage.CATEGORIES)
            and self.owner._source_form is not None
        ):
            if not self.owner._source_restart_blocked():
                self.owner._source_form.editor.restart()
        elif (
            self.owner._selected_page == SettingsPage.WORK_RULES
            and self.owner._work_rules is not None
        ):
            if not self.owner.work_rules_restart_blocked():
                self.owner._work_rules.restart()
        elif self.owner._runtime_controller is not None and not self.owner.source_edits_pending():
            credentials = self.owner._credentials_controller
            self.owner._runtime_controller.restart(
                provider_pending=bool(credentials and credentials.restart_required)
            )
        self.owner.render()

    def saveCredential_(self, sender: object) -> None:
        """새 토큰은 확인 후 한 번 저장한다."""
        self.owner.perform_credential("save")

    def checkCredential_(self, sender: object) -> None:
        """현재 저장된 토큰만 확인한다."""
        self.owner.perform_credential("check")

    def deleteCredential_(self, sender: object) -> None:
        """인증 카드의 명시적 삭제를 전달한다."""
        self.owner.perform_credential("delete")


class PreferencesWindow:
    """기능별 페이지를 전환하면서 입력·스크롤·자동 저장을 보존한다."""

    def __init__(
        self,
        controller: PreferencesController,
        channels: tuple[NotificationChannel, ...],
        runtime_controller: RuntimeSettingsController | None = None,
        credentials_controller: NotionCredentialsController | None = None,
        mcp_controller: McpSettingsController | None = None,
        page_links_controller: NotionPageLinksController | None = None,
        identity_controller: NotificationIdentityController | None = None,
        slack_controller: SlackSettingsController | None = None,
        work_rules: WorkRulesEditor | None = None,
        pending_work_edits: Callable[[], bool] | None = None,
        source_editor: SourceEditor | None = None,
        source_client: SourceSettingsClient | None = None,
        data_backup: DataBackupActions | None = None,
        app_updates: UpdateChecks | None = None,
    ) -> None:
        """설정 작업자와 설치된 채널만 주입받는다."""
        self._render_state: object = None
        self._pending_work_edits = pending_work_edits
        self._source_form: SourceSettingsForm | None = None
        self._category_form: CategorySettingsForm | None = None
        self._collection_page = SettingsPage.SOURCES
        self._backup_view: DataBackupView | None = None
        self._work_rules = work_rules
        self._work_rules_form: WorkRulesForm | None = None
        self._slack_controller = slack_controller
        self._slack_view: SlackSettingsView | None = None
        self._selected_page = SettingsPage.SCREEN
        self.controller = controller
        self._runtime_controller = runtime_controller
        self._credentials_controller = credentials_controller
        self._mcp_controller = mcp_controller
        self._page_links_controller = page_links_controller
        self._page_links_view: NotionPageLinksView | None = None
        self._identity_controller = identity_controller
        self._identity_view: NotificationIdentityView | None = None
        self._mcp_view: McpSettingsView | None = None
        self._credentials_view: NotionCredentialsView | None = None
        self._runtime_form: RuntimeSettingsForm | None = None
        self._actions = PreferencesActions.alloc().init()
        self._actions.owner = self
        screen = NSScreen.mainScreen()
        height = min(600.0, max(500.0, screen.visibleFrame()[1][1] - 60)) if screen else 600.0
        self._window = auxiliary_window("Maeng Mate 설정", 760, height)
        self._window.setCollectionBehavior_(
            NSWindowCollectionBehaviorMoveToActiveSpace
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._window.setDelegate_(self._actions)
        content = NSView.alloc().initWithFrame_(frame(0, 0, 760, height))
        self._glass = glass_container(content, 760, height)
        self._window.setContentView_(self._glass)
        label(content, "설정", 82, height - 44, 60, size=17, bold=True)
        if app_updates:
            app_updates.attach(content)
        else:
            label(content, f"버전 {version('maeng-mate')}", 20, 22, 120, size=12, secondary=True)
        self._page_title = label(
            content, SettingsPage.SCREEN.title, 188, height - 44, 450, size=17, bold=True
        )
        divider = NSBox.alloc().initWithFrame_(frame(148, 16, 1, height - 32))
        divider.setBoxType_(NSBoxSeparator)
        content.addSubview_(divider)
        viewport = height - 102
        pages: tuple[SettingsPage, ...] = (SettingsPage.SCREEN, SettingsPage.NOTIFICATIONS)
        if runtime_controller is not None:
            pages += (SettingsPage.NOTION,)
        if mcp_controller is not None:
            pages += (SettingsPage.MCP,)
        if slack_controller is not None:
            pages += (SettingsPage.SLACK,)
        if work_rules is not None:
            pages += (SettingsPage.WORK_RULES,)
        if source_editor is not None and source_client is not None:
            pages += (SettingsPage.SOURCES, SettingsPage.CATEGORIES)
        if data_backup is not None:
            pages += (SettingsPage.BACKUP,)
        self._pages = {page: self._page(content, viewport) for page in pages}
        if data_backup is not None:
            self._backup_view = DataBackupView(
                self._pages[SettingsPage.BACKUP], viewport, data_backup, self._backup_edits_pending
            )
        self._build_screen_page(self._pages[SettingsPage.SCREEN], viewport)
        self._build_notification_page(self._pages[SettingsPage.NOTIFICATIONS], viewport, channels)
        if runtime_controller is not None:
            self._runtime_form = RuntimeSettingsForm(
                self._pages[SettingsPage.NOTION], viewport, runtime_controller, self._actions
            )
        if credentials_controller is not None:
            page = self._pages.get(SettingsPage.NOTION)
            document = page.documentView() if page else None
            if document is not None:
                self._credentials_view = NotionCredentialsView(
                    document, credentials_controller, self._actions
                )
        if page_links_controller is not None:
            page = self._pages.get(SettingsPage.NOTION)
            document = page.documentView() if page else None
            if document is not None:
                self._page_links_view = NotionPageLinksView(document, page_links_controller)
        if mcp_controller is not None:
            self._mcp_view = McpSettingsView(
                self._pages[SettingsPage.MCP], viewport, mcp_controller
            )
        if slack_controller is not None:
            self._slack_view = SlackSettingsView(
                self._pages[SettingsPage.SLACK], viewport, slack_controller
            )
        if work_rules is not None:
            self._work_rules_form = WorkRulesForm(
                self._pages[SettingsPage.WORK_RULES], viewport, work_rules
            )
        if source_editor is not None and source_client is not None:
            self._source_form = SourceSettingsForm(
                self._pages[SettingsPage.SOURCES],
                viewport,
                source_editor,
                source_client,
                lambda: self._sidebar.select(SettingsPage.CATEGORIES),
            )
            self._category_form = CategorySettingsForm(
                self._pages[SettingsPage.CATEGORIES],
                viewport,
                source_editor,
                controller,
                source_client,
            )
        self._status = label(content, "설정 불러오는 중…", 188, 22, 402, size=12, secondary=True)
        self._reload = button(content, "다시 불러오기", 624, 18.5, 110, self._actions, "reload:")
        self._runtime_save = button(content, "저장", 556, 18.5, 80, self._actions, "saveRuntime:")
        self._runtime_restart = button(
            content, "다시 시작", 644, 18.5, 90, self._actions, "restartRuntime:"
        )
        self._sidebar = PreferencesSidebar(content, height, self._show_page, pages=pages)
        self._show_page(SettingsPage.SCREEN)
        self.render()

    def _page(self, parent: NSView, height: float) -> NSScrollView:
        """각 기능의 스크롤 위치를 별도 뷰에서 보존한다."""
        view = NSScrollView.alloc().initWithFrame_(frame(160, 50, 584, height))
        view.setHasVerticalScroller_(True)
        view.setAutohidesScrollers_(True)
        view.setDrawsBackground_(False)
        view.setBorderType_(NSNoBorder)
        parent.addSubview_(view)
        return view

    def _build_screen_page(self, page: NSScrollView, viewport: float) -> None:
        """화면 페이지에는 표시와 드래그 순서만 배치한다."""
        height = max(284.0, viewport)
        document = NSView.alloc().initWithFrame_(frame(0, 0, 584, height))
        page.setDocumentView_(document)
        rows_height = len(SECTIONS) * 32
        self._screen_card = card(document, 16, height - rows_height - 64, 552, rows_height + 56)
        self._screen_rows_top = height - 52
        self._screen_max_rows = max(2, int((viewport - 64) // 32))
        label(document, "표시와 순서", 28, height - 40, 480, size=13, bold=True)
        hint = label(document, "끌어서 순서 변경", 410, height - 39, 146, size=11, secondary=True)
        hint.setAlignment_(NSTextAlignmentRight)
        self._sections = SectionOrderList(
            document, 28, height - rows_height - 52, 528, self._sections_changed
        )
        document.scrollPoint_((0, max(0, height - viewport)))

    def _build_notification_page(
        self, page: NSScrollView, viewport: float, channels: tuple[NotificationChannel, ...]
    ) -> None:
        """알림 사용·채널·종류를 구분하고 카드 안쪽 시작점을 맞춘다."""
        controls = self._notification_view = NotificationPreferences(
            page,
            viewport,
            channels,
            self._actions,
            identity_height=144 if self._identity_controller else 0,
        )
        if self._identity_controller is not None:
            self._identity_view = NotificationIdentityView(
                controls.fixed,
                controls.fixed.frame()[1][1] - 8,
                self.controller,
                self._identity_controller,
            )
        self._enabled = controls.enabled
        self._channels = controls.channels

    def _show_page(self, selected: SettingsPage) -> None:
        """선택한 기능만 보여주고 다른 페이지의 입력 상태는 유지한다."""
        if selected not in self._pages:
            return
        if (
            selected in (SettingsPage.SOURCES, SettingsPage.CATEGORIES)
            and selected != self._collection_page
        ):
            if self.source_edits_pending():
                self._sidebar.select(self._collection_page)
                self._status.setStringValue_("현재 변경을 저장하거나 다시 불러오세요.")
                return
            self._collection_page = selected
        if self._page_links_view is not None:
            self._page_links_view.hide_help()
        slack = self._slack_controller
        if (
            selected == SettingsPage.SLACK
            and slack is not None
            and not (slack.busy or slack.member_dirty or slack.token_dirty or slack.app_dirty)
        ):
            slack.reload()
        self._selected_page = selected
        self._page_title.setStringValue_(selected.title)
        for page, view in self._pages.items():
            view.setHidden_(page != selected)
        runtime_page = selected in (
            SettingsPage.NOTION,
            SettingsPage.WORK_RULES,
            SettingsPage.SOURCES,
            SettingsPage.CATEGORIES,
        )
        self._runtime_save.setHidden_(not runtime_page or selected == SettingsPage.CATEGORIES)
        self._runtime_restart.setHidden_(not runtime_page)
        self._reload.setHidden_(selected == SettingsPage.BACKUP)
        self._reload.setFrame_(frame(438 if runtime_page else 624, 18.5, 110, 26))
        place_label(self._status, 188, 22, 238 if runtime_page else 402, 19)
        self._render_state = None
        self.render()

    def show(self) -> None:
        """다시 열어도 저장 중인 선택과 스크롤을 보존한다."""
        self.render()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def _sections_changed(self, sections: tuple[SectionPreference, ...]) -> None:
        """목록의 최신 순서와 표시 선택을 저장 초안에 반영한다."""
        value = self.controller.value
        if value is not None:
            visible_keys = {item.key for item in sections}
            source_state = self._source_form.editor.state if self._source_form is not None else None
            registered = {item.key for item in source_state.categories} if source_state else set()
            # 등록 카테고리에 소스가 없어 화면에 안 보이는 동안에도 표시 선택을 보존한다.
            omitted = tuple(
                item
                for item in value.sections
                if item.key in (*SECTIONS, *registered) and item.key not in visible_keys
            )
            self.controller.change(replace(value, sections=(*sections, *omitted)))
        self.render()

    def apply_departure_offsets(self) -> None:
        """설정된 다른 구독을 보존하고 퇴근 시각만 바꾼다."""
        value = self.controller.value
        if value is None:
            return
        try:
            offsets = self._notification_view.read_offsets()
        except ValueError as error:
            self._status.setStringValue_(str(error))
            return
        other = tuple(key for key in value.notification_types if not key.startswith("departure_"))
        self.controller.change(
            replace(
                value, notification_types=(*other, *(f"departure_{minutes}" for minutes in offsets))
            )
        )
        self.render()

    def changed(self) -> None:
        """표시와 알림 선택을 한 설정 버전의 초안으로 모은다."""
        value = self.controller.value
        if value is None:
            return
        muted = dict(value.muted_source_notifications)
        for (group, kinds), control in self._notification_view.source_types.items():
            if kinds[0] in self._notification_view.inactive_rules:
                continue
            enabled = control.state() == 1
            if enabled == any(value.notification_enabled(kind, group) for kind in kinds):
                continue
            disabled = set(
                muted.get(
                    group,
                    tuple(
                        kind for kind in NOTIFICATION_TYPES if kind not in value.notification_types
                    ),
                )
            )
            if enabled:
                disabled.difference_update(kinds)
            else:
                disabled.update(kinds)
            muted[group] = tuple(sorted(disabled))
        self.controller.change(
            replace(
                value,
                notifications_enabled=self._enabled.state() == 1,
                channels=tuple(
                    key for key, control in self._channels.items() if control.state() == 1
                ),
                muted_source_notifications=muted,
            )
        )
        self.render()

    def _credentials_controller_input(self) -> None:
        """보안 입력 변경을 일반 계정 폼의 수정으로 오해하지 않는다."""
        if self._credentials_controller is not None and self._credentials_view is not None:
            self._credentials_controller.edited(bool(self._credentials_view.token.stringValue()))
        self.render()

    def perform_credential(self, action: Literal["check", "save", "delete"]) -> None:
        """보안 입력을 저장 요청에서만 읽고 값이 없는 상태로 유지한다."""
        controller = self._credentials_controller
        view = self._credentials_view
        if controller is None or view is None:
            return
        try:
            token = validated_secret(view.token.stringValue()) if action == "save" else None
            controller.perform(action, token)
        except CredentialError:
            controller.error_code, controller.error = "invalid", "토큰 형식을 확인하세요."
        self.render()

    def save_runtime(self) -> None:
        """입력 오류는 폼에 남기고 검증된 값만 저장 작업자에게 전달한다."""
        if self._runtime_form is not None and self._runtime_controller is not None:
            try:
                self._runtime_controller.save(self._runtime_form.draft())
            except ValueError as error:
                self._runtime_controller.error = str(error)
            self.render()

    def _render_runtime(self) -> None:
        """저장·다시 읽기·재시작 상태를 일반 폼의 푸터에 표시한다."""
        controller = self._runtime_controller
        if controller is None or self._runtime_form is None:
            return
        self._runtime_form.render()
        credentials = self._credentials_controller
        if self._credentials_view is not None:
            self._credentials_view.render()
        links = self._page_links_controller
        if self._page_links_view is not None:
            self._page_links_view.render()

        credentials_busy = credentials is not None and credentials.busy
        links_busy = links is not None and links.busy
        provider_pending = credentials is not None and credentials.restart_required
        message = controller.message
        if provider_pending and not (controller.dirty or controller.busy or controller.restarting):
            message = "저장됨 · 다시 시작 후 적용"
        if links is not None and links.message:
            message = links.message
        self._status.setStringValue_(message)
        self._status.setToolTip_(message)

        # 같은 푸터를 쓰는 세 카드의 요청 상태를 모은 뒤 버튼 상태를 한 번만 적용한다.
        self._reload.setEnabled_(
            not (controller.busy or controller.restarting or credentials_busy or links_busy)
        )
        self._runtime_save.setEnabled_(
            controller.editable and controller.dirty and not (credentials_busy or links_busy)
        )
        self._runtime_restart.setEnabled_(
            controller.editable
            and not controller.dirty
            and not credentials_busy
            and not (credentials and credentials.input_dirty)
            and not (links and (links.busy or links.dirty))
            and not self.source_edits_pending()
            and (controller.can_restart or provider_pending)
        )

    def save_work_rules(self) -> None:
        """입력값이 유효할 때만 읽은 버전의 문서를 저장한다."""
        controller, form = self._work_rules, self._work_rules_form
        if controller is None or form is None or controller.state is None:
            return
        try:
            rules = form.draft()
        except ValueError as error:
            controller.error = str(error)
        else:
            controller.save(WorkRulesDocument(controller.state.settings.revision, rules))
        self.render()

    def save_sources(self) -> None:
        """현재 표의 검증된 매핑만 쓰기 요청으로 보낸다."""
        form = self._source_form
        if form is None or form.busy:
            return
        try:
            draft = form.draft()
        except (ValueError, SourceSettingsError) as error:
            form.error = str(error)
        else:
            form.error = None
            form.editor.save(draft)
        self.render()

    def _source_restart_blocked(self) -> bool:
        """다른 설정이나 근무 입력 초안을 보호한다."""
        rules = self._work_rules
        return self._other_edits_pending() or bool(
            rules and (rules.busy or rules.dirty or rules.restarting)
        )

    def work_rules_restart_blocked(self) -> bool:
        """규칙 적용 전에 수집 소스 편집도 확인한다."""
        return self._other_edits_pending() or self.source_edits_pending()

    def source_edits_pending(self) -> bool:
        """다른 설정 탭에서 다시 시작할 때 소스 초안을 보호한다."""
        sources = self._source_form
        return bool(
            sources
            and (
                sources.busy
                or sources.editor.busy
                or sources.editor.dirty
                or sources.editor.restarting
            )
        )

    def _other_edits_pending(self) -> bool:
        """다른 화면의 저장 작업이나 미저장 입력을 재시작으로 잃지 않게 한다."""
        if self._pending_work_edits and self._pending_work_edits():
            return True
        settings = (
            self.controller,
            self._identity_controller,
            self._runtime_controller,
            self._credentials_controller,
            self._page_links_controller,
            self._slack_controller,
            self._mcp_controller,
        )
        if any(controller is not None and controller.busy for controller in settings):
            return True
        runtime = self._runtime_controller
        credentials = self._credentials_controller
        links = self._page_links_controller
        slack = self._slack_controller
        return bool(
            self.controller.dirty
            or (runtime and (runtime.dirty or runtime.restarting))
            or (credentials and credentials.input_dirty)
            or (links and links.dirty)
            or (slack and (slack.member_dirty or slack.token_dirty or slack.app_dirty))
            or (self._mcp_view and self._mcp_view.is_editing)
        )

    def update_pending(self) -> bool:
        """복원이나 편집 도중에 업데이트 설치가 앱을 종료하지 않게 한다."""
        return self._backup_edits_pending() or bool(self._backup_view and self._backup_view.busy)

    def _backup_edits_pending(self) -> bool:
        """복원 재시작으로 다른 화면의 편집 중인 설정과 기록을 잃지 않게 한다."""
        return (
            self._source_restart_blocked()
            or self.source_edits_pending()
            or bool(self._identity_view and self._identity_view.dirty)
        )

    def _render_work_rules(self, controller: WorkRulesEditor) -> None:
        """공통 하단 버튼과 상태를 현재 근무 규칙 편집에 연결한다."""
        if self._work_rules_form is not None:
            self._work_rules_form.render()
        blocked = controller.can_restart and self.work_rules_restart_blocked()
        message = "다른 변경을 저장하거나 취소하세요." if blocked else controller.message
        self._status.setStringValue_(message)
        self._status.setToolTip_(message)
        self._runtime_save.setEnabled_(controller.editable and controller.dirty)
        self._runtime_restart.setEnabled_(controller.can_restart and not blocked)
        self._reload.setEnabled_(not controller.busy and not controller.restarting)

    def _render_sources(self, form: SourceSettingsForm) -> None:
        """공통 저장·다시 시작 버튼을 현재 소스 상태에 연결한다."""
        form.render()
        controller = form.editor
        blocked = controller.can_restart and self._source_restart_blocked()
        message = "다른 변경을 저장하거나 취소하세요." if blocked else form.message
        self._status.setStringValue_(message)
        self._status.setToolTip_(message)
        self._runtime_save.setEnabled_(controller.editable and controller.dirty and not form.busy)
        self._runtime_restart.setEnabled_(controller.can_restart and not blocked and not form.busy)
        self._reload.setEnabled_(not (form.busy or controller.busy or controller.restarting))

    def _render_categories(self, form: CategorySettingsForm) -> None:
        """대화상자에서 시작한 저장과 적용 상태를 보여 준다."""
        form.render()
        editor = form.editor
        blocked = editor.can_restart and self._source_restart_blocked()
        message = "다른 변경을 저장하거나 취소하세요." if blocked else form.error or editor.message
        self._status.setStringValue_(message)
        self._status.setToolTip_(message)
        self._runtime_restart.setEnabled_(editor.can_restart and not blocked)
        self._reload.setEnabled_(not (editor.busy or editor.restarting))

    def dispose(self) -> None:
        """앱 종료에서만 작업자 수명을 마친다."""
        if self._backup_view is not None:
            self._backup_view.close()
        if self._source_form is not None:
            self._source_form.close()
        self.close()

    def tick(self) -> None:
        """닫힌 창에서는 완료된 작업만 회수하고 화면은 다시 열 때 갱신한다."""
        visible = self._window.isVisible()
        if self._backup_view is not None:
            self._backup_view.tick(visible=visible and self._selected_page == SettingsPage.BACKUP)
        if visible:
            self.render()
            return
        if self._category_form is not None:
            self._category_form.refresh_alert_preferences()
        if self._selected_page == SettingsPage.SOURCES and self._source_form is not None:
            self._source_form.poll()
        if self._credentials_view is not None:
            self._credentials_view.clear_confirmed_input()

    def render(self) -> None:
        """확정값 대신 최신 입력 초안과 진행 상태를 표시한다."""
        alert_settings_pending = False
        if self._category_form is not None:
            self._category_form.refresh_alert_preferences()
            alert_settings_pending = self._category_form.awaiting_alert_preferences
        if self._identity_view is not None:
            self._identity_view.render()
        if self._selected_page == SettingsPage.BACKUP and self._backup_view is not None:
            self._status.setStringValue_(self._backup_view.message)
            self._status.setToolTip_(self._backup_view.message or None)
            return
        if self._selected_page == SettingsPage.SOURCES and self._source_form is not None:
            self._render_sources(self._source_form)
            return
        if self._selected_page == SettingsPage.CATEGORIES and self._category_form is not None:
            self._render_categories(self._category_form)
            return
        if self._selected_page == SettingsPage.WORK_RULES and self._work_rules is not None:
            self._render_work_rules(self._work_rules)
            return
        if self._selected_page == SettingsPage.SLACK and self._slack_controller is not None:
            if self._slack_view is not None:
                self._slack_view.render()
            self._status.setStringValue_(self._slack_controller.message)
            self._status.setToolTip_(self._slack_controller.message or None)
            self._reload.setEnabled_(not self._slack_controller.busy)
            return
        if self._selected_page == SettingsPage.MCP and self._mcp_controller is not None:
            if self._mcp_view is not None:
                self._mcp_view.render()
            self._status.setStringValue_(self._mcp_controller.message)
            self._status.setToolTip_(self._mcp_controller.message)
            self._reload.setEnabled_(not self._mcp_controller.busy)
            return
        if self._selected_page == SettingsPage.NOTION:
            self._render_runtime()
            return
        controller = self.controller
        value = controller.value
        identity_message = (
            (self._identity_controller.error or self._identity_controller.message)
            if self._selected_page == SettingsPage.NOTIFICATIONS and self._identity_controller
            else None
        )
        source_state = self._source_form.editor.state if self._source_form is not None else None
        display = source_state.active_display if source_state is not None else ()
        categories = source_state.categories if source_state is not None else ()
        state = (
            value,
            controller.busy,
            controller.dirty,
            controller.error,
            alert_settings_pending,
            identity_message,
            display,
            categories,
        )
        if state == self._render_state:
            return
        self._render_state = state
        editable = value is not None and controller.error is None and not controller.loading
        sections = source_sections(value.sections, display) if value is not None else ()
        rows_height = min(max(2, len(sections)), self._screen_max_rows) * 32
        self._sections.resize(self._screen_rows_top, rows_height)
        self._screen_card.setFrame_(
            frame(16, self._screen_rows_top - rows_height - 12, 552, rows_height + 56)
        )
        self._sections.update(
            sections,
            editable,
            {
                item.category.key: item.category.name
                for item in display
                if item.category is not None
            },
        )
        self._notification_view.update_categories(categories)
        self._enabled.setEnabled_(editable and not alert_settings_pending)
        for control in (
            *self._channels.values(),
            self._notification_view.departure,
            self._notification_view.apply_departure,
        ):
            control.setEnabled_(
                editable
                and not alert_settings_pending
                and value is not None
                and value.notifications_enabled
            )
        for (group, kinds), control in self._notification_view.source_types.items():
            active = kinds[0] not in self._notification_view.inactive_rules
            control.setEnabled_(
                editable
                and not alert_settings_pending
                and value is not None
                and value.notifications_enabled
                and active
            )
            control.setState_(
                int(
                    active
                    and value is not None
                    and any(value.notification_enabled(kind, group) for kind in kinds)
                )
            )
        if value is not None:
            self._enabled.setState_(int(value.notifications_enabled))
            for key, control in self._channels.items():
                control.setState_(int(key in value.channels))
            self._notification_view.show_offsets(value.departure_offsets)
        self._status.setToolTip_(None)
        self._reload.setEnabled_(not controller.busy)
        self._status.setStringValue_(
            controller.error
            or (
                "불러오는 중…"
                if controller.loading
                else "저장 중…"
                if controller.busy or alert_settings_pending
                else "저장 대기…"
                if controller.dirty
                else "저장됨"
            )
        )
        if (
            self._selected_page == SettingsPage.NOTIFICATIONS
            and self._identity_controller is not None
            and not controller.busy
            and not controller.dirty
        ):
            self._status.setStringValue_(
                self._identity_controller.error or self._identity_controller.message
            )

    def close(self) -> None:
        """앱이 종료될 때도 시스템 닫기 절차를 거친다."""
        self._window.close()

    def clear_private_fields(self) -> None:
        """창이 닫힐 때 도움말과 일회성 비밀 표시를 정리한다."""
        if self._page_links_view is not None:
            self._page_links_view.hide_help()
        if self._mcp_controller is not None:
            self._mcp_controller.forget_secret()
            if self._mcp_view is not None:
                self._mcp_view.render()
        if self._slack_controller is not None:
            self._slack_controller.forget_token()
            if self._slack_view is not None:
                self._slack_view.render()
