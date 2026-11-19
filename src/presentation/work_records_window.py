"""월별 출퇴근 목록과 오른쪽 수정·이력을 한 화면에 배치한다."""

from datetime import date

from AppKit import (
    NSApplication,
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSColor,
    NSImage,
    NSNoBorder,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSScreen,
    NSScrollView,
    NSTextAlignmentCenter,
    NSView,
)
from Foundation import NSObject

from src.application.work_records import WorkRecordCorrection, WorkRecordsMonth
from src.application.workday import LEAVE_LABELS, SELECTABLE_LEAVES, extra_work_label
from src.domain.daily_work import LeaveType
from src.domain.work_dates import SEOUL
from src.presentation.glass_panel import auxiliary_window
from src.presentation.native_views import button, card, frame, glass_container, label
from src.presentation.settings_badge import SettingsBadge
from src.presentation.work_records_controller import WorkRecordsController
from src.presentation.work_records_fields import RecordClockPicker
from src.presentation.work_records_table import WEEKDAYS, WorkRecordsTable


class RecordsWindowActions(NSObject):
    """창의 월 이동·입력·저장 동작을 명시적 콜백으로 분리한다."""

    owner: "WorkRecordsWindow"

    def previous_(self, sender: object) -> None:
        """수정 중이 아닐 때 이전 달로 이동한다."""
        self.owner.shift_month(-1)

    def next_(self, sender: object) -> None:
        """현재 월보다 미래로 이동하지 않는다."""
        self.owner.shift_month(1)

    def current_(self, sender: object) -> None:
        """서버 기준 이번 달 목록을 다시 읽는다."""
        self.owner.controller.load()
        self.owner.render()

    def reload_(self, sender: object) -> None:
        """불명확한 저장 결과는 사용자가 다시 읽어 확인한다."""
        self.owner.controller.reload()
        self.owner.render()

    def changed_(self, sender: object) -> None:
        """시·분·휴가 선택은 자동 저장 없이 초안으로 남긴다."""
        self.owner.controller.edited()
        self.owner.render()

    def cancel_(self, sender: object) -> None:
        """수정 초안을 취소하고 확정값으로 되돌린다."""
        self.owner.controller.cancel()
        self.owner.render()

    def save_(self, sender: object) -> None:
        """현재 선택 날짜의 실제 기록만 명시적으로 저장한다."""
        self.owner.save()

    def confirm_(self, sender: object) -> None:
        """수정 입력과 섞지 않고 표시된 예정시간으로 퇴근을 확정한다."""
        self.owner.controller.confirm()
        self.owner.render()


class WorkRecordsWindow:
    """고정된 두 영역에서 날짜 선택과 실제 시간 정정을 함께 처리한다."""

    def __init__(self, controller: WorkRecordsController) -> None:
        """파일·DB 없이 조회·저장 제어기만 주입한다."""
        self.controller = controller
        self._month_generation = -1
        self._generation = -1
        self._history_key: object = None
        self._actions = RecordsWindowActions.alloc().init()
        self._actions.owner = self
        screen = NSScreen.mainScreen()
        height = min(660.0, max(600.0, screen.visibleFrame()[1][1] - 80)) if screen else 640.0
        self._window = auxiliary_window("출퇴근 기록", 900, height)
        content = NSView.alloc().initWithFrame_(frame(0, 0, 900, height))
        self._window.setContentView_(glass_container(content, 900, height))
        label(content, "출퇴근 기록", 82, height - 48, 100, size=18, bold=True)
        self._spinner = NSProgressIndicator.alloc().initWithFrame_(frame(184, height - 44, 16, 16))
        self._spinner.setStyle_(NSProgressIndicatorStyleSpinning)
        self._spinner.setDisplayedWhenStopped_(False)
        content.addSubview_(self._spinner)
        self._current = button(content, "이번 달", 208, height - 50, 68, self._actions, "current:")
        self._previous = button(content, "‹", 282, height - 50, 30, self._actions, "previous:")
        self._month = label(content, "", 320, height - 46, 110, size=15, bold=True)
        self._month.setAlignment_(NSTextAlignmentCenter)
        self._next = button(content, "›", 438, height - 50, 30, self._actions, "next:")
        for control, symbol, title in (
            (self._previous, "chevron.left", "이전 달"),
            (self._next, "chevron.right", "다음 달"),
        ):
            control.setTitle_("")
            control.setImage_(
                NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, title)
            )
        self._summary = label(content, "", 24, height - 88, 480, size=12, secondary=True)
        card(content, 16, 58, 500, height - 162)
        self._table = WorkRecordsTable(content, frame(24, 66, 484, height - 180), self.select)
        self._empty = label(content, "불러오는 중…", 100, height / 2, 320, size=13, secondary=True)
        card(content, 532, 58, 352, height - 162)
        self._day = label(content, "날짜를 선택하세요.", 548, height - 144, 210, size=16, bold=True)
        self._badge = SettingsBadge(content, 786, height - 145, 82)
        label(content, "퇴근 예정", 548, height - 188, 88, size=12, secondary=True)
        self._expected = label(content, "—", 640, height - 191, 112, size=18, bold=True)
        self._confirm = button(content, "확정", 788, height - 194, 80, self._actions, "confirm:")
        divider = NSBox.alloc().initWithFrame_(frame(548, height - 210, 320, 1))
        divider.setBoxType_(NSBoxSeparator)
        content.addSubview_(divider)
        label(content, "출근", 548, height - 246, 56, size=12, secondary=True)
        self.arrival = RecordClockPicker(content, 618, height - 252, self._actions)
        label(content, "퇴근", 548, height - 286, 56, size=12, secondary=True)
        self.departure = RecordClockPicker(content, 618, height - 292, self._actions)
        label(content, "휴가", 548, height - 326, 56, size=12, secondary=True)
        self.leave = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            frame(618, height - 332, 144, 28), False
        )
        self.leave.addItemsWithTitles_([LEAVE_LABELS[value] for value in SELECTABLE_LEAVES])
        self.leave.setTarget_(self._actions)
        self.leave.setAction_("changed:")
        content.addSubview_(self.leave)
        self.overtime = NSButton.checkboxWithTitle_target_action_("야근", self._actions, "changed:")
        self.overtime.setFrame_(frame(774, height - 332, 94, 28))
        content.addSubview_(self.overtime)
        # 저장·취소는 오른쪽 입력 폼의 동작이며 창 전체나 수정 이력의 동작이 아니다.
        self._cancel = button(content, "취소", 700, height - 370, 80, self._actions, "cancel:")
        self._save = button(content, "저장", 788, height - 370, 80, self._actions, "save:")
        history_divider = NSBox.alloc().initWithFrame_(frame(548, height - 386, 320, 1))
        history_divider.setBoxType_(NSBoxSeparator)
        content.addSubview_(history_divider)
        label(content, "수정 이력", 548, height - 414, 200, size=13, bold=True)
        self._history = NSScrollView.alloc().initWithFrame_(frame(548, 74, 320, height - 506))
        self._history.setHasVerticalScroller_(True)
        self._history.setAutohidesScrollers_(True)
        self._history.setDrawsBackground_(False)
        self._history.setBorderType_(NSNoBorder)
        content.addSubview_(self._history)
        self._reload = button(content, "다시 불러오기", 24, 20, 112, self._actions, "reload:")
        self._status = label(content, "", 148, 24, 532, size=12, secondary=True)
        self.render()

    def show(self) -> None:
        """재열기에서 초안·선택을 보존하고 수정 중이 아닐 때 최신 기록을 읽는다."""
        if not self.controller.dirty and not self.controller.busy:
            self.controller.load(self.controller.month.month if self.controller.month else None)
        self.render()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def is_visible(self) -> bool:
        """숨은 기록 창의 주기적인 표시 갱신을 건너뛸 수 있게 한다."""
        return self._window.isVisible()

    def close(self) -> None:
        """창을 숨기며 다시 열 때 선택과 미저장 내용을 보존한다."""
        self._window.close()

    def select(self, day: date) -> None:
        """날짜 선택 즉시 진행 상태를 표시한다."""
        self.controller.select(day)
        self.render()

    def shift_month(self, amount: int) -> None:
        """월 끝·연도 경계를 날짜 값으로 안전하게 이동한다."""
        month = self.controller.month
        if month is None:
            return
        year, number = map(int, month.month.split("-"))
        index = year * 12 + number - 1 + amount
        target = f"{index // 12:04d}-{index % 12 + 1:02d}"
        if index // 12 < 1 or target > month.today.strftime("%Y-%m"):
            return
        self.controller.load(target)
        self.render()

    def save(self) -> None:
        """미기록은 None으로 전달하고 퇴근 예상을 대신 저장하지 않는다."""
        detail = self.controller.detail
        selected = self.leave.indexOfSelectedItem()
        if detail is None or not 0 <= selected < len(SELECTABLE_LEAVES):
            return
        self.controller.save(
            WorkRecordCorrection(
                detail.item.day,
                detail.item.revision,
                self.arrival.value(),
                self.departure.value(),
                SELECTABLE_LEAVES[selected],
                self.overtime.state() == 1,
            )
        )
        self.render()

    def _render_month_summary(self, month: WorkRecordsMonth) -> None:
        """새 월 조회나 저장으로 확정된 자료에서만 월 제목과 일수를 갱신한다."""
        year, number = map(int, month.month.split("-"))
        self._month.setStringValue_(f"{year}년 {number}월")
        recorded = sum(item.revision > 0 for item in month.items)
        confirmed = sum(item.departure is not None for item in month.items)
        half = sum(
            item.leave in {LeaveType.AM_HALF, LeaveType.PM_HALF} and item.revision > 0
            for item in month.items
        )
        quarter = sum(
            item.leave in {LeaveType.AM_QUARTER, LeaveType.PM_QUARTER} and item.revision > 0
            for item in month.items
        )
        overtime = sum(item.overtime and item.day.weekday() < 5 for item in month.items)
        weekend_work = sum(item.overtime and item.day.weekday() >= 5 for item in month.items)
        self._summary.setStringValue_(
            f"전체 기록 {recorded}일 · 퇴근 기록 {confirmed}일 · 반차 {half}회 · 반반차 {quarter}회 · 야근 {overtime}일 · 주말근무 {weekend_work}일"
        )

    def render(self) -> None:
        """확정 응답에서만 입력을 채우고 진행 중에는 위치를 유지한다."""
        controller = self.controller
        month, detail = controller.month, controller.detail
        if month is not None and self._month_generation != controller.month_generation:
            self._render_month_summary(month)
            self._month_generation = controller.month_generation
        self._table.update(
            month.items if month else (),
            controller.selected,
            month.today if month else None,
            not controller.busy and not controller.dirty,
        )
        self._empty.setHidden_(bool(month and month.items))
        self._empty.setStringValue_(
            "선택한 달에 출퇴근 기록이 없습니다."
            if month
            else "불러오는 중…"
            if controller.busy
            else "기록을 불러오지 못했습니다."
        )
        if self._generation != controller.generation:
            self._generation = controller.generation
            if detail is not None:
                item = detail.item
                self._day.setStringValue_(
                    f"{item.day.month}월 {item.day.day}일 {WEEKDAYS[item.day.weekday()]}요일"
                )
                self.arrival.set_value(item.arrival)
                self.departure.set_value(item.departure)
                self.overtime.setState_(int(item.overtime))
                self.leave.selectItemAtIndex_(
                    SELECTABLE_LEAVES.index(item.leave) if item.leave in SELECTABLE_LEAVES else 0
                )
            else:
                selected = controller.selected
                self._day.setStringValue_(
                    f"{selected.month}월 {selected.day}일 {WEEKDAYS[selected.weekday()]}요일"
                    if selected
                    else "날짜를 선택하세요."
                )
                self.arrival.set_value(None)
                self.departure.set_value(None)
                self.overtime.setState_(0)
                self.leave.selectItemAtIndex_(0)
        editable = controller.editable
        self.arrival.set_enabled(editable)
        self.departure.set_enabled(editable)
        self.leave.setEnabled_(editable)
        self.overtime.setEnabled_(editable)
        selected = controller.selected
        work_label = extra_work_label(selected) if selected else "야근"
        self.overtime.setTitle_(work_label)
        self.overtime.setToolTip_(
            f"{work_label} 여부를 기록합니다. 퇴근 예정 계산은 바뀌지 않습니다."
        )
        self._day.setTextColor_(
            NSColor.systemRedColor()
            if selected and selected.weekday() == 6
            else NSColor.systemBlueColor()
            if selected and selected.weekday() == 5
            else NSColor.labelColor()
        )
        expected = (
            detail.expected_departure
            if detail is not None and detail.item.day == controller.selected
            else None
        )
        self._expected.setStringValue_(
            expected.astimezone(SEOUL).strftime("%H:%M") if expected else "—"
        )
        self._confirm.setEnabled_(controller.can_confirm)
        self._confirm.setToolTip_(
            "바꾼 내용을 남기려면 ‘저장’을 누르세요."
            if controller.dirty
            else "휴무"
            if detail and detail.item.day.weekday() >= 5 and detail.item.revision == 0
            else "이미 퇴근을 확정했습니다. 시간을 바꾸려면 수정한 뒤 ‘저장’을 누르세요."
            if detail and detail.item.departure
            else "주말 근무는 실제 퇴근시간을 입력하고 저장하세요."
            if detail and detail.item.day.weekday() >= 5
            else "퇴근 예정 시각부터 ‘확정’을 눌러 그 시각을 실제 퇴근 시간으로 기록할 수 있습니다."
        )
        self._save.setEnabled_(editable and controller.dirty)
        self._cancel.setEnabled_(not controller.busy and (controller.dirty or controller.blocked))
        can_move = month is not None and not controller.busy and not controller.dirty
        self._previous.setEnabled_(can_move)
        self._current.setEnabled_(can_move)
        self._next.setEnabled_(
            can_move and month is not None and month.month < month.today.strftime("%Y-%m")
        )
        self._reload.setEnabled_(
            not controller.busy and (not controller.dirty or controller.blocked)
        )
        # 확정 여부·수정 상태는 오른쪽 배지로만 표시하고 하단은 진행·오류에만 쓴다.
        footer = controller.message if controller.busy or controller.error else ""
        self._status.setStringValue_(footer)
        self._status.setToolTip_(footer or None)
        self._badge.update(
            "확인 필요"
            if controller.error
            else "확정 중"
            if controller.confirming
            else "저장 중"
            if controller.saving
            else "조회 중"
            if controller.busy
            else "수정 중"
            if controller.dirty
            else "확정됨"
            if detail and detail.item.departure is not None
            else "미확정"
            if expected is not None
            else "저장됨"
            if detail and detail.item.revision
            else "미기록",
            "orange"
            if controller.error
            else "blue"
            if controller.busy or controller.dirty
            else "green"
            if detail and detail.item.departure is not None
            else "neutral",
        )
        if controller.busy:
            self._spinner.startAnimation_(None)
        else:
            self._spinner.stopAnimation_(None)
        self._render_history()

    def _render_history(self) -> None:
        """선택 날짜·버전이 바뀔 때만 이력을 그려 읽던 위치를 유지한다."""
        detail = self.controller.detail
        key = detail if detail and detail.item.day == self.controller.selected else None
        if key == self._history_key:
            return
        self._history_key = key
        versions = key.history if key else ()
        viewport = self._history.frame()[1][1]
        height = max(
            viewport,
            sum(26 + max(1, len(version.changes)) * 20 + 12 for version in versions)
            + (24 if detail and detail.truncated else 0),
        )
        view = NSView.alloc().initWithFrame_(frame(0, 0, 320, height))
        top = height - 4
        if not versions:
            label(view, "수정 이력 없음", 0, top - 24, 300, size=12, secondary=True)
        for version in versions:
            timestamp = (
                version.updated_at.astimezone(SEOUL).strftime("%m.%d %H:%M")
                if version.updated_at
                else "수정 시각 없음"
            )
            label(view, timestamp, 0, top - 20, 300, size=11, bold=True, secondary=True)
            top -= 26
            for change in version.changes:
                text = label(view, change, 0, top - 18, 300, size=12)
                text.setToolTip_(change)
                top -= 20
            top -= 12
        if detail and detail.truncated:
            label(view, "최근 50회 표시", 0, top - 18, 300, size=11, secondary=True)
        self._history.setDocumentView_(view)
        view.scrollPoint_((0, max(0, height - viewport)))
