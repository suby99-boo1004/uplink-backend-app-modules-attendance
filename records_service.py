from __future__ import annotations

import datetime as dt
from typing import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.work_session import WorkSession


def upsert_attendance_records_for_users(
    *,
    db: Session,
    user_ids: Sequence[int],
    basis_dates: Iterable[dt.date],
    actor_user_id: int | None = None,
) -> int:
    """work_sessions를 기반으로 attendance_records(일자 확정)를 갱신.

    - 유니크 키: (user_id, work_date_basis)
    - check_in_at: 해당 일자 귀속의 최소 start_at
    - check_out_at: 해당 일자 귀속의 최대 end_at(없으면 NULL)
    - shift_type: NIGHT 세션이 1개라도 있으면 NIGHT, 아니면 DAY
    - is_holiday_work: 세션 중 is_holiday=True가 1개라도 있으면 True

    ⚠️ status는 DB 기본값(WORKING)을 사용하고, 여기서는 건드리지 않습니다.
    """

    basis_list = list(dict.fromkeys([b for b in basis_dates if b is not None]))
    if not user_ids or not basis_list:
        return 0

    # 1) 대상 work_sessions를 SQL로 집계 (python loop 최소화)
    #    work_date는 우선 work_date_basis와 동일하게 두고, 추후 정책 확정 시 분리 가능
    agg_sql = text(
        """
        SELECT
          user_id,
          work_date_basis,
          MIN(start_at) AS min_start_at,
          MAX(end_at)   AS max_end_at,
          BOOL_OR(is_holiday) AS any_holiday,
          MAX(CASE WHEN shift_type = 'NIGHT' THEN 1 ELSE 0 END) AS has_night
        FROM work_sessions
        WHERE user_id = ANY(:user_ids)
          AND work_date_basis = ANY(:basis_dates)
        GROUP BY user_id, work_date_basis
        """
    )

    rows = db.execute(
        agg_sql,
        {
            "user_ids": list(user_ids),
            "basis_dates": basis_list,
        },
    ).mappings().all()

    if not rows:
        return 0

    # 2) upsert
    upsert_sql = text(
        """
        INSERT INTO attendance_records (
          user_id, work_date, work_date_basis,
          check_in_at, check_out_at,
          shift_type, is_holiday_work,
          created_by, updated_by
        )
        VALUES (
          :user_id, :work_date, :work_date_basis,
          :check_in_at, :check_out_at,
          :shift_type, :is_holiday_work,
          :created_by, :updated_by
        )
        ON CONFLICT (user_id, work_date_basis)
        DO UPDATE SET
          work_date = EXCLUDED.work_date,
          check_in_at = EXCLUDED.check_in_at,
          check_out_at = EXCLUDED.check_out_at,
          shift_type = EXCLUDED.shift_type,
          is_holiday_work = EXCLUDED.is_holiday_work,
          updated_by = EXCLUDED.updated_by,
          updated_at = NOW()
        """
    )

    updated = 0
    for r in rows:
        user_id = int(r["user_id"])
        basis = r["work_date_basis"]
        check_in_at = r["min_start_at"]
        check_out_at = r["max_end_at"]
        is_holiday_work = bool(r["any_holiday"])
        shift_type = "NIGHT" if int(r["has_night"] or 0) == 1 else "DAY"

        db.execute(
            upsert_sql,
            {
                "user_id": user_id,
                "work_date": basis,
                "work_date_basis": basis,
                "check_in_at": check_in_at,
                "check_out_at": check_out_at,
                "shift_type": shift_type,
                "is_holiday_work": is_holiday_work,
                "created_by": actor_user_id,
                "updated_by": actor_user_id,
            },
        )
        updated += 1

    return updated
