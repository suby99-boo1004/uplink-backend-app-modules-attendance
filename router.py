from __future__ import annotations

"""Attendance module router.

경로 이중 등록:
- /today/status
- /api/attendance/today/status
(프로젝트 include_router 방식 차이 흡수)
"""

import importlib
from datetime import datetime, date, time as dtime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.work_session import WorkSession
from app.models.user import User
from app.modules.attendance.service import summarize_today

router = APIRouter(tags=["Attendance"])
KST = ZoneInfo("Asia/Seoul")

INTERNAL_ROLE_IDS = (6, 7, 8)  # 관리자/운영자/회사직원


# -----------------------------------------------------------------------------
# Auth dependency (defensive load)
# -----------------------------------------------------------------------------
def _missing_auth_dependency(*_args, **_kwargs):
    raise HTTPException(
        status_code=401,
        detail=(
            "Auth dependency(get_current_user)를 찾지 못했습니다. "
            "app.core.auth / app.core.security / app.core.deps 중 하나에 get_current_user를 제공해야 합니다."
        ),
    )


def _load_get_current_user():
    for mod_name in ("app.core.auth", "app.core.security", "app.core.deps"):
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        fn = getattr(mod, "get_current_user", None)
        if callable(fn):
            return fn
    return _missing_auth_dependency


get_current_user = _load_get_current_user()


@router.get("/today/status")
@router.get("/api/attendance/today/status")
def today_status(
    include_all: bool = Query(False, description="true면 오늘 세션이 없는 직원도 status=NONE으로 포함"),
    db: Session = Depends(get_db),
):
    today_kst = datetime.now(KST).date()
    internal_ids = _get_internal_user_id_set(db)
    rows = summarize_today(db, today_kst, include_all=include_all)
    # summarize_today 반환 형태(dict/obj) 모두 대응
    filtered = []
    for it in (rows or []):
        uid = None
        if isinstance(it, dict):
            uid = it.get("user_id")
        else:
            uid = getattr(it, "user_id", None)
        if isinstance(uid, int) and (not internal_ids or uid in internal_ids):
            filtered.append(it)
    return filtered


# -----------------------------------------------------------------------------

def _get_internal_user_id_set(db: Session) -> set[int]:
    """내부 인원(관리자/운영자/회사직원) user_id 집합."""
    try:
        q = db.query(User.id).filter(User.role_id.in_(INTERNAL_ROLE_IDS))
        return {int(r[0]) for r in q.all()}
    except Exception:
        # role_id 컬럼이 없거나 예외 발생 시: 안전하게 전체 사용자로 폴백(단, 프론트에서도 2차 필터링)
        try:
            q = db.query(User.id)
            return {int(r[0]) for r in q.all()}
        except Exception:
            return set()
# Helpers
# -----------------------------------------------------------------------------
def _pick_last_meta_for_day(db: Session, user_id: int, work_date: date):
    q = db.query(WorkSession).filter(WorkSession.user_id == user_id)
    if hasattr(WorkSession, "work_date_basis"):
        q = q.filter(WorkSession.work_date_basis == work_date)
    last = q.order_by(WorkSession.start_at.desc()).first()
    shift_type = getattr(last, "shift_type", None) if last else None
    is_holiday = bool(getattr(last, "is_holiday", False)) if last else False
    return shift_type or "DAY", is_holiday


def _close_open_sessions(db: Session, user_id: int, work_date: date, at_kst: datetime):
    # NOTE:
    # - 반차/조퇴 '확인'은 즉시 확정이므로, end_at=None 세션이 있으면 종료시각을 at_kst로 고정.
    # - work_date_basis가 어긋나는(야간/자정넘김) 케이스를 흡수하기 위해, user_id 기준으로 열린 세션을 넓게 잡고,
    #   그 중 work_date와 연관된 세션만 닫는다.
    open_q = db.query(WorkSession).filter(
        WorkSession.user_id == user_id,
        WorkSession.end_at.is_(None),
    )

    # 가능한 경우 work_date_basis로 1차 제한
    if hasattr(WorkSession, "work_date_basis"):
        open_q2 = open_q.filter(WorkSession.work_date_basis == work_date)
        rows = open_q2.all()
    else:
        rows = open_q.all()

    # work_date_basis로 못 잡힌 케이스(자정 넘김 등)를 보강:
    # start_at의 KST 날짜가 work_date인 열린 세션도 포함
    if not rows:
        day_start = datetime.combine(work_date, dtime.min, tzinfo=KST)
        next_day = day_start + timedelta(days=1)
        rows = (
            open_q.filter(
                WorkSession.start_at >= day_start,
                WorkSession.start_at < next_day,
            )
            .all()
        )

    for s in rows:
        s.end_at = at_kst


# -----------------------------------------------------------------------------
# 조퇴 (EARLY_LEAVE)
# -----------------------------------------------------------------------------
class EarlyLeaveBulkRequest(BaseModel):
    user_ids: List[int] = Field(..., description="대상 사용자 ID 목록")
    time_hm: Optional[str] = Field(default=None, description="조퇴 시간(HH:MM, KST)")
    at: Optional[datetime] = Field(default=None, description="조퇴 시각(ISO datetime) - 호환용")
    reason: Optional[str] = Field(default=None, description="사유(선택)")

    @field_validator("user_ids")
    @classmethod
    def _ids(cls, v):
        ids: List[int] = []
        seen = set()
        for x in v:
            xi = int(x)
            if xi <= 0 or xi in seen:
                continue
            seen.add(xi)
            ids.append(xi)
        if not ids:
            raise ValueError("user_ids가 비어있습니다.")
        return ids

    @field_validator("time_hm")
    @classmethod
    def _time_hm(cls, v):
        if v is None:
            return v
        s = (v or "").strip()
        if not s:
            return None
        try:
            hh, mm = s.split(":")
            hh_i = int(hh)
            mm_i = int(mm)
            if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
                raise ValueError
        except Exception:
            raise ValueError("time_hm 형식은 HH:MM 이어야 합니다.")
        return f"{hh_i:02d}:{mm_i:02d}"


@router.post("/early-leave/bulk")
@router.post("/api/attendance/early-leave/bulk")
def early_leave_bulk(body: EarlyLeaveBulkRequest, db: Session = Depends(get_db)):
    today = datetime.now(KST).date()

    if body.time_hm:
        hh, mm = body.time_hm.split(":")
        at_kst = datetime.combine(today, dtime(int(hh), int(mm)), tzinfo=KST)
    elif body.at:
        at = body.at if body.at.tzinfo else body.at.replace(tzinfo=KST)
        at_kst = datetime.combine(today, dtime(at.hour, at.minute), tzinfo=KST)
    else:
        raise HTTPException(status_code=422, detail="time_hm 또는 at 중 하나는 필수입니다.")

    created = 0
    for uid in body.user_ids:
        _close_open_sessions(db, uid, today, at_kst)

        shift_type, is_holiday = _pick_last_meta_for_day(db, uid, today)
        marker = WorkSession(
            user_id=uid,
            session_type="EARLY_LEAVE",
            shift_type=shift_type,
            start_at=at_kst,
            end_at=at_kst,
            place=None,
            task=body.reason,
            is_holiday=is_holiday,
        )
        if hasattr(marker, "work_date_basis"):
            setattr(marker, "work_date_basis", today)

        db.add(marker)
        created += 1

    db.commit()
    return {"created": created}


# -----------------------------------------------------------------------------
# 반차 (HALF_LEAVE)
# -----------------------------------------------------------------------------
class HalfLeaveBulkRequest(BaseModel):
    user_ids: List[int] = Field(..., description="대상 사용자 ID 목록")
    work_date: date = Field(..., description="반차 적용 날짜(YYYY-MM-DD)")
    time_hm: str = Field(..., description="반차 퇴근 시각(HH:MM, KST)")
    reason: Optional[str] = Field(default=None, description="사유(선택)")

    @field_validator("user_ids")
    @classmethod
    def _ids(cls, v):
        ids: List[int] = []
        seen = set()
        for x in v:
            xi = int(x)
            if xi <= 0 or xi in seen:
                continue
            seen.add(xi)
            ids.append(xi)
        if not ids:
            raise ValueError("user_ids가 비어있습니다.")
        return ids

    @field_validator("time_hm")
    @classmethod
    def _time_hm(cls, v):
        s = (v or "").strip()
        try:
            hh, mm = s.split(":")
            hh_i = int(hh)
            mm_i = int(mm)
            if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
                raise ValueError
        except Exception:
            raise ValueError("time_hm 형식은 HH:MM 이어야 합니다.")
        return f"{hh_i:02d}:{mm_i:02d}"


@router.post("/half-leave/bulk")
@router.post("/api/attendance/half-leave/bulk")
def half_leave_bulk(body: HalfLeaveBulkRequest, db: Session = Depends(get_db)):
    hh, mm = body.time_hm.split(":")
    at_kst = datetime.combine(body.work_date, dtime(int(hh), int(mm)), tzinfo=KST)

    created = 0
    for uid in body.user_ids:
        # 확인 누르는 순간 확정: 열린 세션이 있으면 종료시각을 at_kst로 고정
        _close_open_sessions(db, uid, body.work_date, at_kst)

        shift_type, is_holiday = _pick_last_meta_for_day(db, uid, body.work_date)
        marker = WorkSession(
            user_id=uid,
            session_type="HALF_LEAVE",
            shift_type=shift_type,
            start_at=at_kst,
            end_at=at_kst,
            place=None,
            task=body.reason,
            is_holiday=is_holiday,
        )
        if hasattr(marker, "work_date_basis"):
            setattr(marker, "work_date_basis", body.work_date)

        db.add(marker)
        created += 1

    db.commit()
    return {"created": created}


# -----------------------------------------------------------------------------
# 관리자 리포트(검색) - 출퇴근기록 검색 버튼용
# - 프론트 AttendanceReportPage.tsx가 호출하는 엔드포인트를 이 모듈에서 제공
#   * /api/admin/attendance/details
#   * /api/admin/attendance/day/sessions
# -----------------------------------------------------------------------------

class _AdminDetailsDay(BaseModel):
    work_date: date
    shift_type: Optional[str] = None
    is_holiday: bool = False
    work_minutes: int = 0
    work_hours: float = 0.0
    session_types: List[str] = Field(default_factory=list)
    places: List[str] = Field(default_factory=list)
    tasks: List[str] = Field(default_factory=list)
    first_start_at: Optional[datetime] = None
    last_end_at: Optional[datetime] = None


class _AdminDetailsOut(BaseModel):
    user_id: int
    user_name: str
    start_date: date
    end_date: date
    days: List[_AdminDetailsDay]


class _AdminDaySessionsItem(BaseModel):
    session_type: Optional[str] = None
    shift_type: Optional[str] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    effective_end_at: Optional[datetime] = None
    work_minutes: int = 0
    place: Optional[str] = None
    task: Optional[str] = None


class _AdminDaySessionsOut(BaseModel):
    sessions: List[_AdminDaySessionsItem] = Field(default_factory=list)
    first_start_at: Optional[datetime] = None
    last_end_at: Optional[datetime] = None


def _is_admin_user(user: User) -> bool:
    try:
        if getattr(user, "role_id", None) == 6:
            return True
    except Exception:
        pass
    code = str(getattr(user, "role_code", "") or "").strip().upper()
    return code == "ADMIN"


def _require_admin_user(user: Optional[User]) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    if not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return user


def _to_work_date_basis(ws: WorkSession) -> date:
    # work_date_basis가 있으면 우선 사용, 없으면 start_at의 KST 날짜 사용
    if hasattr(ws, "work_date_basis"):
        v = getattr(ws, "work_date_basis", None)
        if isinstance(v, date):
            return v
    sa = getattr(ws, "start_at", None)
    if isinstance(sa, datetime):
        sa_kst = sa.astimezone(KST) if sa.tzinfo else sa.replace(tzinfo=KST)
        return sa_kst.date()
    return datetime.now(KST).date()


def _effective_end(ws: WorkSession) -> datetime:
    # end_at이 없으면 start_at로 인정(안전)
    sa = getattr(ws, "start_at", None)
    ea = getattr(ws, "end_at", None)
    if isinstance(ea, datetime):
        return ea
    if isinstance(sa, datetime):
        return sa
    return datetime.now(KST)


def _work_minutes(ws: WorkSession) -> int:
    sa = getattr(ws, "start_at", None)
    ee = _effective_end(ws)
    if not isinstance(sa, datetime):
        return 0
    sa2 = sa.astimezone(KST) if sa.tzinfo else sa.replace(tzinfo=KST)
    ee2 = ee.astimezone(KST) if ee.tzinfo else ee.replace(tzinfo=KST)
    mins = int((ee2 - sa2).total_seconds() // 60)
    return max(0, mins)


@router.get("/api/admin/attendance/details", response_model=_AdminDetailsOut)
def admin_attendance_details(
    user_id: int = Query(..., description="조회 대상 사용자 ID"),
    start_date: date = Query(..., description="시작일(YYYY-MM-DD)"),
    end_date: date = Query(..., description="종료일(YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_user(current_user)

    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date는 start_date 이상이어야 합니다.")

    # 사용자 이름
    u = db.query(User).filter(User.id == int(user_id)).first()
    user_name = getattr(u, "name", None) or getattr(u, "username", None) or f"#{user_id}"

    # 기간 내 세션 조회 (work_date_basis 우선)
    q = db.query(WorkSession).filter(WorkSession.user_id == int(user_id))
    if hasattr(WorkSession, "work_date_basis"):
        q = q.filter(WorkSession.work_date_basis >= start_date, WorkSession.work_date_basis <= end_date)
    else:
        # start_at 기준 (KST date)
        start_dt = datetime.combine(start_date, dtime.min, tzinfo=KST)
        end_dt = datetime.combine(end_date + timedelta(days=1), dtime.min, tzinfo=KST)
        q = q.filter(WorkSession.start_at >= start_dt, WorkSession.start_at < end_dt)

    rows = q.order_by(WorkSession.start_at.asc()).all()

    by_day: dict[date, list[WorkSession]] = {}
    for ws in rows:
        wd = _to_work_date_basis(ws)
        if wd < start_date or wd > end_date:
            continue
        by_day.setdefault(wd, []).append(ws)

    out_days: List[_AdminDetailsDay] = []
    cur = start_date
    while cur <= end_date:
        items = by_day.get(cur, [])
        if not items:
            out_days.append(
                _AdminDetailsDay(
                    work_date=cur,
                    shift_type=None,
                    is_holiday=False,
                    work_minutes=0,
                    work_hours=0.0,
                    session_types=[],
                    places=[],
                    tasks=[],
                    first_start_at=None,
                    last_end_at=None,
                )
            )
            cur += timedelta(days=1)
            continue

        # shift_type / holiday는 마지막 세션의 값을 따름(기존 정책과 유사)
        last = items[-1]
        shift_type = str(getattr(last, "shift_type", None) or "") or None
        is_holiday = bool(getattr(last, "is_holiday", False))

        mins = sum(_work_minutes(ws) for ws in items)

        session_types = []
        places = []
        tasks = []
        first_start = None
        last_end = None
        for ws in items:
            st = getattr(ws, "session_type", None)
            if st:
                session_types.append(str(st))
            p = getattr(ws, "place", None)
            if p:
                places.append(str(p))
            t = getattr(ws, "task", None)
            if t:
                tasks.append(str(t))
            sa = getattr(ws, "start_at", None)
            if isinstance(sa, datetime):
                first_start = sa if first_start is None else min(first_start, sa)
            ea = getattr(ws, "end_at", None)
            if isinstance(ea, datetime):
                last_end = ea if last_end is None else max(last_end, ea)

        out_days.append(
            _AdminDetailsDay(
                work_date=cur,
                shift_type=shift_type,
                is_holiday=is_holiday,
                work_minutes=int(mins),
                work_hours=round((mins / 60.0), 2),
                session_types=session_types,
                places=places,
                tasks=tasks,
                first_start_at=first_start,
                last_end_at=last_end,
            )
        )
        cur += timedelta(days=1)

    return _AdminDetailsOut(
        user_id=int(user_id),
        user_name=str(user_name),
        start_date=start_date,
        end_date=end_date,
        days=out_days,
    )


@router.get("/api/admin/attendance/day/sessions", response_model=_AdminDaySessionsOut)
def admin_attendance_day_sessions(
    user_id: int = Query(..., description="조회 대상 사용자 ID"),
    work_date: date = Query(..., description="근무일(YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin_user(current_user)

    q = db.query(WorkSession).filter(WorkSession.user_id == int(user_id))
    if hasattr(WorkSession, "work_date_basis"):
        q = q.filter(WorkSession.work_date_basis == work_date)
    else:
        day_start = datetime.combine(work_date, dtime.min, tzinfo=KST)
        next_day = day_start + timedelta(days=1)
        q = q.filter(WorkSession.start_at >= day_start, WorkSession.start_at < next_day)

    rows = q.order_by(WorkSession.start_at.asc()).all()

    sessions: List[_AdminDaySessionsItem] = []
    first_start = None
    last_end = None
    for ws in rows:
        sa = getattr(ws, "start_at", None)
        ea = getattr(ws, "end_at", None)
        ee = _effective_end(ws)
        mins = _work_minutes(ws)

        if isinstance(sa, datetime):
            first_start = sa if first_start is None else min(first_start, sa)
        if isinstance(ea, datetime):
            last_end = ea if last_end is None else max(last_end, ea)

        sessions.append(
            _AdminDaySessionsItem(
                session_type=(str(getattr(ws, "session_type", None)) if getattr(ws, "session_type", None) is not None else None),
                shift_type=(str(getattr(ws, "shift_type", None)) if getattr(ws, "shift_type", None) is not None else None),
                start_at=sa if isinstance(sa, datetime) else None,
                end_at=ea if isinstance(ea, datetime) else None,
                effective_end_at=ee,
                work_minutes=int(mins),
                place=(str(getattr(ws, "place", None)) if getattr(ws, "place", None) is not None else None),
                task=(str(getattr(ws, "task", None)) if getattr(ws, "task", None) is not None else None),
            )
        )

    return _AdminDaySessionsOut(sessions=sessions, first_start_at=first_start, last_end_at=last_end)
