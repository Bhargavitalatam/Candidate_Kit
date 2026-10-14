# Employee Attendance & Analytics API

A robust, production-grade backend service for enterprise attendance tracking, manual regularization auditing, and MongoDB aggregation-driven analytics. Built with **FastAPI**, **Python 3.11+**, and **MongoDB 6.0+**.

---

## 1. Project Overview

This API manages the complete employee attendance lifecycle:
- **Employee Management:** Provision unique employee records with customizable work shifts (including overnight shifts).
- **Attendance Punch-In & Punch-Out:** Enforces exact grace periods (10 minutes), overtime minimums (30 minutes), half-day thresholds (< 4.50 hours), and half-up rounding.
- **Manual Corrections & Audit Trail:** Supports managerial regularization with immutable before/after audit entries in `history`.
- **Database-Driven Analytics:** Aggregates monthly summaries, department metrics, competition-ranked late-comer leaderboards with ties, and 7-day moving average daily trends using native MongoDB aggregation pipelines (`$densify`, `$setWindowFields`, `$lookup`, `$isoDayOfWeek`).
- **Explainability & Performance:** High-speed query execution on ~100k records backed by compound and unique indexes without collection scans (`COLLSCAN`).

---

## 2. Features & Contract Endpoints

All endpoints follow the OpenAPI 3.0 specification defined in `openapi.yaml`:

| Method | Path | Summary | Description |
|---|---|---|---|
| `GET` | `/health` | Readiness probe | Returns 200 OK only when MongoDB answers a ping; else 503 Service Unavailable. |
| `POST` | `/employees` | Create Employee | Creates employee record with unique `emp_code` enforced by unique index. |
| `GET` | `/employees` | List Employees | Filterable by department, paginated, sorted by `emp_code` ascending. |
| `POST` | `/attendance/punch-in` | Record Punch-In | Creates the day's record (R1, R2); race-safe with compound unique index on `(emp_code, date)`. |
| `POST` | `/attendance/punch-out` | Record Punch-Out | Computes work hours (R4), overtime (R3), and half-day (R5); concurrent-safe. |
| `GET` | `/attendance` | List Attendance | Filter by employee, date range, or status; paginated; sorted `(date DESC, emp_code ASC)`. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize Record | Manually correct attendance; recomputes derived fields; appends audit entry to `history`. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly Summary | Aggregates working days, Mon-Fri present days, leaves, overtime, and attendance percentage. |
| `GET` | `/analytics/departments/summary` | Department Summary | Cross-collection aggregation computing headcount (including zero-log employees) and average work hours over records. |
| `GET` | `/analytics/leaderboard/late` | Late Leaderboard | Standard competition ranking with ties (`$rank`); `limit` applied after ranking. |
| `GET` | `/analytics/departments/{department}/trend` | Department Trend | Gap-filling via `$densify`, headcount per day, attendance rates, and 7-day moving average via `$setWindowFields`. |
| `GET` | `/admin/explain/{endpoint}` | Query Explain | Runs MongoDB `explain` with `executionStats` to verify index scans (`IXSCAN`). |

---

## 3. Business Rules Implementation (R1 – R10)

* **R1 (Time Zone & Attendance Date):** Attendance dates and shifts use IST (`UTC+05:30`). Instants are truncated to whole seconds. Overnight shifts (`shift_end <= shift_start`) assign punch-ins prior to `shift_end` to the shift starting the previous calendar day.
* **R2 (Late Arrival):** 10-minute grace period. Punch-in strictly after `shift_start + 10:00` incurs whole minutes (floor) elapsed since `shift_start`.
* **R3 (Overtime):** Whole minutes (floor) between `shift_end` and `punch_out`, counted only when >= 30 minutes; otherwise 0.
* **R4 (Work Hours):** `(punch_out - punch_in)` in seconds divided by 3600, rounded 2 decimals half-up (`ROUND_HALF_UP`) using `Decimal`.
* **R5 (Half Day):** True when rounded `work_hours < 4.50`. Counts as 0.5 of a present day.
* **R6 (Presence Statuses):** `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
* **R7 (Working Days):** Monday–Friday only. Calculated from `joined_on` for mid-month joiners. `present_days` counts Mon–Fri records only (weekend logs add to late/overtime totals).
* **R8 (Rounding):** Reported figures are 2 decimals half-up (rates are 4 decimals half-up).
* **R9 (Headcount):** Employees whose `joined_on` is on or before the period end are included, regardless of whether attendance logs exist.
* **R10 (Pagination):** 1-indexed (`page >= 1`), `page_size` defaults to 20 (max 100). Total count reflects applied query filters.

---

## 4. Prerequisites

* **Python:** 3.11 or newer (Python 3.10+ compatible)
* **MongoDB:** 6.0 or newer (tested against MongoDB 6.0 / 7.0 / Atlas M0)
* **Operating System:** Windows, macOS, or Linux

---

## 5. Local Setup & Installation

### Step 1: Clone the repository
```bash
git clone <repository_url>
cd candidate_kit
```

### Step 2: Create and activate a virtual environment
**On Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**On Linux / macOS:**
```bash
python -m venv .venv
source .venv/bin/activate
```

### Step 3: Install dependencies
```bash
pip install -r requirements.txt
```

---

## 6. MongoDB Configuration

The application reads configuration from environment variables. Real system environment variables take priority over a local `.env` file.

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI (e.g. Atlas connection string or local Docker) |
| `MONGO_DB` | `attendance_db` | Target MongoDB database name |

### Option A: Local Docker
```bash
docker run -d --name mongo -p 27017:27017 mongo:7.0
```

### Option B: MongoDB Atlas (Free Tier M0)
Set the connection URI directly in your environment:
```bash
# Windows PowerShell
$env:MONGO_URI="mongodb+srv://<user>:<password>@cluster.mongodb.net/?retryWrites=true&w=majority"
$env:MONGO_DB="attendance_db"

# Linux / macOS
export MONGO_URI="mongodb+srv://<user>:<password>@cluster.mongodb.net/?retryWrites=true&w=majority"
export MONGO_DB="attendance_db"
```

### Option C: Local `.env` file (Optional)
Create a `.env` file in the repository root:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Note: Do not commit `.env` files to source control).*

---

## 7. Seeding Sample Data

To load the sample data documents from `sample_data/`:
```bash
python sample_seed.py
```

---

## 8. Running the Application

Start the API service with Uvicorn:
```bash
uvicorn app.main:app --port 8000 --reload
```
The application will automatically connect to MongoDB, verify health, and create all necessary indexes idempotently at startup.

---

## 9. Inspecting API Documentation

Once the server is running, open your browser to inspect and interact with the endpoints:
* **Interactive Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
* **ReDoc Documentation:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
* **OpenAPI Raw Specification:** `openapi.yaml` in the project root.

---

## 10. Running Automated Tests

Run the full automated test suite using `pytest`:
```bash
python -m pytest tests/ -v
```

The test suite tests:
1. Mathematical precision and edge cases for Rules R1–R8 (`tests/test_domain_rules.py`).
2. API contract adherence, HTTP status codes, validation, error handling, audit trails, and concurrency simulation (`tests/test_api_endpoints.py`).

---

## 11. Known Limitations & Notes

* **External Network Access:** The service does not make external HTTP calls; all operations are local or direct to the configured MongoDB cluster.
* **Authentication:** Per assignment specifications, no authentication/authorization layer is required; actor tracking in regularization audit trails relies on the client-provided `regularized_by` field.
