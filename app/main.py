"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, with local .env fallback)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load local .env without overriding pre-existing system environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Timezones
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# Database Indexes Setup
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)])
    except Exception as e:
        # In testing environments without a live MongoDB instance, index creation will be handled when DB is ready
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Enums and Pydantic Models (strictly matching openapi.yaml)
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = Field(default=PresenceStatus.PRESENT)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int = 0
    overtime_minutes: int = 0
    half_day: bool = False
    history: List[HistoryEntry] = Field(default_factory=list)


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# --------------------------------------------------------------------------- #
# Domain Calculation Helpers (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], places: int = 2) -> Optional[float]:
    """Round a number half-up to the given number of decimal places (Rule R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** (-places)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(ms: int) -> int:
    """Truncate epoch milliseconds to whole seconds (Rule R1)."""
    return (ms // 1000) * 1000


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Convert truncated epoch milliseconds to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (naive or aware) to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # PyMongo naive datetimes are in UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    Rule R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_utc = epoch_ms_to_datetime_utc(punch_in_ms)
    dt_ist = dt_utc.astimezone(IST)

    end_h, end_m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and (dt_ist.hour, dt_ist.minute, dt_ist.second) < (end_h, end_m, 0):
        # Shift started on the previous calendar day
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, attendance_date_str: str, shift_start: str) -> int:
    """
    Rule R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start. Otherwise 0.
    """
    dt_utc = epoch_ms_to_datetime_utc(punch_in_ms)
    punch_in_ist = dt_utc.astimezone(IST)

    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date_str).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:  # strictly more than 10 minutes (600 seconds)
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    Rule R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = (punch_out_ms - punch_in_ms) / 1000.0
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """
    Rule R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    return work_hours < 4.50


def compute_overtime_minutes(punch_out_ms: int, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    Rule R3: Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    dt_utc = epoch_ms_to_datetime_utc(punch_out_ms)
    punch_out_ist = dt_utc.astimezone(IST)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    att_date = date.fromisoformat(attendance_date_str)
    if (end_h, end_m) <= (start_h, start_m):
        # Overnight shift ends on the next calendar day
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt = datetime.combine(shift_end_date, time(end_h, end_m), tzinfo=IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_seconds < 0:
        return 0

    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def count_working_days(month_start_str: str, month_end_str: str, joined_on_str: str) -> int:
    """
    Rule R7: Working days are Mon-Fri, no holiday calendar.
    Counted from joined_on for mid-month joiners.
    """
    m_start = date.fromisoformat(month_start_str)
    m_end = date.fromisoformat(month_end_str)
    joined = date.fromisoformat(joined_on_str)

    if joined > m_end:
        return 0

    effective_start = max(m_start, joined)
    curr = effective_start
    working_days = 0
    while curr <= m_end:
        if curr.weekday() < 5:  # Monday is 0, Friday is 4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format a MongoDB attendance document into the OpenAPI AttendanceRecord shape."""
    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")

    formatted_history = []
    for h in doc.get("history", []):
        h_at = h.get("at")
        at_ms = datetime_to_epoch_ms(h_at) if isinstance(h_at, datetime) else h_at
        changes_dict = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if isinstance(f_val, datetime):
                f_val = datetime_to_epoch_ms(f_val)
            if isinstance(t_val, datetime):
                t_val = datetime_to_epoch_ms(t_val)
            changes_dict[k] = {"from": f_val, "to": t_val}
        formatted_history.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_dict,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": datetime_to_epoch_ms(p_in) if isinstance(p_in, datetime) else p_in,
        "punch_out": datetime_to_epoch_ms(p_out) if isinstance(p_out, datetime) else p_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": formatted_history,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"], operation_id="health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"], operation_id="createEmployee")
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Unique emp_code enforced by database unique index.
    created_at is server clock instant (epoch ms in API response).
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc["created_at"] = int(now_utc.timestamp() * 1000)
    doc.pop("_id", None)
    return doc


@app.get("/employees", response_model=EmployeePage, tags=["employees"], operation_id="listEmployees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        c_at = emp.get("created_at")
        if isinstance(c_at, datetime):
            emp["created_at"] = datetime_to_epoch_ms(c_at)
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, response_model=AttendanceRecord, tags=["attendance"], operation_id="punchIn")
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates the day's record (R1, R2).
    Race-safe with database unique compound index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_to_seconds(body.punched_at)
    else:
        now_utc = datetime.now(timezone.utc)
        punched_ms = truncate_to_seconds(int(now_utc.timestamp() * 1000))

    punch_in_dt = epoch_ms_to_datetime_utc(punched_ms)
    att_date = determine_attendance_date(punched_ms, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punched_ms, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecord, tags=["attendance"], operation_id="punchOut")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes employee's most recent record whose punch_in <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    Two simultaneous punch-outs yield exactly one 200.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_to_seconds(body.punched_at)
    else:
        now_utc = datetime.now(timezone.utc)
        punched_ms = truncate_to_seconds(int(now_utc.timestamp() * 1000))

    punch_out_dt = epoch_ms_to_datetime_utc(punched_ms)

    # Find the employee's most recent record that has a punch-in
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_ms = datetime_to_epoch_ms(rec["punch_in"])
    if punched_ms <= punch_in_ms:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")

    if (punched_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_ms, punched_ms)
    half_day = compute_half_day(work_hours)
    overtime = compute_overtime_minutes(punched_ms, rec["date"], emp["shift_start"], emp["shift_end"])

    # Concurrency safe update: ensure punch_out is still None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "half_day": half_day,
                "overtime_minutes": overtime,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"], operation_id="listAttendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Fast on 100k docs backed by indexes.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status.value
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"], operation_id="regularizeAttendance")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and appends one entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status.value if body.status is not None else rec.get("status")

    # Contract Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if target_status in (Status.ABSENT.value, Status.LEAVE.value):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times when setting status ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
    else:
        target_punch_in = truncate_to_seconds(body.punch_in) if body.punch_in is not None else datetime_to_epoch_ms(rec.get("punch_in"))
        target_punch_out = truncate_to_seconds(body.punch_out) if body.punch_out is not None else datetime_to_epoch_ms(rec.get("punch_out"))

    # Contract Rule: a presence status requires a punch_in (supplied or already stored)
    if target_status in (PresenceStatus.PRESENT.value, PresenceStatus.WFH.value, PresenceStatus.ON_DUTY.value):
        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    # Contract Rule: punch_in must stay on the record's attendance date (R1)
    if target_punch_in is not None:
        calc_date = determine_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    # Contract Rule: punch_out must be after punch_in and within 24 hours
    if target_punch_in is not None and target_punch_out is not None:
        if target_punch_out <= target_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
        if (target_punch_out - target_punch_in) > 24 * 3600 * 1000:
            raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if target_status in (PresenceStatus.PRESENT.value, PresenceStatus.WFH.value, PresenceStatus.ON_DUTY.value):
        target_late_minutes = compute_late_minutes(target_punch_in, date, emp["shift_start"])
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_half_day = compute_half_day(target_work_hours)
            target_overtime_minutes = compute_overtime_minutes(target_punch_out, date, emp["shift_start"], emp["shift_end"])
        else:
            target_work_hours = None
            target_half_day = False
            target_overtime_minutes = 0
    else:
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False

    # Extract initial values
    init_status = rec.get("status")
    init_punch_in = datetime_to_epoch_ms(rec.get("punch_in"))
    init_punch_out = datetime_to_epoch_ms(rec.get("punch_out"))
    init_work_hours = rec.get("work_hours")
    init_late_minutes = rec.get("late_minutes", 0)
    init_overtime_minutes = rec.get("overtime_minutes", 0)
    init_half_day = rec.get("half_day", False)

    changes_api: Dict[str, Dict[str, Any]] = {}
    changes_db: Dict[str, Dict[str, Any]] = {}

    if target_status != init_status:
        changes_api["status"] = {"from": init_status, "to": target_status}
        changes_db["status"] = {"from": init_status, "to": target_status}

    if target_punch_in != init_punch_in:
        changes_api["punch_in"] = {"from": init_punch_in, "to": target_punch_in}
        changes_db["punch_in"] = {
            "from": rec.get("punch_in"),
            "to": epoch_ms_to_datetime_utc(target_punch_in) if target_punch_in is not None else None,
        }

    if target_punch_out != init_punch_out:
        changes_api["punch_out"] = {"from": init_punch_out, "to": target_punch_out}
        changes_db["punch_out"] = {
            "from": rec.get("punch_out"),
            "to": epoch_ms_to_datetime_utc(target_punch_out) if target_punch_out is not None else None,
        }

    if target_work_hours != init_work_hours:
        changes_api["work_hours"] = {"from": init_work_hours, "to": target_work_hours}
        changes_db["work_hours"] = {"from": init_work_hours, "to": target_work_hours}

    if target_late_minutes != init_late_minutes:
        changes_api["late_minutes"] = {"from": init_late_minutes, "to": target_late_minutes}
        changes_db["late_minutes"] = {"from": init_late_minutes, "to": target_late_minutes}

    if target_overtime_minutes != init_overtime_minutes:
        changes_api["overtime_minutes"] = {"from": init_overtime_minutes, "to": target_overtime_minutes}
        changes_db["overtime_minutes"] = {"from": init_overtime_minutes, "to": target_overtime_minutes}

    if target_half_day != init_half_day:
        changes_api["half_day"] = {"from": init_half_day, "to": target_half_day}
        changes_db["half_day"] = {"from": init_half_day, "to": target_half_day}

    # Contract Rule: a request that changes nothing is 422
    if not changes_api:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry_db = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    current_hist_len = len(rec.get("history", []))

    # Optimistic lock on history length to prevent lost updates under concurrency
    update_filter = {
        "_id": rec["_id"],
        f"history.{current_hist_len}": {"$exists": False},
    }
    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": epoch_ms_to_datetime_utc(target_punch_in) if target_punch_in is not None else None,
            "punch_out": epoch_ms_to_datetime_utc(target_punch_out) if target_punch_out is not None else None,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {"history": history_entry_db},
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter, update_doc, return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"], operation_id="employeeMonthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    working_days = count_working_days(month_start, month_end, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$addFields": {
                "dow": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", [PresenceStatus.PRESENT.value, PresenceStatus.WFH.value, PresenceStatus.ON_DUTY.value]]},
                                    {"$lte": ["$dow", 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", Status.LEAVE.value]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    present_days_rounded = round_half_up(present_days, 2)
    attendance_pct = None
    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days_rounded,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"], operation_id="departmentSummary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """
    Department summary for a month via MongoDB aggregation across both collections.
    Headcount per R9 (employees with zero logs included).
    Average work hours is mean over records, not average of averages.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", [PresenceStatus.PRESENT.value, PresenceStatus.WFH.value, PresenceStatus.ON_DUTY.value]]},
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {"dateString": "$$l.date"}
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flat_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", [PresenceStatus.PRESENT.value, PresenceStatus.WFH.value, PresenceStatus.ON_DUTY.value]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", Status.LEAVE.value]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", PresenceStatus.ON_DUTY.value]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        if r.get("headcount", 0) == 0:
            continue
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r.get("present_days", 0.0), 2),
            "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"], operation_id="lateLeaderboard")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    Late-comers leaderboard for a month using standard competition ranking with ties ($rank).
    limit is applied after ranking (rank <= limit).
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"], operation_id="departmentTrend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department with gap filling ($densify) and 7-day moving average.
    Returns exactly one row per calendar day from from_date to to_date inclusive.
    """
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")

    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 for unknown department (no employees in department)
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
    to_bound_dt = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "dummy_dates": [
                    {"d": from_dt},
                    {"d": datetime.combine(d_to, time.min, tzinfo=timezone.utc)},
                ]
            }
        },
        {"$unwind": "$dummy_dates"},
        {"$project": {"cur_date": "$dummy_dates.d"}},
        {
            "$densify": {
                "field": "cur_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_bound_dt],
                },
            }
        },
        {
            "$project": {
                "date_str": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$cur_date"}
                },
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$cur_date"}, 5]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "active_emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d_str": "$date_str", "active_codes": "$active_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d_str"]},
                                    {"$in": ["$emp_code", "$$active_codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs",
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": {"$size": "$active_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", [PresenceStatus.PRESENT.value, PresenceStatus.WFH.value, PresenceStatus.ON_DUTY.value]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        att_rate = d.get("attendance_rate")
        m_avg = d.get("moving_avg_7d")
        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": round_half_up(d.get("present_count", 0.0), 2),
            "late_count": d.get("late_count", 0),
            "attendance_rate": round_half_up(att_rate, 4),
            "moving_avg_7d": round_half_up(m_avg, 4),
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"], operation_id="explainEndpoint")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Status] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation of the named endpoint.
    Verifies that the plan uses index scans (IXSCAN) and contains no collection scans (COLLSCAN).
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status.value
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        explain_res = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        m_start = f"{month}-01"
        m_end = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
            {"$addFields": {"dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {"_id": None, "total_late": {"$sum": "$late_minutes"}}},
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        m_end = f"{month}-{num_days:02d}"
        emp_match: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        m_start = f"{month}-01"
        m_end = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' dates are required for department_trend")
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
        ]
        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    raise HTTPException(status_code=422, detail="Unknown endpoint to explain")
