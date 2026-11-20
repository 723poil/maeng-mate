"""현재 수집 매핑을 설정 화면에서 읽을 수 있는 설명으로 바꾼다."""

from src.domain.models import SourceMapping


def source_summary(source: SourceMapping) -> str:
    """실제 연결된 속성과 용도만 설명하며 없는 속성을 추측하지 않는다."""
    category = "회의" if source.kind == "meeting" else "QA" if source.section == "qa" else "업무"
    roles = (
        ("title", "제목", "목록에 표시할 이름"),
        ("assignees", "담당자", "본인 업무 판별"),
        ("status", "상태", "원본 상태 표시·검색"),
        ("dueDate", "기한", "마감일 표시·기한 변경 확인"),
        ("participants", "참석자", "본인 참석 일정 판별"),
        ("period", "기간", "일정 날짜·시각 표시"),
        ("category", "일정 분류", "회의·휴가·기타 구분"),
        ("cancelled", "취소 상태", "취소 일정 제외"),
    )
    lines = [f"{category} 수집 속성"]
    if source.round_title_property is not None:
        column = " ".join(source.round_title_property.split())
        lines.append("부모 항목 안의 하위 표에서 수집")
        if source.parent_label:
            lines.append(f"부모 제목 표시 ({column})")
    for key, label, purpose in roles:
        if key in source.properties:
            column = " ".join(source.properties[key].split())
            lines.append(f"{label} ({column}): {purpose}")
    lines.append("링크를 바꿔도 수집 연결은 유지됩니다.")
    return "\n".join(lines)
