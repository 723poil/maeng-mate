"""근무 선택과 스크롤 일정을 한 화면에 모은 macOS 팝오버다."""

import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSControlSizeRegular,
    NSDatePicker,
    NSDatePickerElementFlagHourMinute,
    NSDatePickerStyleTextField,
    NSFont,
    NSFontWeightMedium,
    NSFontWeightSemibold,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSNoBorder,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSScreen,
    NSScrollView,
    NSStatusBar,
    NSTextAlignmentRight,
    NSTextField,
    NSVariableStatusItemLength,
    NSView,
    NSWorkspace,
)
from Foundation import (
    NSDate,
    NSLocale,
    NSObject,
    NSRunLoop,
    NSRunLoopCommonModes,
    NSTimer,
    NSTimeZone,
)

from src.application.ai_skills import SkillInstaller
from src.application.app_updates import AppUpdates
from src.application.calendar import CalendarClient
from src.application.data_backup import DataBackupActions
from src.application.desktop import DayUpdate, DesktopClient, DesktopSnapshot, MenuEntry
from src.application.mcp_access import McpSettingsClient
from src.application.notification_identity import NotificationBranding
from src.application.notifications import NotificationChannel
from src.application.notion_credentials import NotionCredentialsClient
from src.application.notion_links import notion_link
from src.application.notion_page_links import NotionPageLinksClient, NotionPageLinksState
from src.application.preferences import PreferencesClient
from src.application.runtime_settings_service import RestartOperation, RuntimeSettingsClient
from src.application.slack_settings import SlackSettingsClient
from src.application.source_settings import SourceSettingsClient
from src.application.work_records import WorkRecordsClient
from src.application.work_rules_settings import WorkRulesClient
from src.application.workday import LEAVE_LABELS, SELECTABLE_LEAVES
from src.domain.calendar import CALENDAR_KINDS, CalendarPreferences
from src.domain.daily_work import (
    OVERNIGHT_DEPARTURE_REASON,
    DepartureResult,
    LeaveType,
    WorkRules,
    parse_clock,
)
from src.domain.preferences import SECTIONS, Preferences, SectionPreference
from src.domain.source_display import SourceDisplay, display_groups
from src.domain.work_dates import SEOUL
from src.presentation.calendar_controller import CalendarController
from src.presentation.calendar_controls import CalendarFilterChoice, calendar_filter_choices
from src.presentation.calendar_views import (
    COMPACT_CALENDAR_HEIGHT,
    CalendarPage,
    CompactCalendarView,
)
from src.presentation.desktop_controller import DesktopController
from src.presentation.entry_section import EntrySection
from src.presentation.glass_panel import GlassPanel
from src.presentation.main_window import HomeCard, MainWindow
from src.presentation.mcp_settings_controller import McpSettingsController
from src.presentation.native_views import (
    ENTRY_TITLE_X,
    button,
    card,
    frame,
    glass_container,
    label,
    place_label,
    symbol,
)
from src.presentation.notification_identity_controller import NotificationIdentityController
from src.presentation.notion_credentials_controller import NotionCredentialsController
from src.presentation.notion_page_links_controller import NotionPageLinksController
from src.presentation.preferences_controller import PreferencesController
from src.presentation.preferences_sidebar import SettingsPage
from src.presentation.preferences_window import PreferencesWindow
from src.presentation.runtime_settings_controller import RuntimeSettingsController, SettingsEditor
from src.presentation.slack_settings_controller import SlackSettingsController
from src.presentation.source_sections import group_board_url, source_sections
from src.presentation.source_settings_form import SourceEditor
from src.presentation.update_button import UpdateChecks
from src.presentation.work_records_controller import WorkRecordsController
from src.presentation.work_records_window import WorkRecordsWindow
from src.presentation.work_rules_form import WorkRulesEditor

type Preview = Callable[[DayUpdate, WorkRules | None], DepartureResult]
POPOVER_WIDTH = 480
INTERVAL_MINUTES = (1, 5, 10, 15, 30, 60)


def local_day() -> str:
    """화면의 날짜 전환은 서울 날짜로 확인한다."""
    return datetime.now(SEOUL).date().isoformat()


def clock_label(value: datetime | None) -> str:
    """표시 시점은 서울 시간으로 통일한다."""
    return value.astimezone(SEOUL).strftime("%H:%M") if value else "—"


class Callbacks(NSObject):
    """Cocoa 선택자와 Python 화면 로직을 연결한다."""

    owner: "MenuBarApp"

    def toggle_(self, sender: object) -> None:
        """팝오버를 열거나 닫는다."""
        self.owner.toggle()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app: object, visible: bool) -> bool:
        """Finder가 실행 중인 메뉴바로 직접 보낸 재열기를 처리한다."""
        self.owner.open_main()
        return False

    def home_(self, sender: object) -> None:
        """빠른 패널에서 메인 창의 홈으로 이동한다."""
        self.owner.open_main()

    def applicationShouldTerminate_(self, app: NSApplication) -> int:
        """Dock과 단축키의 종료 요청에도 미완료 입력을 보호한다."""
        return 0 if self.owner.termination_pending() else 1

    def calendar_(self, sender: object) -> None:
        """메인 표시와 별개로 캘린더 창을 연다."""
        self.owner.open_calendar()

    def records_(self, sender: object) -> None:
        """근무 카드에서 별도 출퇴근 기록 창을 연다."""
        self.owner.open_records()

    def tick_(self, sender: object) -> None:
        """계산·저장 결과를 메인 루프에서 처리한다."""
        self.owner.tick()

    def calendarLoaded_(self, sender: object) -> None:
        """월 조회가 끝나면 다음 타이머를 기다리지 않고 보이는 달력을 그린다."""
        self.owner.calendar_loaded()

    def animateResize_(self, sender: object) -> None:
        """확대·축소 전환의 다음 프레임을 그린다."""
        self.owner.animate_resize()

    def refresh_(self, sender: object) -> None:
        """명시적으로 마지막 저장값과 목록을 다시 읽는다."""
        self.owner.refresh(manual=True)

    def interval_(self, sender: object) -> None:
        """선택한 수집 주기를 즉시 저장한다."""
        self.owner.change_interval()

    def changed_(self, sender: object) -> None:
        """선택 변경은 즉시 계산하고 잠시 뒤 자동 저장한다."""
        self.owner.mark_dirty()
        self.owner._place_departure_title()

    def open_(self, sender: NSButton) -> None:
        """검증한 원천 링크를 연다."""
        url = sender.cell().representedObject()
        if isinstance(url, str):
            self.owner._open_notion(url)

    def openSourceBoard_(self, sender: NSButton) -> None:
        """사용자가 지정한 소스의 검증된 원본 주소를 연다."""
        self.owner.open_source_board(sender.tag())

    def expandSource_(self, sender: NSButton) -> None:
        """선택한 사용자 영역만 높이를 전환한다."""
        self.owner.expand_source(sender.tag())

    def screenSettings_(self, sender: object) -> None:
        """홈의 화면 설정 버튼은 해당 설정 페이지를 직접 연다."""
        self.owner.open_settings(screen=True)

    def settings_(self, sender: object) -> None:
        """독립된 설정 창을 연다."""
        self.owner.open_settings()

    def connect_(self, sender: object) -> None:
        """빈 상태의 연결 버튼은 다음에 할 일이 있는 설정 페이지를 바로 연다."""
        self.owner.open_connection_settings()

    def quit_(self, sender: object) -> None:
        """메뉴바 프로세스만 종료한다."""
        self.owner.quit()


def open_browser_url(url: str) -> None:
    """검증한 Notion 페이지를 기본 브라우저의 새 탭에서 연다."""
    webbrowser.open(url, new=2)


class MenuBarApp:
    """화면 이동·별도 저장 버튼 없이 근무를 조정한다."""

    def __init__(
        self,
        client: DesktopClient,
        preview: Preview,
        *,
        preferences_client: PreferencesClient,
        runtime_settings_client: RuntimeSettingsClient | None = None,
        notion_credentials_client: NotionCredentialsClient | None = None,
        mcp_settings_client: McpSettingsClient | None = None,
        page_links_client: NotionPageLinksClient | None = None,
        skills: SkillInstaller | None = None,
        notification_branding: NotificationBranding | None = None,
        work_records_client: WorkRecordsClient | None = None,
        calendar_client: CalendarClient | None = None,
        slack_settings_client: SlackSettingsClient | None = None,
        work_rules_client: WorkRulesClient | None = None,
        source_client: SourceSettingsClient | None = None,
        open_url: Callable[[str], None] = open_browser_url,
        restart_services: Callable[[], RestartOperation] | None = None,
        data_backup: DataBackupActions | None = None,
        app_updates: AppUpdates | None = None,
        notification_channels: tuple[NotificationChannel, ...] = (
            NotificationChannel("macos", "macOS 알림"),
        ),
        notification_status: Callable[[], str | None] | None = None,
        panel_requested: Callable[[], bool] | None = None,
        shutdown_requested: Callable[[], bool] | None = None,
        today: Callable[[], str] = local_day,
    ) -> None:
        """로컬 연결과 순수 예상시간 계산 유스케이스를 주입받는다."""
        self._app_updates = UpdateChecks(app_updates, self._update_pending) if app_updates else None
        self._data_backup = data_backup
        self._notification_channels = notification_channels
        self._open_url = open_url
        # 저장 설정을 읽기 전에는 숨긴 업무가 잠깐 노출되지 않게 한다.
        self._preferences = Preferences(
            sections=tuple(SectionPreference(key, False) for key in SECTIONS)
        )
        self._preferences_controller = PreferencesController(preferences_client)
        self._identity_controller = (
            NotificationIdentityController(notification_branding) if notification_branding else None
        )
        self._runtime_settings_controller = (
            RuntimeSettingsController(runtime_settings_client, restart_services)
            if runtime_settings_client is not None
            else None
        )
        self._notion_credentials_controller = (
            NotionCredentialsController(notion_credentials_client)
            if notion_credentials_client
            else None
        )
        self._mcp_settings_controller = (
            McpSettingsController(mcp_settings_client, skills) if mcp_settings_client else None
        )
        self._page_links_controller = (
            NotionPageLinksController(page_links_client, self.update_page_links)
            if page_links_client
            else None
        )
        self._slack_settings_controller = (
            SlackSettingsController(slack_settings_client, self._slack_member_saved)
            if slack_settings_client
            else None
        )
        self._work_rules_controller: WorkRulesEditor | None = (
            SettingsEditor(work_rules_client, restart_services) if work_rules_client else None
        )
        self._source_display: tuple[SourceDisplay, ...] = ()
        self._calendar_choices = calendar_filter_choices(self._source_display)
        self._source_groups: dict[str, tuple[SourceDisplay, ...]] = {}
        self._section_order: tuple[SectionPreference, ...] = ()
        self._custom_sections: dict[str, EntrySection] = {}
        self._custom_expanded: set[str] = set()
        self._source_client = source_client
        self._source_editor: SourceEditor | None = (
            SettingsEditor(source_client, restart_services) if source_client else None
        )
        # 같은 설정 작업들을 갱신하고, 종료 전에는 진행 여부를 확인한 뒤 정리한다.
        self._settings_controllers = tuple(
            controller
            for controller in (
                self._slack_settings_controller,
                self._page_links_controller,
                self._mcp_settings_controller,
                self._notion_credentials_controller,
                self._runtime_settings_controller,
                self._work_rules_controller,
                self._source_editor,
            )
            if controller is not None
        )
        self._settings_window: PreferencesWindow | None = None
        self._records_controller = (
            WorkRecordsController(work_records_client, self._records_saved)
            if work_records_client
            else None
        )
        self._records_window: WorkRecordsWindow | None = None
        self._calendar_controller = (
            CalendarController(calendar_client, notify_loaded=self._notify_calendar_loaded)
            if calendar_client
            else None
        )
        self._main_window: MainWindow | None = None
        self._calendar_view: CompactCalendarView | None = None
        self._today = today
        self._display_day = today()
        self._preview = preview
        self._notification_status = notification_status
        self._panel_requested = panel_requested
        self._shutdown_requested = shutdown_requested
        self._notification_error: str | None = None
        self._application = NSApplication.sharedApplication()
        self._application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        self._callbacks = Callbacks.alloc().init()
        self._callbacks.owner = self
        self._application.setDelegate_(self._callbacks)
        # Dock·응용 프로그램 메뉴에서 같은 정식 종료 경로를 사용한다.
        menu = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        menu.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        app_menu.addItem_(
            NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Catch 종료", "terminate:", "q")
        )
        app_item.setSubmenu_(app_menu)
        self._application.setMainMenu_(menu)
        self._controller = DesktopController(client)
        self._dirty = False
        self._save_blocked = False
        self._last_edit = 0.0
        self._base: DayUpdate | None = None
        self._next_refresh = time.monotonic() + 15
        self._maximum_height = 660.0
        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        status_button = self._status_item.button()
        # 템플릿의 투명 영역을 살려 메뉴바 배경과 선택 상태에 맞게 시스템이 채색한다.
        # 개발용도 같은 도안을 쓰고 도움말·VoiceOver 이름과 설정의 버전 표시로 구분한다.
        mark = NSImage.alloc().initWithContentsOfFile_(
            str(Path(__file__).with_name("assets") / "MenuMark.svg")
        )
        if mark is None:
            raise RuntimeError("메뉴바 아이콘을 불러오지 못했습니다.")
        mark.setTemplate_(True)
        status_button.setImage_(mark)
        self._set_status_description("연결 중")
        status_button.setTarget_(self._callbacks)
        status_button.setAction_("toggle:")
        self._view = NSView.alloc().initWithFrame_(frame(0, 0, POPOVER_WIDTH, 660))
        self._glass = glass_container(self._view, POPOVER_WIDTH, 660)
        self._panel = GlassPanel(
            self._glass, POPOVER_WIDTH, 660, on_hide=self._close_calendar_filters
        )
        self._maximum_height = self._available_height()
        self._base_height = min(760.0, self._maximum_height)
        self._rendered_base_height = 0.0
        self._build()
        self._layout_state: dict[str, float] = {}
        self._layout_target = self._layout_state
        self._layout_from = self._layout_state
        self._animation_started = 0.0
        self._animation_timer: NSTimer | None = None
        self._timer: NSTimer | None = None
        self.refresh()

    def _build(self) -> None:
        """고정 기능을 배치하고 카테고리 카드는 확인한 소스로 따로 만든다."""
        view = self._view
        self._date = label(view, "오늘", 22, 615, POPOVER_WIDTH - 202, size=14)
        # 날짜는 패널의 기준 정보일 뿐이라 아래 카드 제목과 비슷한 무게로 둔다.
        self._date.setFont_(NSFont.systemFontOfSize_weight_(14, NSFontWeightMedium))
        home_button = button(
            view, "", POPOVER_WIDTH - 170, 614, 26, self._callbacks, "home:", bordered=False
        )
        home_button.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_("house", "홈 열기")
        )
        home_button.setAccessibilityLabel_("홈 열기")
        home_button.setToolTip_("홈 열기")
        calendar_button = button(
            view, "", POPOVER_WIDTH - 138, 614, 26, self._callbacks, "calendar:", bordered=False
        )
        calendar_button.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_("calendar", "캘린더 열기")
        )
        calendar_button.setToolTip_("캘린더 열기")
        calendar_button.setEnabled_(self._calendar_controller is not None)
        self._refresh_button = button(
            view, "", POPOVER_WIDTH - 74, 614, 26, self._callbacks, "refresh:", bordered=False
        )
        self._refresh_button.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "arrow.clockwise", "새로고침"
            )
        )
        self._refresh_button.setToolTip_("화면 새로고침")
        settings_button = button(
            view, "", POPOVER_WIDTH - 106, 614, 26, self._callbacks, "settings:", bordered=False
        )
        settings_button.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_("gearshape", "설정")
        )
        settings_button.setToolTip_("화면 구성 및 알림 설정")
        self._spinner = NSProgressIndicator.alloc().initWithFrame_(frame(23, 586, 16, 16))
        self._spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self._spinner.setIndeterminate_(True)
        self._spinner.setDisplayedWhenStopped_(False)
        view.addSubview_(self._spinner)
        close = button(
            view, "", POPOVER_WIDTH - 42, 614, 26, self._callbacks, "quit:", bordered=False
        )
        close.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_("power", "메뉴바 종료")
        )
        self._collection_icon = symbol(view, "arrow.triangle.2.circlepath", 22, 585)
        self._collection = label(view, "연결 중", 48, 584, POPOVER_WIDTH - 225, size=12, bold=True)
        self._collection_detail = label(
            view, "", 48, 565, POPOVER_WIDTH - 225, size=11, secondary=True
        )
        self._interval_label = label(
            view, "수집 주기", POPOVER_WIDTH - 170, 584, 60, size=11, secondary=True
        )
        # 연결한 표가 없으면 상태 줄 전체를 이 버튼 하나로 바꾼다. 버튼 테두리 안쪽 여백을 빼고
        # 위 날짜 글자와 왼쪽 끝을 맞추고, 상태 두 줄이 차지하던 높이의 가운데에 둔다.
        self._connect_button = button(
            view, "Notion 연결…", 16, 571, 118, self._callbacks, "connect:"
        )
        self._connect_button.setHidden_(True)
        self._empty = False
        self._interval = self._selector(
            POPOVER_WIDTH - 112, 577, 90, [f"{minutes}분" for minutes in INTERVAL_MINUTES]
        )
        self._interval.setFont_(NSFont.systemFontOfSize_(12))
        self._interval.setAction_("interval:")
        self._interval.selectItemAtIndex_(INTERVAL_MINUTES.index(15))
        self._interval.setEnabled_(False)
        self._interval.setToolTip_("Notion 수집 간격 · 변경 즉시 저장")
        self._header_frames = [(control, control.frame()) for control in view.subviews()]
        self._body_scroll = NSScrollView.alloc().initWithFrame_(frame(0, 0, POPOVER_WIDTH, 555))
        self._body_scroll.setHasVerticalScroller_(True)
        self._body_scroll.setAutohidesScrollers_(True)
        self._body_scroll.setDrawsBackground_(False)
        self._body_scroll.setBorderType_(NSNoBorder)
        self._body = NSView.alloc().initWithFrame_(frame(0, 0, POPOVER_WIDTH, 555))
        self._body_scroll.setDocumentView_(self._body)
        view.addSubview_(self._body_scroll)
        view = self._body
        card(view, 16, 443, POPOVER_WIDTH - 32, 112)
        symbol(view, "clock", 30, 525)
        label(view, "근무", ENTRY_TITLE_X, 525, 140, size=13, bold=True)
        self._records_button = button(
            view, "기록 ↗", POPOVER_WIDTH - 92, 520, 66, self._callbacks, "records:", bordered=False
        )
        self._records_button.setEnabled_(self._records_controller is not None)
        # 저장 실패·휴무처럼 사용자가 알아야 하는 상태만 제목 옆에 표시한다.
        self._work_message = label(view, "", 96, 525, POPOVER_WIDTH - 196, size=11, secondary=True)
        # 카드의 주인공은 퇴근 시각이다. 제목은 실제 글자 폭 바로 뒤에 붙여 한 덩어리로 읽힌다.
        self._departure = label(view, "--:--", 30, 482, 220, size=30, bold=True)
        self._departure.setTextColor_(NSColor.tertiaryLabelColor())
        self._departure_title = label(view, "퇴근 예정", 120, 487, 80, size=12, secondary=True)
        self._departure_warning = label(
            view, "", POPOVER_WIDTH - 222, 488, 180, size=10, secondary=True
        )
        self._departure_warning.setAlignment_(NSTextAlignmentRight)
        # 입력은 테두리 없는 한 줄 문장으로 두고 누른 부분만 편집 상태가 된다.
        label(view, "출근", 30, 455, 30, size=13, secondary=True)
        self.arrival = self._time_picker(60, 453, 50, view)
        self._set_arrival(9, 0)
        label(view, "·", 112, 455, 10, size=13, secondary=True)
        self.leave = self._selector(
            120,
            451,
            140,
            [
                # 휴가가 없는 날을 '출근'으로 쓰면 앞의 출근 시각과 겹쳐 읽혀 이 화면에서만 바꿔 부른다.
                "일반 근무" if leave == LeaveType.NONE else LEAVE_LABELS[leave]
                for leave in SELECTABLE_LEAVES
            ],
            parent=view,
        )
        self.leave.setBordered_(False)
        # 연차인 날은 근무 카드로 출근을 기록하지 않으므로 고를 수 없게 숨긴다. 기록 창에서 연차로
        # 정한 날도 같은 이름으로 보여야 해서 항목을 지우지 않고 숨겨 목록 위치를 유지한다.
        self.leave.itemAtIndex_(SELECTABLE_LEAVES.index(LeaveType.FULL)).setHidden_(True)
        self._work_frames = [(control, control.frame()) for control in view.subviews()]
        if self._calendar_controller is not None:
            self._calendar_view = CompactCalendarView(
                view,
                POPOVER_WIDTH,
                self._calendar_controller,
                self._calendar_preferences,
                self._toggle_calendar_filter,
                self.open_calendar,
                choices=self._calendar_filter_choices,
            )
        self._empty_view = label(
            view, "설정 → 화면에서 표시할 카드를 선택하세요.", 26, 20, 460, secondary=True
        )
        self.arrival.setEnabled_(False)
        self.leave.setEnabled_(False)

    def _resize_lists(self, *, animated: bool = False) -> None:
        """표시하는 카테고리만 계산하고 확대·축소를 같은 높이 기준으로 전환한다."""
        self._maximum_height = min(760.0, self._available_height())
        self._section_order = source_sections(self._preferences.sections, self._source_display)
        visible = {item.key for item in self._section_order if item.visible}
        for key, section in self._custom_sections.items():
            section.view.setHidden_(key not in visible)
        self._empty_view.setHidden_(bool(visible))
        category_keys = visible.intersection(self._custom_sections)
        self._section_height = (self._maximum_height - 269) / 3
        self._base_height = max(
            160.0,
            121
            + (112 if "work" in visible else 0)
            + self._section_height * len(category_keys)
            + (COMPACT_CALENDAR_HEIGHT if "calendar" in visible and self._calendar_view else 0)
            + max(0, len(visible) - 1) * 12,
        )
        target = {key: 144.0 if key in self._custom_expanded else 0.0 for key in category_keys}
        self._stop_resize_animation()
        self._layout_target = target
        if self._layout_state == target and self._rendered_base_height == self._base_height:
            return
        if (
            not animated
            or not self._panel.is_visible()
            or NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
        ):
            self._apply_layout(target)
            return
        self._layout_from = self._layout_state
        self._animation_started = time.monotonic()
        self._animation_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 60, self._callbacks, "animateResize:", None, True
            )
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._animation_timer, NSRunLoopCommonModes)

    def animate_resize(self) -> None:
        """현재 높이에서 목표 높이까지 감속해 열림 상태를 안정적으로 유지한다."""
        progress = min(1.0, (time.monotonic() - self._animation_started) / 0.22)
        if not self._panel.is_visible():
            progress = 1.0
        weight = 1 - (1 - progress) ** 3
        step = {}
        for key, target in self._layout_target.items():
            start = self._layout_from.get(key, 0.0)
            step[key] = start + (target - start) * weight
        self._apply_layout(step)
        if progress >= 1:
            self._stop_resize_animation()

    def _stop_resize_animation(self) -> None:
        """이전 전환 타이머가 새 목표나 종료된 화면을 덮어쓰지 않게 한다."""
        if self._animation_timer is not None:
            self._animation_timer.invalidate()
            self._animation_timer = None

    def _apply_layout(self, extras: dict[str, float]) -> None:
        """카테고리 카드를 사용자 순서대로 놓고 본문의 읽던 위치를 복원한다."""
        self._layout_state = extras
        self._rendered_base_height = self._base_height
        natural_height = self._base_height + sum(extras.values())
        height = min(natural_height, self._maximum_height)
        previous_doc = self._body.frame()[1][1]
        previous_viewport = self._body_scroll.frame()[1][1]
        previous_origin = self._body_scroll.contentView().bounds()[0][1]
        distance_from_top = max(0, previous_doc - previous_viewport - previous_origin)
        document_height = natural_height - 105
        viewport_height = height - 105
        self._body.setFrameSize_((POPOVER_WIDTH, document_height))
        self._body_scroll.setFrame_(frame(0, 0, POPOVER_WIDTH, viewport_height))
        self._panel.resize(POPOVER_WIDTH, height)
        self._view.setFrameSize_((POPOVER_WIDTH, height))
        for control, original in self._header_frames:
            (x, y), (width, control_height) = original
            if isinstance(control, NSTextField):
                place_label(control, x, y + height - 660, width, control_height)
            else:
                control.setFrame_(frame(x, y + height - 660, width, control_height))
        top = document_height
        for item in self._section_order:
            if item.key == "work":
                for control, original in self._work_frames:
                    control.setHidden_(not item.visible)
                    (x, y), (width, control_height) = original
                    bottom = top - 112
                    if isinstance(control, NSTextField):
                        place_label(control, x, y + bottom - 443, width, control_height)
                    else:
                        control.setFrame_(frame(x, y + bottom - 443, width, control_height))
                self._place_departure_title()
                if item.visible:
                    top -= 124
            elif item.key == "calendar":
                if self._calendar_view is not None:
                    self._calendar_view.set_hidden(not item.visible)
                    if item.visible:
                        self._calendar_view.resize(top - COMPACT_CALENDAR_HEIGHT)
                        top -= COMPACT_CALENDAR_HEIGHT + 12
            else:
                section = self._custom_sections[item.key]
                if item.visible:
                    section_height = self._section_height + extras.get(item.key, 0.0)
                    section.resize(top - section_height, section_height, POPOVER_WIDTH)
                    top -= section_height + 12

        self._body.scrollPoint_((0, max(0, document_height - viewport_height - distance_from_top)))

    def _available_height(self) -> float:
        """열거나 크기를 바꿀 때 현재 표시 중인 모니터를 다시 확인한다."""
        window = (
            self._view.window() if self._panel.is_visible() else self._status_item.button().window()
        )
        screen = window.screen() if window is not None else None
        screen = screen or NSScreen.mainScreen()
        return max(660.0, screen.visibleFrame()[1][1] - 24) if screen else 660.0

    def _selector(
        self, x: float, y: float, width: float, titles: list[str], *, parent: NSView | None = None
    ) -> NSPopUpButton:
        """근무 형태와 수집 주기는 기본 macOS 선택 컨트롤을 사용한다."""
        view = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame(x, y, width, 30), False)
        view.addItemsWithTitles_(titles)
        view.setFont_(NSFont.systemFontOfSize_(13))
        view.setControlSize_(NSControlSizeRegular)
        view.setBezelStyle_(NSBezelStyleRounded)
        view.setTarget_(self._callbacks)
        view.setAction_("changed:")
        (parent if parent is not None else self._view).addSubview_(view)
        return view

    def _time_picker(self, x: float, y: float, width: float, parent: NSView) -> NSDatePicker:
        """출근 시각은 글자처럼 보이다가 누르면 시·분을 키보드와 방향키로 고치는 기본 입력이다."""
        view = NSDatePicker.alloc().initWithFrame_(frame(x, y, width, 22))
        view.setDatePickerStyle_(NSDatePickerStyleTextField)
        view.setDatePickerElements_(NSDatePickerElementFlagHourMinute)
        view.setBezeled_(False)
        view.setBordered_(False)
        view.setDrawsBackground_(False)
        # 퇴근 예정과 같은 24시간 표기를 쓰고 기기 시간대와 무관하게 서울 시각으로 읽는다.
        view.setLocale_(NSLocale.localeWithLocaleIdentifier_("en_GB"))
        view.setTimeZone_(NSTimeZone.timeZoneWithName_(SEOUL.key))
        # 숫자 폭이 같은 글꼴로 시각을 바꿔도 뒤의 근무 형태가 흔들리지 않는다.
        view.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(13, NSFontWeightSemibold))
        view.setControlSize_(NSControlSizeRegular)
        view.setTarget_(self._callbacks)
        view.setAction_("changed:")
        parent.addSubview_(view)
        return view

    def _set_arrival(self, hour: int, minute: int) -> None:
        """시·분만 쓰는 입력이므로 날짜는 고정 기준일로 채운다."""
        moment = datetime(2001, 1, 1, hour, minute, tzinfo=SEOUL)
        self.arrival.setDateValue_(NSDate.dateWithTimeIntervalSince1970_(moment.timestamp()))

    def _arrival(self) -> str:
        """입력의 서울 시각을 저장 형식의 시:분으로 읽는다."""
        seconds = self.arrival.dateValue().timeIntervalSince1970()
        return datetime.fromtimestamp(seconds, SEOUL).strftime("%H:%M")

    def run(self) -> None:
        """가벼운 메인 타이머로 자동 저장 결과만 반영한다."""
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, self._callbacks, "tick:", None, True
        )
        try:
            if self._app_updates:
                self._app_updates.start()
            self._application.run()
        finally:
            self.close()

    def toggle(self) -> None:
        """한 화면 팝오버를 같은 위치에서 열고 닫는다."""
        if self._panel.is_visible():
            self._panel.hide()
        else:
            self.show_panel()

    def show_panel(self) -> None:
        """반복 열기 요청에서도 이미 열린 패널을 유지한다."""
        if not self._panel.is_visible():
            self._resize_lists()
            self._panel.show(self._status_item.button())
            self._next_refresh = 0
            self._render_calendars()

    def _selection(self) -> DayUpdate | None:
        """처음 읽은 날짜·버전과 현재 선택값을 결합한다."""
        if self._base is None:
            return None
        selected = self.leave.indexOfSelectedItem()
        if not 0 <= selected < len(SELECTABLE_LEAVES):
            return None
        return replace(self._base, attendance=self._arrival(), leave=SELECTABLE_LEAVES[selected])

    def _forecast(self) -> None:
        """선택 즉시 예상시간을 표시하고 저장 결과를 기다리지 않는다."""
        update = self._selection()
        snapshot = self._controller.snapshot
        if update is None or snapshot is None:
            return
        actual = snapshot.day.actual_departure
        displayed: datetime | None
        if actual is not None:
            displayed = actual
        elif not self._dirty and snapshot.day.update.attendance is not None:
            # 서버가 오늘 규칙으로 갱신한 예정 시각을 표시한다.
            displayed = snapshot.day.departure
        else:
            displayed = self._preview(update, snapshot.day.rules).departure
        self._departure_title.setStringValue_("퇴근" if actual is not None else "퇴근 예정")
        departure = clock_label(displayed) if displayed else "--:--"
        if displayed and displayed.astimezone(SEOUL).date().isoformat() != update.day:
            departure = displayed.astimezone(SEOUL).strftime("%m/%d %H:%M")
        self._departure.setFont_(NSFont.boldSystemFontOfSize_(18 if "/" in departure else 30))
        (x, y), (width, height) = self._departure.frame()
        place_label(self._departure, x, y + height / 2 - 18.5, width, 37)
        self._departure.setStringValue_(departure)
        self._place_departure_title()
        overnight = (
            actual is None
            and displayed is not None
            and displayed.astimezone(SEOUL).date().isoformat() != update.day
        )
        # 아직 기록하지 않은 날의 예상은 흐리게 두어 별도 문구 없이 확정값과 구분한다.
        recorded = actual is not None or self._dirty or snapshot.day.update.attendance is not None
        self._departure.setTextColor_(
            NSColor.labelColor() if recorded else NSColor.tertiaryLabelColor()
        )
        self._departure_warning.setStringValue_("익일 확정 불가 · 알림 없음" if overnight else "")
        self._departure_warning.setToolTip_(OVERNIGHT_DEPARTURE_REASON if overnight else None)
        self._departure.setToolTip_(OVERNIGHT_DEPARTURE_REASON if overnight else None)

    def _place_departure_title(self) -> None:
        """글자 폭에 맞춰 붙는 요소를 다시 맞춘다. 카드 배치가 바뀐 뒤에도 호출한다."""
        # 퇴근 제목은 시각의 실제 글자 폭 바로 뒤에 붙여 한 덩어리로 읽히게 한다.
        (x, _), _ = self._departure.frame()
        (_, y), (width, height) = self._departure_title.frame()
        place_label(
            self._departure_title, x + self._departure.fittingSize()[0] + 6, y, width, height
        )
        # 테두리 없는 메뉴는 선택한 이름 폭에 맞춰야 화살표가 글자 옆에 붙는다.
        (leave_x, leave_y), (_, leave_height) = self.leave.frame()
        self.leave.setFrame_(frame(leave_x, leave_y, self.leave.fittingSize()[0] + 4, leave_height))

    def _set_message(self, value: str) -> None:
        """조치가 필요한 안내만 제목 옆에 표시하고 빈 값은 이전 안내를 지운다."""
        self._work_message.setStringValue_(value)
        self._work_message.setToolTip_(value or None)

    def mark_dirty(self) -> None:
        """빠른 연속 선택은 마지막 값으로 모으고 계산은 즉시 갱신한다."""
        if self._base is None:
            return
        self._dirty = True
        self._last_edit = time.monotonic()
        self._forecast()
        self._set_message("")
        self._render_activity()

    def _reset_workday(self, day: str) -> None:
        """날짜가 바뀌면 전날 입력 초안과 예상만 비우고 저장 기록은 보존한다."""
        self._display_day = day
        self._dirty = self._save_blocked = False
        self._base = None
        self._controller.draft = None
        self._controller.connected = False
        self._controller.error = None
        self._controller.conflict = False
        if not self._controller.busy:
            self._controller.snapshot = None
        self._set_arrival(9, 0)
        self.leave.selectItemAtIndex_(0)
        self.arrival.setEnabled_(False)
        self.leave.setEnabled_(False)
        self._departure.setStringValue_("--:--")
        self._departure.setTextColor_(NSColor.tertiaryLabelColor())
        self._departure_warning.setStringValue_("")
        self._departure_warning.setToolTip_(None)
        self._departure.setToolTip_(None)
        self._departure_title.setStringValue_("퇴근 예정")
        current = datetime.fromisoformat(day)
        self._date.setStringValue_(
            f"{current.month}월 {current.day}일 {('월', '화', '수', '목', '금', '토', '일')[current.weekday()]}요일"
        )
        self._set_message("")
        for view in self._custom_sections.values():
            view.entries.update(())
        self._next_refresh = 0

    def tick(self) -> None:
        """저장 중 바뀐 선택은 이전 응답으로 되돌리지 않고 다음 버전으로 저장한다."""
        if self._panel_requested and self._panel_requested():
            self.open_main()
        if self._calendar_controller is not None:
            visible = self._compact_calendar_visible() or (self._main_calendar_visible())
            self._calendar_controller.tick(visible=visible)
            self._render_calendars()
        if self._records_controller is not None:
            self._records_controller.tick()
            if self._records_window is not None and self._records_window.is_visible():
                month = self._records_controller.month
                if (
                    month is not None
                    and month.today.isoformat() != self._today()
                    and not self._records_controller.busy
                    and not self._records_controller.dirty
                ):
                    self._records_controller.load(month.month)
                detail = self._records_controller.detail
                snapshot = self._controller.snapshot
                # 열린 기록 창도 오늘 버전이 바뀌면 다시 읽되, 편집 중인 입력은 보존한다.
                if (
                    detail is not None
                    and snapshot is not None
                    and detail.item.day.isoformat() == snapshot.day.update.day
                    and detail.item.revision < snapshot.day.update.revision
                    and not self._records_controller.blocked
                ):
                    self._records_controller.select(detail.item.day)
                self._records_window.render()
        rules_editor = self._work_rules_controller
        previous_rules = rules_editor.state if rules_editor else None
        for controller in self._settings_controllers:
            controller.tick()
        if rules_editor and previous_rules is not None and previous_rules != rules_editor.state:
            # 규칙 저장 직후 메뉴바는 다음 주기를 기다리지 않고 서버의 확정 기록을 읽는다.
            self._next_refresh = 0
        if self._source_editor is not None:
            state = self._source_editor.state
            if state is not None and self._source_display != state.active_display:
                self._apply_source_display(state.active_display)
        if self._preferences_controller.tick():
            confirmed = self._preferences_controller.confirmed
            if confirmed is not None:
                self._preferences = confirmed
                self._rendered_base_height = -1
                self._resize_lists()
                self._render()
        if self._identity_controller is not None:
            saved = self._preferences_controller.confirmed
            self._identity_controller.tick(
                saved.notification_identity if saved else None, saved.revision if saved else 0
            )
        if self._app_updates:
            self._app_updates.tick()
        if self._settings_window is not None:
            self._settings_window.tick()
        if self._preferences_controller.confirmed is None:
            self._empty_view.setStringValue_(
                "잠시 후 설정을 다시 불러옵니다."
                if self._preferences_controller.retrying
                else "설정 확인 필요"
                if self._preferences_controller.error
                else "설정 불러오는 중…"
            )
        else:
            self._empty_view.setStringValue_("설정 → 화면에서 표시할 카드를 선택하세요.")
        today = self._today()
        if today != self._display_day:
            self._reset_workday(today)
        operation = self._controller.operation
        if self._controller.poll():
            snapshot = self._controller.snapshot
            if snapshot is not None and snapshot.day.update.day != today:
                # 자정 전에 시작한 요청의 늦은 응답으로 전날 근무를 다시 표시하지 않는다.
                self._reset_workday(today)
                self.refresh()
                return
            if operation == "save":
                snapshot = self._controller.snapshot
                if self._controller.error is None and snapshot is not None:
                    self._base = snapshot.day.update
                    selected = self._selection()
                    self._dirty = selected is not None and (
                        selected.attendance != self._base.attendance
                        or selected.leave != self._base.leave
                    )
                    self._set_message("")
                else:
                    self._save_blocked = True
            elif self._controller.error is None and not self._dirty:
                snapshot = self._controller.snapshot
                if snapshot is not None:
                    self._set_message("")
            self._render()
            # 열린 패널에서 수집할 때만 다음 조회를 앞당겨 소스 전환과 종료를 따라간다.
            if (
                operation in {"fetch", "interval"}
                and self._panel.is_visible()
                and self._controller.connected
                and snapshot is not None
                and snapshot.collection.running
            ):
                self._next_refresh = time.monotonic() + 2
        if (
            self._dirty
            and not self._save_blocked
            and time.monotonic() - self._last_edit >= 0.35
            and self._controller.can_edit
        ):
            update = self._selection()
            if update is not None and self._controller.save(update):
                self._render_activity()
        if time.monotonic() >= self._next_refresh and not self._controller.busy and not self._dirty:
            self.refresh()
        if (
            self._notification_status is not None
            and not self._dirty
            and self._controller.error is None
        ):
            error = self._notification_status()
            if error != self._notification_error:
                self._notification_error = error
                self._set_message(error or "")

        if (
            self._main_window
            and self._main_window.window.isVisible()
            and not self._main_window.calendar
        ):
            self._render_home()

        if self._shutdown_requested is not None and self._shutdown_requested():
            self.quit()

    def refresh(self, *, manual: bool = False) -> None:
        """명시적 새로고침만 실패한 입력을 확정 기록으로 되돌린다."""
        if self._controller.busy:
            return
        if manual and self._save_blocked:
            self._dirty = False
            self._base = None
            self._save_blocked = False
            self._controller.draft = None
        if self._controller.refresh():
            self._next_refresh = time.monotonic() + 15
            self._render_activity()
        # 작은 달력에는 따로 새로고침 버튼을 두지 않으므로 패널 새로고침이 달력도 다시 읽는다.
        if manual and self._calendar_controller is not None and not self._calendar_controller.busy:
            self._calendar_controller.refresh()
            self._render_calendars()

    def change_interval(self) -> None:
        """기록 저장과 겹치지 않게 새 주기를 저장하고 즉시 반응을 보인다."""
        selected = self._interval.indexOfSelectedItem()
        if self._dirty or not 0 <= selected < len(INTERVAL_MINUTES):
            return
        if self._controller.set_interval(INTERVAL_MINUTES[selected] * 60):
            self._render_activity()

    def _render_activity(self) -> None:
        """기존 목록을 유지한 채 진행 표시와 중복 클릭 제한만 바꾼다."""
        controller = self._controller
        snapshot = controller.snapshot
        loading = controller.busy and controller.operation in {"fetch", "interval"}
        collecting = snapshot is not None and snapshot.collection.running and controller.connected
        active = loading or collecting
        # 연결 전에는 상태 줄 대신 연결 버튼만 두므로 상태 아이콘·진행 표시도 숨긴다.
        self._collection_icon.setHidden_(active or self._empty)
        self._spinner.setHidden_(not active or self._empty)
        if active:
            self._spinner.startAnimation_(None)
        else:
            self._spinner.stopAnimation_(None)
        self._refresh_button.setEnabled_(not controller.busy)
        self._interval.setEnabled_(
            not controller.busy
            and not self._dirty
            and controller.connected
            and snapshot is not None
            and snapshot.collection.enabled
        )
        if loading and (not collecting or controller.operation == "interval"):
            self._collection.setStringValue_(
                "주기 저장 중…" if controller.operation == "interval" else "새로고침 중…"
            )
        elif controller.error and controller.operation == "interval":
            self._collection.setStringValue_("새로고침해 수집 주기를 확인하세요.")

    def _fill_form(self, snapshot: DesktopSnapshot) -> None:
        """조회한 확정 값을 표시하되 새 날짜를 자동 출근 기록하지 않는다."""
        base = snapshot.day.update
        clock = parse_clock(base.attendance or "09:00")
        self._set_arrival(clock.hour, clock.minute)
        selected = base.leave if base.leave != LeaveType.UNSELECTED else LeaveType.NONE
        self.leave.selectItemAtIndex_(SELECTABLE_LEAVES.index(selected))
        self._base = base
        self._forecast()

    def _set_status_description(self, state: str) -> None:
        """메뉴바 아이콘의 도움말과 VoiceOver에 같은 수집 상태를 제공한다."""
        name = "Catch"
        if self._app_updates and self._app_updates.actions.is_development:
            name += " 개발용"
        description = f"{name} · {state}"
        status_button = self._status_item.button()
        status_button.setToolTip_(description)
        status_button.setAccessibilityLabel_(description)

    def _render(self) -> None:
        """원천 목록과 수집 상태를 갱신하며 미저장 선택은 그대로 둔다."""
        controller = self._controller
        snapshot = controller.snapshot
        if snapshot is None:
            title = "연결 확인 필요" if controller.error else "연결 중"
            self._collection.setStringValue_(title)
            self._collection.setToolTip_(title)
            self._set_status_description(title)
            self._render_activity()
            return
        if not self._dirty:
            self._fill_form(snapshot)
        else:
            self._forecast()
        day = datetime.fromisoformat(snapshot.day.update.day)
        weekdays = ("월", "화", "수", "목", "금", "토", "일")
        self._date.setStringValue_(f"{day.month}월 {day.day}일 {weekdays[day.weekday()]}요일")
        collection = snapshot.collection
        disconnected = not controller.connected
        reasons = snapshot.freshness_reasons
        failed = collection.failed > 0 or "last_sync_failed" in reasons
        delayed = collection.enabled and "source_expired" in reasons and not collection.running
        needs_refresh = any(
            reason in reasons for reason in ("source_mapping_changed", "source_timestamp_in_future")
        )
        attention = disconnected or (
            not collection.running and (failed or delayed or needs_refresh)
        )
        if disconnected:
            title = "로컬 앱 연결 끊김"
        elif collection.running:
            name = next(
                (
                    source.name or source.title
                    for source in self._source_display
                    if source.id == collection.active_source_id
                ),
                None,
            )
            title = f"수집 중 · {name}" if name else "수집 중"
        elif collection.retrying:
            title = f"재시도 대기 · {collection.retry_attempt}/3"
        elif failed:
            title = "수집 실패"
        elif (
            not collection.enabled
            and self._source_editor is not None
            and self._source_editor.state is not None
            and not self._source_display
        ):
            title = (
                "카테고리 지정 필요"
                if self._source_editor.state.settings.sources
                else "수집 소스 없음"
            )
        elif not collection.enabled:
            title = "수집 꺼짐"
        elif "source_mapping_changed" in reasons:
            title = "새 설정으로 수집할 때까지 대기 중"
        elif "source_timestamp_in_future" in reasons:
            title = "최근 수집 시각이 현재 시각보다 늦습니다."
        elif "source_not_collected" in reasons:
            title = "첫 수집 대기"
        elif delayed:
            title = "수집 지연"
        else:
            title = "수집 대기"
        self._collection.setStringValue_(title)
        self._collection.setToolTip_(title)
        # 연결한 표가 없으면 수집 상태·시각·주기가 의미 없으므로 연결 버튼 하나만 남긴다.
        empty = title == "수집 소스 없음"
        self._empty = empty
        self._connect_button.setHidden_(not empty)
        for control in (
            self._collection,
            self._collection_detail,
            self._interval_label,
            self._interval,
        ):
            control.setHidden_(empty)
        self._collection_icon.setContentTintColor_(
            NSColor.secondaryLabelColor()
            if empty
            else NSColor.systemOrangeColor()
            if attention
            else NSColor.systemGreenColor()
        )
        detail = f"최근 {clock_label(collection.last_success)}  ·  다음 {clock_label(collection.next_at)}"
        if (
            failed
            and not collection.running
            and not collection.retrying
            and collection.last_success is not None
        ):
            detail = f"저장된 자료 표시 중  ·  최근 수집 {clock_label(collection.last_success)}"
        if snapshot.work_error:
            detail = "수집한 항목을 불러오지 못했습니다."
        self._collection_detail.setStringValue_(detail)
        self._collection_detail.setToolTip_(detail)
        seconds = collection.interval_seconds
        minutes = seconds // 60 if seconds is not None and seconds % 60 == 0 else None
        # 환경에만 있는 사용자 지정 간격을 임의의 다른 값으로 표시하지 않는다.
        self._interval.selectItemAtIndex_(
            INTERVAL_MINUTES.index(minutes) if minutes in INTERVAL_MINUTES else -1
        )
        self._render_activity()
        self._set_status_description(title)
        if controller.error:
            self._set_message(
                "기록 변경 · 새로고침"
                if controller.conflict
                else "저장 실패 · 입력 유지"
                if controller.draft
                else "연결 확인 필요"
            )
        if (
            snapshot.day.state in {"weekend_manual", "non_workday"}
            and snapshot.day.update.attendance is None
            and not self._dirty
            and not controller.error
        ):
            self._set_message("휴무")
        enabled = controller.connected and not controller.conflict
        self.arrival.setEnabled_(enabled)
        self.leave.setEnabled_(enabled)
        self._render_source_lists(snapshot)

    def _apply_source_display(self, display: tuple[SourceDisplay, ...]) -> None:
        """확정된 카테고리 카드만 배치에 추가한다."""
        self._source_display = display
        self._calendar_choices = calendar_filter_choices(display)
        groups = display_groups(display)
        self._source_groups = groups
        # 숨김 선택은 뷰를 보존하고, 연결이 사라진 카드는 네이티브 컨트롤까지 제거한다.
        for key in self._custom_sections.keys() - groups.keys():
            self._custom_sections.pop(key).view.removeFromSuperview()
        self._custom_expanded.intersection_update(groups)
        for index, (key, group) in enumerate(groups.items()):
            view = self._custom_sections.get(key)
            if view is None:
                view = EntrySection(
                    self._body,
                    group[0].title,
                    "square.stack",
                    self._callbacks,
                    "openSourceBoard:",
                    "expandSource:",
                    POPOVER_WIDTH,
                    title_width=310,
                )
                self._custom_sections[key] = view
            view.board.setTag_(index)
            view.expand.setTag_(index)
            view.expand.setTitle_("축소" if key in self._custom_expanded else "확대")
        self._rendered_base_height = -1
        self._resize_lists()
        self._render()
        self._render_calendars()

    def open_source_board(self, index: int) -> None:
        """현재 카테고리에 유일한 원본 주소를 연다."""
        areas = tuple(self._source_groups.values())
        if 0 <= index < len(areas):
            self._open_notion(group_board_url(areas[index]))

    def expand_source(self, index: int) -> None:
        """선택한 카테고리의 카드 높이만 바꾼다."""
        keys = tuple(self._source_groups)
        if not 0 <= index < len(keys):
            return
        key = keys[index]
        if key in self._custom_expanded:
            self._custom_expanded.remove(key)
        else:
            self._custom_expanded.add(key)
        self._custom_sections[key].expand.setTitle_(
            "축소" if key in self._custom_expanded else "확대"
        )
        self._resize_lists(animated=True)

    def _source_rows(
        self, snapshot: DesktopSnapshot, key: str
    ) -> tuple[int, tuple[MenuEntry, ...]]:
        """홈과 메뉴바가 서버의 동일한 카테고리 순서를 사용한다."""
        group = self._source_groups[key]
        # 사용자 규칙을 적용한 카드는 서버가 소스 경계를 넘어 확정한 순서를 쓴다.
        area = snapshot.category_areas.get(key)
        areas = (
            (area,)
            if area is not None
            else tuple(
                snapshot.source_areas[item.id] for item in group if item.id in snapshot.source_areas
            )
        )
        count = sum(area.count for area in areas)
        rows = tuple(entry for area in areas for entry in area.entries)
        return count, rows

    def _render_source_lists(self, snapshot: DesktopSnapshot) -> None:
        """카테고리에 연결한 소스들의 미리보기와 실제 개수를 합산한다."""
        for key, group in self._source_groups.items():
            count, rows = self._source_rows(snapshot, key)
            view = self._custom_sections[key]
            suffix = "" if snapshot.work_error else f" {count}"
            view.title.setStringValue_(group[0].title + suffix)
            view.board.setEnabled_(group_board_url(group) is not None)
            view.entries.update(rows)

    def update_page_links(self, state: NotionPageLinksState) -> None:
        """이전 링크 편집 클라이언트가 있다면 보드 주소 버전만 보존한다."""
        pages = tuple(
            next((item.url for item in state.sources if item.section == section and item.url), None)
            for section in ("tasks", "qa", "meetings")
        )
        self._controller.update_page_links(state.revision, (pages[0], pages[1], pages[2]))

    def _open_notion(self, value: str | None) -> None:
        """카테고리 카드와 항목에 같은 원본 주소 검증을 적용한다."""
        url = notion_link(value)
        if url is not None:
            self._open_url(url)

    def _close_calendar_filters(self) -> None:
        """메인 창이 닫힐 때 필터 선택창을 화면에 남기지 않는다."""
        if self._calendar_view is not None:
            self._calendar_view.filter_popover.hide()

    def _calendar_preferences(self) -> CalendarPreferences:
        """저장 중에도 두 달력에서 최신 표시 선택을 사용한다."""
        value = self._preferences_controller.value
        return value.calendar if value is not None else CalendarPreferences()

    def _calendar_filter_choices(self) -> tuple[CalendarFilterChoice, ...]:
        """두 달력에 같은 카테고리 선택지를 제공한다."""
        return self._calendar_choices

    def _toggle_calendar_filter(self, key: str, visible: bool) -> None:
        """카테고리와 출근 기록 표시만 저장한다."""
        value = self._preferences_controller.value
        if value is None or key not in {item.key for item in self._calendar_filter_choices()}:
            return
        preferences = value.calendar
        if key == "attendance":
            selected = set(preferences.visible_kinds)
            if visible:
                selected.add("attendance")
            else:
                selected.discard("attendance")
            preferences = replace(
                preferences,
                visible_kinds=tuple(item for item in CALENDAR_KINDS if item in selected),
            )
        else:
            hidden = set(preferences.hidden_categories)
            shown = set(preferences.shown_categories)
            if visible:
                hidden.discard(key)
                shown.add(key)
            else:
                hidden.add(key)
                shown.discard(key)
            preferences = replace(
                preferences,
                hidden_categories=tuple(sorted(hidden)),
                shown_categories=tuple(sorted(shown)),
            )
        self._preferences_controller.change(replace(value, calendar=preferences))
        self._render_calendars()

    def _notify_calendar_loaded(self) -> None:
        """백그라운드 조회 결과는 AppKit 메인 스레드에서만 표시한다."""
        self._callbacks.performSelectorOnMainThread_withObject_waitUntilDone_(
            "calendarLoaded:", None, False
        )

    def calendar_loaded(self) -> None:
        """종료되거나 이미 반영된 월 응답은 다시 그리지 않는다."""
        if self._calendar_controller is not None and self._calendar_controller.apply_completed():
            self._render_calendars()

    def _compact_calendar_visible(self) -> bool:
        """메뉴바 패널과 달력 카드가 동시에 표시될 때만 격자를 그린다."""
        return self._panel.is_visible() and any(
            item.key == "calendar" and item.visible for item in self._preferences.sections
        )

    def _render_calendars(self) -> None:
        """필터 입력과 조회 진행을 두 화면에서 즉시 반영한다."""
        enabled = (
            self._preferences_controller.value is not None
            and not self._preferences_controller.loading
            and self._preferences_controller.error is None
        )
        if self._calendar_view is not None and self._compact_calendar_visible():
            self._calendar_view.render(enabled)
        window = self._main_window
        if window is not None and window.window.isVisible():
            if window.calendar and window.page:
                window.page.render(enabled)
            elif not window.calendar and window.compact:
                window.compact.render(enabled)

    def _main_calendar_visible(self) -> bool:
        """홈 달력이나 월간 화면이 실제로 보이는 동안만 자동 조회한다."""
        window = self._main_window
        return bool(window and window.window.isVisible() and (window.calendar or window.compact))

    def open_main(self, *, calendar: bool = False) -> None:
        """명시적인 실행 요청에서만 유리 창을 생성하고 앞으로 가져온다."""
        self._panel.hide()
        if self._main_window is None:
            controller = self._calendar_controller

            def month(parent: NSView) -> CalendarPage:
                """메뉴바와 같은 월 조회 및 필터를 연결한다."""
                assert controller is not None
                return CalendarPage(
                    parent,
                    controller,
                    self._calendar_preferences,
                    self._toggle_calendar_filter,
                    self._open_notion,
                    choices=self._calendar_filter_choices,
                )

            def compact(parent: NSView, width: float) -> CompactCalendarView:
                """선택된 홈 달력 카드는 메뉴바와 같은 컨트롤을 쓴다."""
                assert controller is not None
                return CompactCalendarView(
                    parent,
                    width,
                    controller,
                    self._calendar_preferences,
                    self._toggle_calendar_filter,
                    self.open_calendar,
                    inset=0,
                    choices=self._calendar_filter_choices,
                )

            self._main_window = MainWindow(
                self._callbacks, month if controller else None, compact if controller else None
            )
        self._render_home()
        self._main_window.show(calendar)
        self._next_refresh = 0
        self._render_calendars()

    def _render_home(self) -> None:
        """확정 설정과 현재 스냅샷에서 홈에 선택된 카드만 전달한다."""
        if self._main_window is None:
            return
        snapshot = self._controller.snapshot
        cards: list[HomeCard] = []
        if self._preferences_controller.confirmed is not None:
            for section in self._section_order:
                if not section.visible:
                    continue
                key = section.key
                if key == "work":
                    cards.append(
                        HomeCard(
                            key,
                            "근무",
                            message=f"{self._departure_title.stringValue()}  {self._departure.stringValue()}",
                        )
                    )
                elif key == "calendar":
                    if self._calendar_controller:
                        cards.append(HomeCard(key, "캘린더"))
                else:
                    title = self._source_groups[key][0].title
                    if snapshot is None or snapshot.work_error:
                        message = (
                            "연결 확인 필요"
                            if self._controller.error or snapshot
                            else "불러오는 중…"
                        )
                        cards.append(HomeCard(key, title, message=message))
                    else:
                        count, rows = self._source_rows(snapshot, key)
                        cards.append(HomeCard(key, f"{title} {count}", rows))
        self._main_window.update(tuple(cards), self._empty_view.stringValue())

    def open_calendar(self) -> None:
        """같은 메인 창에서 선택 날짜의 큰 달력을 연다."""
        if self._calendar_controller is not None:
            self.open_main(calendar=True)

    def _records_saved(self, day: date) -> None:
        """오늘 정정은 다음 조회로 반영하고 메뉴바의 미저장 입력은 보존한다."""
        if self._calendar_controller is not None:
            self._calendar_controller.invalidate()
        if day.isoformat() == self._today():
            self._next_refresh = 0

    def open_records(self) -> None:
        """설정과 분리한 기록 창을 열고 선택·초안을 재사용한다."""
        if self._records_controller is None:
            return
        self._panel.hide()
        if self._records_window is None:
            self._records_window = WorkRecordsWindow(self._records_controller)
        self._records_window.show()

    def open_settings(self, *, screen: bool = False, page: SettingsPage | None = None) -> None:
        """메뉴바를 닫고 재사용 가능한 설정 창을 표시한다."""
        self._panel.hide()
        if self._settings_window is None:
            self._settings_window = PreferencesWindow(
                self._preferences_controller,
                self._notification_channels,
                self._runtime_settings_controller,
                self._notion_credentials_controller,
                self._mcp_settings_controller,
                self._page_links_controller,
                self._identity_controller,
                self._slack_settings_controller,
                self._work_rules_controller,
                self._pending_work_edits,
                self._source_editor,
                self._source_client,
                self._data_backup,
                self._app_updates,
            )
        self._settings_window.show(page=SettingsPage.SCREEN if screen else page)

    def open_connection_settings(self) -> None:
        """토큰이 없으면 Notion 인증부터, 있으면 표를 연결할 수집 소스 페이지를 연다."""
        controller = self._notion_credentials_controller
        state = controller.state if controller is not None else None
        configured = state is not None and state.configured
        self.open_settings(page=SettingsPage.SOURCES if configured else SettingsPage.NOTION)

    def _update_pending(self) -> bool:
        """설정 창을 열기 전에도 메뉴바와 기록 창의 편집·저장을 보호한다."""
        if self._settings_window is not None:
            return self._settings_window.update_pending()
        return self._pending_work_edits()

    def _pending_work_edits(self) -> bool:
        """근무 카드나 기록 창에서 저장할 내용이 남았는지 확인한다."""
        records = self._records_controller
        return (
            self._dirty
            or (self._controller.busy and self._controller.operation == "save")
            or bool(records and (records.dirty or records.saving))
        )

    def _slack_member_saved(self) -> None:
        """Notion 미저장 초안은 유지하고 깨끗한 설정 버전만 갱신한다."""
        controller = self._runtime_settings_controller
        if controller is not None and not controller.dirty and not controller.busy:
            controller.reload()

    def close(self) -> None:
        """타이머·작업자·상태 아이템을 정리한다."""
        self._stop_resize_animation()
        self._panel.close()
        if self._timer:
            self._timer.invalidate()
            self._timer = None
        self._controller.close()
        if self._main_window is not None:
            self._main_window.window.close()
        if self._calendar_controller is not None:
            self._calendar_controller.close()
        if self._records_window is not None:
            self._records_window.close()
        if self._records_controller is not None:
            self._records_controller.close()
        if self._settings_window is not None:
            self._settings_window.dispose()
        if self._app_updates:
            self._app_updates.close()
        self._preferences_controller.close()
        if self._identity_controller is not None:
            self._identity_controller.close()
        for controller in self._settings_controllers:
            controller.close()
        NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)

    def termination_pending(self) -> bool:
        """저장 작업 또는 미적용 입력이 있으면 종료를 보류한다."""
        if any(controller.busy for controller in self._settings_controllers):
            return True
        if self._records_controller is not None and self._records_controller.saving:
            return True
        if self._identity_controller is not None and self._identity_controller.busy:
            return True
        if self._preferences_controller.busy or (
            self._preferences_controller.dirty and self._preferences_controller.error is None
        ):
            return True
        if self._dirty or (self._controller.busy and self._controller.operation == "save"):
            self._set_message("저장 확인 후 종료")
            return True
        return self._update_pending()

    def quit(self) -> None:
        """메뉴바 종료도 Dock과 동일한 앱 종료 요청으로 처리한다."""
        if not self.termination_pending():
            self._application.terminate_(None)
