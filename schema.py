from __future__ import annotations
from datetime import datetime, date
from enum import Enum
from pydantic import BaseModel, Field


class ShiftType(str, Enum):
    DAY = "DAY"      # 주간
    NIGHT = "NIGHT"  # 야간(다음날 근무로 귀속)


class CheckInRequest(BaseModel):
    shift_type: ShiftType = Field(default=ShiftType.DAY, description="주간/야간")
    is_holiday_work: bool = Field(default=False, description="휴일/주말 근무 여부")
    note: str | None = Field(default=None, description="메모(선택)")


class CheckOutRequest(BaseModel):
    note: str | None = Field(default=None, description="메모(선택)")


class AttendanceRecord(BaseModel):
    id: int
    user_id: int
    work_date_basis: date
    shift_type: ShiftType
    check_in_at: datetime | None
    check_out_at: datetime | None
    is_holiday_work: bool
    worked_minutes: int
    worked_hours: float
    status_label: str  # 정상/야근/미출근 등
    note: str | None = None


class TodayStatusItem(BaseModel):
    user_id: int
    user_name: str
    department_id: int | None = None
    work_date_basis: date
    shift_type: ShiftType | None = None
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    worked_minutes: int = 0
    status_label: str = "미출근"
    is_holiday_work: bool = False
