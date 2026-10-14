"""
Integration and API endpoint tests using FastAPI TestClient with mongomock.
Verifies contract compliance, HTTP status codes, validation, error handling, and audit trails.
"""
from datetime import datetime, timezone
import mongomock
import pytest
from starlette.testclient import TestClient

import app.main as main_module
from app.main import app


@pytest.fixture(autouse=True)
def mock_mongodb(monkeypatch):
    """Set up an isolated in-memory mongomock database with indexes for each test."""
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_attendance_db"]

    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_db)

    # Initialize unique and compound indexes on the mock database
    main_module.init_indexes()
    yield mock_db


@pytest.fixture
def client_app():
    return TestClient(app)


def test_health_success(client_app):
    response = client_app.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_database_failure(client_app, monkeypatch):
    class BrokenAdmin:
        def command(self, *args, **kwargs):
            raise Exception("Connection lost")

    class BrokenClient:
        admin = BrokenAdmin()

    monkeypatch.setattr(main_module, "client", BrokenClient())
    response = client_app.get("/health")
    assert response.status_code == 503
    assert "Database unavailable" in response.json()["detail"]


def test_create_and_list_employee(client_app):
    # Valid create
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client_app.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # 409 Duplicate employee
    res_dup = client_app.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # 422 Invalid shift (start == end)
    bad_payload = payload.copy()
    bad_payload["emp_code"] = "EMP0002"
    bad_payload["shift_end"] = "09:30"
    res_bad_shift = client_app.post("/employees", json=bad_payload)
    assert res_bad_shift.status_code == 422

    # 422 Invalid emp_code regex
    bad_payload2 = payload.copy()
    bad_payload2["emp_code"] = "INVALID"
    bad_payload2["shift_end"] = "18:30"
    res_bad_code = client_app.post("/employees", json=bad_payload2)
    assert res_bad_code.status_code == 422

    # List employees
    res_list = client_app.get("/employees?department=Engineering&page=1&page_size=20")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] == 1
    assert len(list_data["items"]) == 1
    assert list_data["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_and_punch_out_flow(client_app):
    # Create employee
    emp_payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    client_app.post("/employees", json=emp_payload)

    # Punch in: 2026-07-06 09:28 IST (03:58 UTC) = 1783310280000 ms
    p_in_ms = 1783310280000
    res_in = client_app.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": p_in_ms, "status": "PRESENT"},
    )
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["emp_code"] == "EMP0001"
    assert in_data["date"] == "2026-07-06"
    assert in_data["status"] == "PRESENT"
    assert in_data["punch_in"] == p_in_ms
    assert in_data["punch_out"] is None
    assert in_data["work_hours"] is None
    assert in_data["late_minutes"] == 0
    assert in_data["overtime_minutes"] == 0
    assert in_data["half_day"] is False
    assert in_data["history"] == []
    assert "_id" not in in_data
    assert "id" not in in_data

    # Duplicate punch-in for same date -> 409
    res_dup = client_app.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0001", "punched_at": p_in_ms, "status": "PRESENT"},
    )
    assert res_dup.status_code == 409

    # Punch out: 2026-07-06 18:35 IST (13:05 UTC) = 1783343100000 ms
    p_out_ms = 1783343100000
    res_out = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": p_out_ms},
    )
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["punch_out"] == p_out_ms
    assert out_data["work_hours"] == 9.12
    assert out_data["half_day"] is False
    assert out_data["overtime_minutes"] == 0

    # Repeat punch-out -> 409 already punched out
    res_out_again = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": p_out_ms},
    )
    assert res_out_again.status_code == 409


def test_punch_out_errors(client_app):
    # Unknown employee -> 404
    res_unknown = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP9999", "punched_at": 1783343100000},
    )
    assert res_unknown.status_code == 404

    # Create employee with no punch-in -> 404
    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-13",
        },
    )
    res_no_in = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": 1783343100000},
    )
    assert res_no_in.status_code == 404

    # Punch in at t
    p_in_ms = 1783310280000
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": p_in_ms})

    # Punch out <= punch in -> 422
    res_early = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": p_in_ms - 1000},
    )
    assert res_early.status_code == 422

    # Punch out > 24 hours later -> 422
    res_late = client_app.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": p_in_ms + 25 * 3600 * 1000},
    )
    assert res_late.status_code == 422


def test_attendance_list_and_date_validation(client_app):
    # Bad date range date_from > date_to -> 422
    res = client_app.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422


def test_regularize_attendance_with_audit_trail(client_app):
    # Create employee & punch-in
    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    # Punch in late: 10:05 IST (04:35 UTC) = 1783312500000 ms -> date is 2026-07-06, late_minutes = 35
    p_in_ms = 1783312500000
    # Punch out: 18:30 IST (13:00 UTC) = 1783342800000 ms
    p_out_ms = 1783342800000

    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p_in_ms})
    client_app.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": p_out_ms})

    # Regularize punch-in to 09:28 IST (03:58 UTC) = 1783310280000 ms
    # Recomputes late_minutes from 35 to 0, work_hours from 8.42 to 9.03
    corrected_in_ms = 1783310280000
    reg_payload = {
        "punch_in": corrected_in_ms,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin",
    }
    patch_res = client_app.patch("/attendance/EMP0001/2026-07-06", json=reg_payload)
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["punch_in"] == corrected_in_ms
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03

    # Check history audit entry
    assert len(data["history"]) == 1
    entry = data["history"][0]
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert "punch_in" in entry["changes"]
    assert entry["changes"]["punch_in"]["from"] == p_in_ms
    assert entry["changes"]["punch_in"]["to"] == corrected_in_ms
    assert entry["changes"]["late_minutes"]["from"] == 35
    assert entry["changes"]["late_minutes"]["to"] == 0
    assert entry["changes"]["work_hours"]["from"] == 8.42
    assert entry["changes"]["work_hours"]["to"] == 9.03

    # Request changing nothing -> 422
    res_noop = client_app.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"reason": "no-op change", "regularized_by": "hr.admin"},
    )
    assert res_noop.status_code == 422


def test_regularize_absent_cannot_have_punch_times(client_app):
    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    p_in_ms = 1783310280000
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p_in_ms})

    # Setting ABSENT while supplying punch_in -> 422
    res = client_app.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": p_in_ms,
            "reason": "marked absent",
            "regularized_by": "hr.admin",
        },
    )
    assert res.status_code == 422


def test_analytics_validations_and_404s(client_app):
    # Employee monthly unknown employee -> 404
    res_m = client_app.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res_m.status_code == 404

    # Invalid month format -> 422
    res_bad_m = client_app.get("/analytics/employees/EMP0001/monthly?month=2026-7")
    assert res_bad_m.status_code == 422

    # Department trend unknown department -> 404
    res_t404 = client_app.get("/analytics/departments/NonexistentDept/trend?from=2026-07-01&to=2026-07-07")
    assert res_t404.status_code == 404

    # Department trend to < from -> 422
    res_bad_range = client_app.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res_bad_range.status_code == 422

    # Department trend range > 92 days -> 422
    res_long_range = client_app.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
    assert res_long_range.status_code == 422


def test_admin_explain_validations(client_app):
    # Missing required month for employee_monthly -> 422
    res = client_app.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res.status_code == 422

    # Missing required dates for department_trend -> 422
    res_trend = client_app.get("/admin/explain/department_trend?department=Engineering")
    assert res_trend.status_code == 422

    # Invalid endpoint name -> 422
    res_unknown = client_app.get("/admin/explain/unknown_endpoint")
    assert res_unknown.status_code == 422


def test_regularize_to_absent_clears_punch_times_and_derived(client_app):
    # Create employee & punch in/out
    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )
    p_in_ms = 1783310280000
    p_out_ms = 1783343100000
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": p_in_ms})
    client_app.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": p_out_ms})

    # Regularize to ABSENT
    patch_res = client_app.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "ABSENT", "reason": "unapproved absence", "regularized_by": "hr.admin"},
    )
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    # Check history entries
    assert len(data["history"]) == 1
    changes = data["history"][0]["changes"]
    assert changes["status"]["from"] == "PRESENT"
    assert changes["status"]["to"] == "ABSENT"
    assert changes["punch_in"]["from"] == p_in_ms
    assert changes["punch_in"]["to"] is None
    assert changes["punch_out"]["from"] == p_out_ms
    assert changes["punch_out"]["to"] is None


def test_analytics_employee_monthly_success(client_app, monkeypatch):
    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    fake_agg = [
        {
            "_id": None,
            "present_days": 18.5,
            "leave_days": 2,
            "late_count": 3,
            "total_late_minutes": 45,
            "total_overtime_minutes": 60,
        }
    ]
    monkeypatch.setattr(main_module.db.attendance_logs, "aggregate", lambda pipeline: fake_agg)

    res = client_app.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 18.5
    assert data["leave_days"] == 2
    assert data["late_count"] == 3
    assert data["total_late_minutes"] == 45
    assert data["total_overtime_minutes"] == 60
    assert data["attendance_pct"] == 80.43  # (18.5 / 23) * 100 = 80.4347... -> 80.43


def test_analytics_department_summary_success(client_app, monkeypatch):
    fake_summary = [
        {
            "department": "Engineering",
            "headcount": 5,
            "present_days": 92.5,
            "avg_work_hours": 8.75,
            "late_count": 4,
            "total_late_minutes": 80,
            "leave_count": 3,
            "on_duty_count": 1,
        }
    ]
    monkeypatch.setattr(main_module.db.employees, "aggregate", lambda pipeline: fake_summary)

    res = client_app.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["department"] == "Engineering"
    assert item["headcount"] == 5
    assert item["present_days"] == 92.5
    assert item["avg_work_hours"] == 8.75
    assert item["late_count"] == 4
    assert item["total_late_minutes"] == 80
    assert item["leave_count"] == 3
    assert item["on_duty_count"] == 1


def test_analytics_late_leaderboard_success(client_app, monkeypatch):
    fake_leaderboard = [
        {
            "rank": 1,
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "department": "Sales",
            "total_late_minutes": 75,
            "late_count": 2,
        },
        {
            "rank": 2,
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "department": "Engineering",
            "total_late_minutes": 35,
            "late_count": 1,
        },
    ]
    monkeypatch.setattr(main_module.db.attendance_logs, "aggregate", lambda pipeline: fake_leaderboard)

    res = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 2
    assert data["items"][0]["rank"] == 1
    assert data["items"][0]["emp_code"] == "EMP0003"
    assert data["items"][1]["rank"] == 2


def test_analytics_department_trend_success(client_app, monkeypatch):
    client_app.post(
        "/employees",
        json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        },
    )

    fake_trend_docs = [
        {
            "date": "2026-07-06",
            "is_working_day": True,
            "headcount": 1,
            "present_count": 1.0,
            "late_count": 0,
            "attendance_rate": 1.0,
            "moving_avg_7d": 1.0,
        },
        {
            "date": "2026-07-07",
            "is_working_day": True,
            "headcount": 1,
            "present_count": 0.5,
            "late_count": 0,
            "attendance_rate": 0.5,
            "moving_avg_7d": 0.75,
        },
    ]
    monkeypatch.setattr(main_module.db.employees, "aggregate", lambda pipeline: fake_trend_docs)

    res = client_app.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 2
    assert data["items"][0]["date"] == "2026-07-06"
    assert data["items"][0]["attendance_rate"] == 1.0
    assert data["items"][1]["moving_avg_7d"] == 0.75
