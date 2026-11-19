"""복원할 사본에서 과거 발송 여부가 불분명한 알림을 마감한다."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.infrastructure.sqlite.notification_deliveries import finish_deliveries


def prepare_restored_notifications(database: Path) -> None:
    """이미 도래하거나 배정된 알림의 재발송을 막고 미래의 미배정 예약을 보존한다."""
    now = time.time()
    with (
        closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as connection,
        connection,
    ):
        # 배정된 채널은 백업 이후 실제 발송됐을 수 있다. 결과를 모르는 채널만 마감한다.
        connection.execute(
            """UPDATE notification_deliveries SET state='unknown',revoked=1,token=NULL
            WHERE state IN ('pending','delivering')"""
        )
        finish_deliveries(connection, now)
        # 채널 배정 전의 과거 예약과 이전 형식의 획득된 실행권도 다시 요청할 수 없다.
        connection.execute(
            """UPDATE notification_jobs SET state='unknown',token=NULL
            WHERE (state='pending' AND due_at<=?) OR state='delivering'
                OR (state='obsolete' AND token IS NOT NULL)""",
            (now,),
        )
