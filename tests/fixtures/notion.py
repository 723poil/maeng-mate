"""회사 자료 없는 Notion 응답과 소스 매핑이다."""

from src.domain.models import SourceMapping

SOURCE = SourceMapping(
    "task", "fake-source", {"title": "t", "assignees": "p", "status": "s", "dueDate": "d"}
)


OBSERVED = "2026-11-13T00:00:00+00:00"


def task_schema() -> dict[str, object]:
    """행에서 사용한 상태와 아직 사용하지 않은 원본 선택값을 제공한다."""
    return {
        "object": "data_source",
        "properties": {
            "Status": {
                "id": "s",
                "type": "status",
                "status": {"options": [{"name": "진행 중"}, {"name": "미사용"}]},
            }
        },
    }


def page() -> dict[str, object]:
    """개인 자료 없는 Notion 응답을 구성한다."""
    return {
        "object": "page",
        "id": "fake-page",
        "url": "https://notion.so/fake",
        "last_edited_time": OBSERVED,
        "properties": {
            "Title": {"id": "t", "type": "title", "title": [{"plain_text": "가상 업무"}]},
            "People": {"id": "p", "type": "people", "people": [{"id": "me"}]},
            "Status": {"id": "s", "type": "status", "status": {"name": "진행 중"}},
            "Date": {
                "id": "d",
                "type": "date",
                "date": {"start": "2026-11-13T09:00:00", "end": None, "time_zone": "Asia/Seoul"},
            },
        },
    }


def truncated_page() -> dict[str, object]:
    """속성별 추가 조회가 필요한 참조 제목과 25명 담당자를 제공한다."""
    raw = page()
    props = raw["properties"]
    assert isinstance(props, dict)
    props["Title"] = {
        "id": "t",
        "type": "title",
        "title": [
            {
                "type": "mention",
                "plain_text": "생략된 참조",
                "mention": {"type": "page", "page": {"id": "fake-reference"}},
            }
        ],
    }
    props["People"] = {
        "id": "p",
        "type": "people",
        "people": [{"id": f"fake-{i}"} for i in range(25)],
    }
    return raw
