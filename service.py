from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func

from app.models.work_session import WorkSession
from app.models.user import User
from app.modules.attendance.utils import calc_work_date_basis

KST = ZoneInfo("Asia/Seoul")

DAY_END = time(18, 30)   # (참고) 과거 미퇴근(DAY) 추정 종료시간
NIGHT_END = time(6, 0)   # (참고) 과거 미퇴근(NIGHT) 추정 종료시간


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt


def _effective_end_at(session: WorkSession, now_kst: datetime) -> datetime:
    """근무시간 계산용 종료시각

    - 종료(end_at)가 있으면 그 값을 사용
    - 미퇴근(end_at is None)이면 "현재시각"을 사용

    NOTE: 과거에는 미퇴근을 고정 종료시간(주간 18:30, 야간 06:00)으로 추정했으나,
          대표님 요구사항(출근 직후 근무시간 표기가 이상함)을 위해 현재시간 기준으로 변경.
    """
    if session.end_at:
        return _ensure_aware(session.end_at).astimezone(KST)

    # 미퇴근: 현재시간(=now_kst)
    return now_kst


def summarize_today(db: Session, target_date: date, include_all: bool = False):
    """오늘 기준(=work_date_basis)으로 직원별 상태 요약

    반환 필드(프론트용):
      - user_id
      - status: NONE / OFFICE / OUTSIDE / TRIP_VIRTUAL / LEAVE
      - shift_type: DAY / NIGHT (해당 status의 세션 기준, 없으면 None)
      - is_working: 진행중 세션 존재 여부
      - place / task
      - worked_minutes: (세션 합산) 진행중이면 현재시각까지 누적
      - session_count: (세션 건수) 휴가(월차/반차) 제외
      - is_overtime: worked_minutes >= 720 (12h)  ※ 기존 로직 유지
      - is_holiday
    """

    # 오늘 현황에는 "오늘 귀속(work_date_basis=오늘)" 세션이 기본.
    # 추가 요구사항:
    #   - 주간(DAY) 퇴근한 뒤, 같은 날 야간(NIGHT) 출근하면
    #     야간은 다음날 귀속이지만, '오늘 현황'에도 진행중 세션으로 보여야 함.
    # → 오늘 KST 날짜에 시작했고 아직 미퇴근인 야간 세션은 오늘 조회에 포함.
    now_kst = datetime.now(KST)
    # (추가) 연/누적 휴가(월차/반차) 사용량 집계
    year_start = date(target_date.year, 1, 1)
    leave_rows = (
        db.query(
            WorkSession.user_id.label('user_id'),
            WorkSession.session_type.label('session_type'),
            func.count(WorkSession.id).label('cnt'),
        )
        .filter(
            WorkSession.work_date_basis >= year_start,
            WorkSession.work_date_basis <= target_date,
            WorkSession.session_type.in_(['LEAVE', 'HALF_LEAVE']),
        )
        .group_by(WorkSession.user_id, WorkSession.session_type)
        .all()
    )
    leave_agg = defaultdict(lambda: {'leave': 0, 'half_leave': 0})
    for r in leave_rows:
        if r.session_type == 'LEAVE':
            leave_agg[r.user_id]['leave'] = int(r.cnt or 0)
        elif r.session_type == 'HALF_LEAVE':
            leave_agg[r.user_id]['half_leave'] = int(r.cnt or 0)

    start_kst = datetime.combine(target_date, time.min, tzinfo=KST)
    next_kst = start_kst + timedelta(days=1)

    sessions = (
        db.query(WorkSession)
        .filter(
            or_(
                # 기본: 오늘 귀속(work_date_basis=오늘)
                WorkSession.work_date_basis == target_date,
                # 자정 이후 표시 보강:
                # 전날(또는 오늘) 시작했지만 아직 미퇴근(end_at=None)인 세션은
                # 오늘 현황에서 "근무중"으로 계속 보여줘야 함.
                # 너무 과거 데이터까지 끌고오지 않도록 최근 24시간 범위로 제한.
                and_(
                    WorkSession.end_at.is_(None),
                    WorkSession.start_at < next_kst,
                    WorkSession.start_at >= (start_kst - timedelta(days=1)),
                ),
            )
        )
        .order_by(WorkSession.user_id.asc(), WorkSession.start_at.asc())
        .all()
    )

    # include_all=true면 오늘 세션이 없어도 모든 유저를 포함
    user_map = {}
    if include_all:
        for u in db.query(User).order_by(User.id.asc()).all():
            user_map[u.id] = {
                "user_id": u.id,
                "user_name": u.name,
                "status": "NONE",
                "shift_type": None,
                "is_working": False,
                "place": None,
                "task": None,
                "start_at": None,
                "end_at": None,
                "worked_minutes": 0,
                "session_count": 0,
                "is_overtime": False,
                "is_holiday": False,
            }

    # user_id -> list[WorkSession]
    by_user: dict[int, list[WorkSession]] = {}
    for s in sessions:
        by_user.setdefault(s.user_id, []).append(s)

    # 세션에 포함된 유저 이름 맵
    name_map: dict[int, str] = {}
    if by_user:
        for u in db.query(User).filter(User.id.in_(list(by_user.keys()))).all():
            name_map[u.id] = u.name

    for user_id, ss in by_user.items():
        # ------------------------------------------------------------------
        # 핵심 규칙(대표님 고정): 야간(NIGHT)은 "다음날 근무"로 귀속
        #
        # today/status는 화면상 "오늘 현황"이므로,
        # - 표시(근무중 여부/세션 목록)는 열린 세션을 포함해 보여줄 수 있음
        # - 하지만 근무시간 합산/출근·퇴근시간/건수 집계는
        #   반드시 (work_date_basis == target_date) 기준으로만 계산해야
        #   야간 근무가 전날에 잘못 누적되는 문제가 사라짐.
        # ------------------------------------------------------------------
        ss_for_day: list[WorkSession] = []
        ss_extra: list[WorkSession] = []
        for s in ss:
            # DB에 work_date_basis가 있으면 그 값을 우선 사용하고,
            # 혹시 누락/불일치가 있으면 계산값으로 보강.
            basis = getattr(s, "work_date_basis", None)
            if basis is None:
                basis = calc_work_date_basis(s.start_at, s.end_at, getattr(s, "shift_type", "DAY"))

            if basis == target_date:
                ss_for_day.append(s)
            else:
                ss_extra.append(s)
        total_minutes = 0
        is_holiday = False

        # 세션 상세(시간대) - 휴가(월차/반차) 포함
        sessions_detail = []

        # 휴가/조퇴/반차 마커 제외한 '근무 세션' 건수 (오늘 귀속만)
        work_session_count = len(
            [
                s
                for s in ss_for_day
                if (s.session_type or "") not in ("LEAVE", "HALF_LEAVE", "EARLY_LEAVE")
            ]
        )

        # 대표 세션(rep) 선정 우선순위 (즉시 확정 로직)
        # 1) LEAVE(월차) 2) HALF_LEAVE(반차) 3) EARLY_LEAVE(조퇴)
        # 4) 진행중(end_at=None) 5) 마지막 세션
        def _last_of(stypes: set[str]):
            cand = [x for x in ss_for_day if (x.session_type or "") in stypes]
            if not cand:
                return None
            # ss는 start_at ASC 정렬되어 있으므로 마지막이 최신
            return cand[-1]

        rep_leave = _last_of({"LEAVE"})
        rep_half = _last_of({"HALF_LEAVE"})
        rep_early = _last_of({"EARLY_LEAVE"})

        if rep_leave is not None:
            rep = rep_leave
            is_working = False
        elif rep_half is not None:
            rep = rep_half
            is_working = False
        elif rep_early is not None:
            rep = rep_early
            is_working = False
        else:
            # "오늘 귀속" 열린 세션 우선
            open_sessions_today = [x for x in ss_for_day if x.end_at is None]
            if open_sessions_today:
                rep = open_sessions_today[-1]
                is_working = True
            else:
                # (표시 보강) 오늘 귀속 세션이 없지만 열린 세션이 있다면
                # 야간(다음날 귀속) 근무중일 수 있으므로 대표세션으로 사용.
                open_sessions_any = [x for x in ss if x.end_at is None]
                if open_sessions_any:
                    rep = open_sessions_any[-1]
                    is_working = True
                else:
                    rep = (ss_for_day[-1] if ss_for_day else ss[-1])
                    is_working = False

        # 합산 근무시간: "오늘 귀속"만 누적 (야간 다음날 귀속 규칙 반영)
        for s in ss_for_day:
            stype = (s.session_type or '')

            # 월차는 시간 개념이 없음 (표시도 제외)
            if stype == 'LEAVE':
                sessions_detail.append({
                    'session_type': s.session_type,
                    'shift_type': s.shift_type,
                    'is_holiday': bool(s.is_holiday),
                    'place': s.place,
                    'task': s.task,
                    'start_at': None,
                    'end_at': None,
                })
                continue

            # 반차/조퇴는 '확정 이벤트'이므로 시간은 표시하되, 근무시간 합산에는 포함하지 않음
            if stype in ('HALF_LEAVE', 'EARLY_LEAVE'):
                start_kst_dt = _ensure_aware(s.start_at).astimezone(KST) if s.start_at else None
                end_kst_dt = _ensure_aware(s.end_at).astimezone(KST) if s.end_at else start_kst_dt
                sessions_detail.append({
                    'session_type': s.session_type,
                    'shift_type': s.shift_type,
                    'is_holiday': bool(s.is_holiday),
                    'place': s.place,
                    'task': s.task,
                    'start_at': start_kst_dt.isoformat() if start_kst_dt else None,
                    'end_at': end_kst_dt.isoformat() if end_kst_dt else None,
                })
                continue
            start_at = _ensure_aware(s.start_at)
            start_kst_dt = start_at.astimezone(KST)
            end_kst_dt = _effective_end_at(s, now_kst)

            # UI 표시용(펼침/마우스오버) 시간대 목록
            sessions_detail.append(
                {
                    "session_type": s.session_type,
                    "shift_type": s.shift_type,
                    "is_holiday": bool(s.is_holiday),
                    "place": s.place,
                    "task": s.task,
                    "start_at": start_kst_dt.isoformat(),
                    "end_at": (
                        _ensure_aware(s.end_at).astimezone(KST).isoformat() if s.end_at else None
                    ),
                }
            )

            diff = int((end_kst_dt - start_kst_dt).total_seconds() // 60)
            if diff > 0:
                total_minutes += diff
            if s.is_holiday:
                is_holiday = True

        # 세션 상세 표시: 오늘 귀속 세션 + (표시용) 추가 세션(열린 야간 등)
        # - extra는 근무시간 합산에는 포함하지 않음
        for s in ss_extra:
            stype = (s.session_type or '')
            if stype == 'LEAVE':
                sessions_detail.append({
                    'session_type': s.session_type,
                    'shift_type': s.shift_type,
                    'is_holiday': bool(s.is_holiday),
                    'place': s.place,
                    'task': s.task,
                    'start_at': None,
                    'end_at': None,
                })
                continue
            start_kst_dt = _ensure_aware(s.start_at).astimezone(KST) if s.start_at else None
            sessions_detail.append(
                {
                    "session_type": s.session_type,
                    "shift_type": s.shift_type,
                    "is_holiday": bool(s.is_holiday),
                    "place": s.place,
                    "task": s.task,
                    "start_at": start_kst_dt.isoformat() if start_kst_dt else None,
                    "end_at": (
                        _ensure_aware(s.end_at).astimezone(KST).isoformat() if s.end_at else None
                    ),
                }
            )

        # 대표 상태 구성
        status = rep.session_type or "NONE"
        shift_type = rep.shift_type
        place = rep.place
        task = rep.task

        # 오늘 현황 표기용 출근/퇴근 시간
        # 요구사항: 사무실 근무중(OFFICE) 상태에서 외근(OUTSIDE)로 전환해도 "출근시간"은 바뀌면 안 됨.
        # 따라서 "대표 상태(rep)"의 start_at이 아니라,
        #   - 당일 비휴가 세션(OFFICE/OUTSIDE/TRIP_VIRTUAL)의 최소 start_at을 "출근시간"으로 사용
        #   - 당일 세션 중 미퇴근이 1개라도 있으면 "퇴근시간"은 None
        #   - 모두 퇴근했다면 최대 end_at을 "퇴근시간"으로 사용

        if status == 'LEAVE':
            check_in_dt = None
            check_out_iso = None
        elif status in ('HALF_LEAVE', 'EARLY_LEAVE'):
            non_marker_sessions = [s for s in ss if (s.session_type or "") not in ("LEAVE", "HALF_LEAVE", "EARLY_LEAVE")]
            start_candidates = non_marker_sessions or ss
            start_values = [_ensure_aware(s.start_at).astimezone(KST) for s in start_candidates if s.start_at]
            check_in_dt = min(start_values) if start_values else None
            marker_dt = None
            if rep is not None and (rep.end_at or rep.start_at):
                marker_dt = _ensure_aware(rep.end_at or rep.start_at).astimezone(KST)
            check_out_iso = marker_dt.isoformat() if marker_dt else None
        else:
            non_leave_sessions = [s for s in ss_for_day if (s.session_type or "") not in ("LEAVE", "HALF_LEAVE", "EARLY_LEAVE")]
            start_candidates = non_leave_sessions or ss
            start_values = [_ensure_aware(s.start_at).astimezone(KST) for s in start_candidates if s.start_at]
            check_in_dt = min(start_values) if start_values else None

            if any(s.end_at is None for s in ss_for_day):
                check_out_iso = None
            else:
                end_candidates = [s for s in ss_for_day if s.end_at]
                check_out_iso = (
                    max(_ensure_aware(s.end_at).astimezone(KST) for s in end_candidates).isoformat()
                    if end_candidates
                    else None
                )

        check_in_iso = check_in_dt.isoformat() if check_in_dt else None

        # 연/누적 휴가 사용량(월차=1, 반차=0.5)
        leave_used = leave_agg[user_id]['leave']
        half_leave_used = leave_agg[user_id]['half_leave']
        leave_days_used = float(leave_used) + (float(half_leave_used) * 0.5)

        user_map[user_id] = {
            "user_id": user_id,
            "user_name": name_map.get(user_id),
            "status": status,
            "shift_type": shift_type,
            "is_working": is_working,
            "place": place,
            "task": task,
            "start_at": check_in_iso,
            "end_at": check_out_iso,
            "worked_minutes": total_minutes,
            "session_count": work_session_count,
            "leave_used": leave_used,
            "half_leave_used": half_leave_used,
            "leave_days_used": leave_days_used,
            "sessions": sessions_detail,
            "is_overtime": total_minutes >= 720,
            "is_holiday": is_holiday,
        }

    return list(user_map.values())
