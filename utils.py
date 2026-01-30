from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def _to_kst(dt: datetime) -> datetime:
    # DB(timestamptz)는 tz-aware로 들어오는 것이 정상. 혹시 naive면 KST로 간주.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def calc_work_date_basis(start_at: datetime, end_at: datetime | None, shift_type: str):
    """근무 귀속일(work_date_basis) 계산 (KST 기준).

    - DAY  : start_at의 날짜
    - NIGHT: end_at이 있으면 end_at의 날짜, 없으면 start_at+1일의 날짜
    """
    start_kst = _to_kst(start_at)

    if shift_type == "NIGHT":
        if end_at:
            return _to_kst(end_at).date()
        return (start_kst + timedelta(days=1)).date()

    return start_kst.date()
