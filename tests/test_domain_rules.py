"""
Unit tests for domain calculation rules (Rules R1 - R8).
Verifies mathematical precision, edge cases, half-up rounding, and overnight shifts.
"""
from datetime import datetime, timezone
import pytest

from app.main import (
    determine_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_half_day,
    compute_overtime_minutes,
    count_working_days,
    round_half_up,
    truncate_to_seconds,
)


def test_truncate_to_seconds():
    # 09:40:00.900 ms -> 09:40:00.000 ms
    raw_ms = 1783312800900
    expected = 1783312800000
    assert truncate_to_seconds(raw_ms) == expected


def test_round_half_up():
    # Standard half-up rounding vs banker's rounding
    assert round_half_up(2.525, 2) == 2.53
    assert round_half_up(2.524, 2) == 2.52
    assert round_half_up(2.526, 2) == 2.53
    assert round_half_up(9.116666, 2) == 9.12
    assert round_half_up(None) is None
    # 4 decimals for rates
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234


def test_determine_attendance_date_normal_shift():
    # Normal shift 09:30 - 18:30 IST
    # 2026-07-06 09:28:00 IST = 2026-07-06 03:58:00 UTC
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    assert determine_attendance_date(ms, "09:30", "18:30") == "2026-07-06"


def test_determine_attendance_date_overnight_shift():
    # Overnight shift 22:00 - 06:00 IST
    # Case 1: Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    dt1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    ms1 = int(dt1.timestamp() * 1000)
    assert determine_attendance_date(ms1, "22:00", "06:00") == "2026-07-06"

    # Case 2: Punch in at 01:15 IST on 2026-07-07 (19:45 UTC on 2026-07-06)
    # Earlier than shift_end 06:00 -> belongs to shift that started previous day (2026-07-06)
    dt2 = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    ms2 = int(dt2.timestamp() * 1000)
    assert determine_attendance_date(ms2, "22:00", "06:00") == "2026-07-06"

    # Case 3: Punch in at 22:05 IST on 2026-07-07 (after shift_end) -> belongs to 2026-07-07
    dt3 = datetime(2026, 7, 7, 16, 35, 0, tzinfo=timezone.utc)
    ms3 = int(dt3.timestamp() * 1000)
    assert determine_attendance_date(ms3, "22:00", "06:00") == "2026-07-07"


def test_rule_r2_late_minutes():
    # Shift 09:30 IST on 2026-07-06
    # 09:30:00 IST = 04:00:00 UTC
    # 09:40:00 IST = 04:10:00 UTC (10 mins elapsed -> exactly grace period -> 0 late)
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(int(dt_grace.timestamp() * 1000), "2026-07-06", "09:30") == 0

    # 09:40:01 IST = 04:10:01 UTC (strictly > 10 mins elapsed -> 10 minutes)
    dt_late1 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(int(dt_late1.timestamp() * 1000), "2026-07-06", "09:30") == 10

    # 10:15:59 IST = 04:45:59 UTC (45 min 59 sec -> 45 minutes)
    dt_late45 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(int(dt_late45.timestamp() * 1000), "2026-07-06", "09:30") == 45

    # Early punch-in 09:20:00 IST -> 0 late
    dt_early = datetime(2026, 7, 6, 3, 50, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(int(dt_early.timestamp() * 1000), "2026-07-06", "09:30") == 0


def test_rule_r3_overtime_minutes():
    # Shift 09:30 - 18:30 IST on 2026-07-06
    # 18:30 IST = 13:00 UTC
    # Case 1: Punch out 18:35 IST (5 mins after shift end -> under 30 min minimum -> 0)
    dt_ot5 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(int(dt_ot5.timestamp() * 1000), "2026-07-06", "09:30", "18:30") == 0

    # Case 2: Punch out 18:59 IST (29 mins after shift end -> under 30 min minimum -> 0)
    dt_ot29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(int(dt_ot29.timestamp() * 1000), "2026-07-06", "09:30", "18:30") == 0

    # Case 3: Punch out 19:00 IST (30 mins after shift end -> >= 30 -> 30)
    dt_ot30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(int(dt_ot30.timestamp() * 1000), "2026-07-06", "09:30", "18:30") == 30

    # Case 4: Punch out 19:10 IST (40 mins after shift end -> 40)
    dt_ot40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(int(dt_ot40.timestamp() * 1000), "2026-07-06", "09:30", "18:30") == 40


def test_overtime_overnight_shift():
    # Overnight shift 22:00 - 06:00 on 2026-07-06
    # Shift ends at 2026-07-07 06:00 IST (00:30 UTC)
    # Punch out at 2026-07-07 06:40 IST (01:10 UTC) -> 40 min overtime
    dt_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(int(dt_out.timestamp() * 1000), "2026-07-06", "22:00", "06:00") == 40


def test_rule_r4_and_r5_work_hours_and_half_day():
    # 9 hours 7 minutes = 32820 seconds -> 9.116666... hrs -> 9.12 hrs
    p_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    p_out = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    wh = compute_work_hours(p_in, p_out)
    assert wh == 9.12
    assert compute_half_day(wh) is False

    # 3.5 hours worked -> 3.50 hrs -> half_day is True (< 4.50)
    p_in_hd = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    p_out_hd = int(datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    wh_hd = compute_work_hours(p_in_hd, p_out_hd)
    assert wh_hd == 3.50
    assert compute_half_day(wh_hd) is True

    # 4.49 hours worked -> half_day True; 4.50 hours worked -> half_day False
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False


def test_rule_r7_working_days():
    # July 2026: 31 days. Starts on Wednesday, ends on Friday. Total Mon-Fri = 23 days.
    assert count_working_days("2026-07-01", "2026-07-31", "2026-01-01") == 23

    # Mid-month joiner: joined on Monday 2026-07-13
    # Weekdays from 2026-07-13 to 2026-07-31 = 15 working days
    assert count_working_days("2026-07-01", "2026-07-31", "2026-07-13") == 15

    # Employee joins next month: 0 working days
    assert count_working_days("2026-07-01", "2026-07-31", "2026-08-01") == 0
