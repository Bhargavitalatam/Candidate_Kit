# Engineering Candidate Study Guide: Employee Attendance & Analytics API

Welcome! This study guide is designed specifically for you to prepare for your live technical interview walkthrough and discussion with the hiring team. It explains every design choice, algorithm, MongoDB pipeline stage, and test case in straightforward, intuitive language so you can speak with confidence and clarity.

---

## 1. System Architecture & Request Flow

```
                      +---------------------------------------+
                      |       Client / HTTP Request           |
                      +---------------------------------------+
                                          |
                                          v
                      +---------------------------------------+
                      |         FastAPI Router & CORS         |
                      +---------------------------------------+
                                          |
                        [Pydantic Request Validation (422)]
                                          |
                                          v
                      +---------------------------------------+
                      |  Domain Rules Engine (R1 - R8)        |
                      |  - Timezone conversion (IST / UTC)    |
                      |  - Whole-second truncation            |
                      |  - Late & Overtime calculation        |
                      |  - Half-up Decimal rounding           |
                      +---------------------------------------+
                                          |
                                          v
                      +---------------------------------------+
                      |        MongoDB Database Layer         |
                      |  - Unique Indexes (Concurrency guard) |
                      |  - Native Aggregations ($densify, etc)|
                      +---------------------------------------+
                                          |
                        [Pydantic Response Serialization]
                                          |
                                          v
                      +---------------------------------------+
                      |      Client JSON Response (200/201)   |
                      +---------------------------------------+
```

### High-Level Request Lifecycle:
1. **HTTP Ingestion:** FastAPI routes the incoming request to the endpoint handler in `app/main.py`.
2. **Input Validation:** Pydantic models automatically validate patterns (e.g. `^EMP\d{4,6}$`), date formats, shift constraints (`shift_start != shift_end`), and epoch millisecond bounds. If invalid, FastAPI immediately returns an HTTP 422 Unprocessable Entity error without hitting the database.
3. **Business Logic Execution:** Domain helper functions truncate timestamps to whole seconds (Rule R1), compute IST shift dates and overnight shifts, calculate late minutes (R2), compute work hours (R4), evaluate half-day criteria (R5), and calculate overtime (R3).
4. **Database Operations & Concurrency Safety:**
   - Atomic inserts and updates are protected by database unique compound indexes (`(emp_code, date)`).
   - If two identical requests hit the server at the exact same millisecond, MongoDB's unique index rejects the second with a `DuplicateKeyError`, which our API translates into an HTTP 409 Conflict.
5. **Response Formatting:** Documents are cleaned (MongoDB's internal `_id` is removed, timestamps are converted from BSON datetimes into integer epoch milliseconds), validated against the response schema, and returned with the appropriate status code.

---

## 2. Endpoints & Their Business Rules

### 1. `GET /health`
* **Purpose:** Readiness probe polled by load balancers and test runners.
* **Rule:** Must return 200 OK **only** if MongoDB successfully replies to a `ping` command. If the database is disconnected or down, it returns 503 Service Unavailable.

### 2. `POST /employees`
* **Purpose:** Creates a new employee record.
* **Rules:** `emp_code` must be unique (enforced by a database unique index). `shift_start` must not equal `shift_end`. `created_at` is set by the server clock in UTC and returned as epoch milliseconds.

### 3. `GET /employees`
* **Purpose:** Lists employees with pagination and optional department filtering.
* **Rules:** Paginated (page >= 1, page_size 1..100). Sorted by `emp_code` ascending. Total count must reflect the filtered set, not the entire collection.

### 4. `POST /attendance/punch-in`
* **Purpose:** Records a daily punch-in event.
* **Rules (R1, R2):**
  - Truncates millisecond instant to whole seconds.
  - Determines attendance date in IST. If employee has an overnight shift (`shift_end <= shift_start`) and punches in before `shift_end` in IST, the record belongs to the shift that started the previous calendar day.
  - Computes late minutes: 10-minute grace period. If arrival is strictly greater than 10 minutes past `shift_start`, computes floor minutes elapsed since `shift_start` (not since grace ended).
  - Enforces at most one punch-in per employee per attendance date.

### 5. `POST /attendance/punch-out`
* **Purpose:** Closes an open attendance session.
* **Rules (R3, R4, R5):**
  - Finds the employee's most recent record that has a `punch_in`.
  - Rejects with 409 if already punched out.
  - Rejects with 422 if `punched_at <= punch_in` or if session exceeds 24 hours.
  - Computes `work_hours`: `(punch_out - punch_in)` in seconds / 3600, rounded 2 decimals half-up.
  - Evaluates `half_day`: `work_hours < 4.50`.
  - Computes `overtime_minutes`: whole minutes (floor) past `shift_end`, counted only if >= 30 minutes; otherwise 0.

### 6. `GET /attendance`
* **Purpose:** Chronological query of attendance records.
* **Rules:** Sorted by `date` descending, then `emp_code` ascending. Backed by compound index. Validates `date_from <= date_to` (otherwise 422).

### 7. `PATCH /attendance/{emp_code}/{date}`
* **Purpose:** Managerial regularization (manual correction).
* **Rules:**
  - Setting status to `ABSENT` or `LEAVE` clears punch times (passing punch times with these statuses yields 422).
  - Setting a presence status (`PRESENT`, `WFH`, `ON_DUTY`) requires a `punch_in`.
  - `punch_in` must remain on the record's attendance date.
  - A request that changes nothing yields 422.
  - Recomputes all derived fields (`late_minutes`, `work_hours`, `half_day`, `overtime_minutes`).
  - Appends an immutable audit entry to `history` capturing `{at, by, reason, changes: {field: {from, to}}}`.

### 8. `GET /analytics/employees/{emp_code}/monthly`
* **Purpose:** Monthly summary for an individual employee.
* **Rules (R5, R6, R7, R8):**
  - `working_days`: Monday–Friday only. If employee joined mid-month, counts working days from `joined_on`.
  - `present_days`: Counts presence records on Mon–Fri only. Half days count as 0.5. Weekend records do not add to `present_days`, but do contribute to late and overtime totals.
  - `attendance_pct`: `(present_days / working_days) * 100`, rounded 2 decimals half-up. Null if `working_days == 0`.

### 9. `GET /analytics/departments/summary`
* **Purpose:** Departmental attendance statistics across both collections.
* **Rules (R9):**
  - `headcount`: Includes active employees whose `joined_on <= end_of_month`, even if they have zero attendance records. Departments with 0 headcount in that month are omitted.
  - `avg_work_hours`: Mean of `work_hours` across all qualifying records in the department (average over records, not average of employee averages).

### 10. `GET /analytics/leaderboard/late`
* **Purpose:** Monthly leaderboard of employees with the highest late minutes.
* **Rules:**
  - Standard competition ranking (1, 2, 2, 4) using MongoDB's `$rank` window function.
  - Employees tied on `total_late_minutes` share the same rank, and subsequent ranks are skipped.
  - `limit` is applied *after* ranking (`rank <= limit`). If there is a tie at the cutoff rank, all tied employees are returned.
  - Ordered by `total_late_minutes` descending, then `emp_code` ascending.

### 11. `GET /analytics/departments/{department}/trend`
* **Purpose:** Daily attendance trend for a department.
* **Rules:**
  - Returns exactly one row per calendar day from `from` to `to` inclusive, including weekends and days with no records at all (gap-filling via MongoDB `$densify`).
  - `is_working_day`: Mon–Fri boolean.
  - `headcount`: Number of department employees active on that day (`joined_on <= date`).
  - `attendance_rate`: `present_count / headcount`, 4 decimals half-up, only on working days with `headcount > 0`, otherwise `null`.
  - `moving_avg_7d`: 7-day moving average of `attendance_rate` over preceding days inside the requested range using MongoDB `$setWindowFields`.

### 12. `GET /admin/explain/{endpoint}`
* **Purpose:** Query plan verification.
* **Rules:** Runs MongoDB `explain` with `executionStats` on the exact query or pipeline constructed by the endpoint, proving execution uses index scans (`IXSCAN`) without collection scans (`COLLSCAN`).

---

## 3. Important Functions in `app/main.py`

| Function | What it does | Key implementation detail |
|---|---|---|
| `init_indexes()` | Creates MongoDB indexes idempotently at startup. | Called during FastAPI lifespan; guarantees uniqueness and fast range scans. |
| `truncate_to_seconds(ms)` | Truncates epoch milliseconds to whole seconds. | `(ms // 1000) * 1000`. Satisfies Rule R1 before any calculations. |
| `round_half_up(val, places)` | Rounds numbers half-up (standard arithmetic). | Uses Python's `decimal.Decimal` with `ROUND_HALF_UP` to prevent banker's rounding errors. |
| `determine_attendance_date(...)` | Determines the IST calendar date for a punch. | Converts to IST. If overnight shift and time is before `shift_end`, rolls date back 1 day. |
| `compute_late_minutes(...)` | Calculates late arrival duration. | Checks `seconds_elapsed > 600` (10-min grace), then returns `seconds // 60`. |
| `compute_work_hours(...)` | Computes shift duration. | `(punch_out - punch_in) / 3600` rounded 2 decimals half-up. |
| `compute_half_day(...)` | Evaluates half day status. | `work_hours < 4.50`. |
| `compute_overtime_minutes(...)` | Calculates overtime past shift end. | Aligns shift end (next day for overnight), checks `>= 30` minutes threshold. |
| `count_working_days(...)` | Calculates Mon–Fri days in a month. | Loops from `max(start_of_month, joined_on)` to `end_of_month`, checking `weekday() < 5`. |
| `format_attendance_doc(...)` | Serializes MongoDB documents into API format. | Strips `_id`, ensures no `id` field exists, formats BSON datetimes into epoch milliseconds. |

---

## 4. MongoDB Aggregation Pipeline Stages Explained

When asked how you implemented the analytics, explain that you leveraged native MongoDB aggregation stages rather than loading collections into Python:

### A. Late Leaderboard Pipeline (`late_leaderboard`)
1. **`$match`**: Filters `attendance_logs` where `date` falls in the requested month and `late_minutes > 0`. Uses index `(date, late_minutes, emp_code)`.
2. **`$group`**: Groups by `_id: "$emp_code"`, calculating `total_late_minutes: {"$sum": "$late_minutes"}` and `late_count: {"$sum": 1}`.
3. **`$lookup` & `$unwind`**: Left joins `employees` on `emp_code` to retrieve the employee's name and department. Any log referencing a non-existent employee is filtered out by `$unwind`.
4. **`$match` (Optional)**: If the client requested a specific department, filters by `emp.department == department` *before* ranking.
5. **`$setWindowFields` with `$rank`**: Computes standard competition ranking:
   ```json
   {
     "$setWindowFields": {
       "sortBy": {"total_late_minutes": -1},
       "output": {"rank": {"$rank": {}}}
     }
   }
   ```
   *Why `$rank` instead of `$denseRank`?* Standard competition ranking skips ranks after ties (1, 2, 2, 4). `$rank` does this; `$denseRank` does not.
6. **`$match`**: Filters `rank <= limit`. Because this runs *after* ranking, all employees tied at rank `limit` are kept!
7. **`$sort` & `$project`**: Orders rows by `total_late_minutes` descending, then `emp_code` ascending, and shapes the final JSON keys.

### B. Department Summary Pipeline (`department_summary`)
1. **`$match`**: Starts on the `employees` collection filtering `joined_on <= month_end` (Rule R9). This ensures employees with zero logs are included.
2. **`$lookup`**: Joins `attendance_logs` for that employee within the month. If an employee had no logs, their joined array is simply `[]`.
3. **`$group`**: Groups by `department`.
   - `headcount`: Computed as `{"$sum": 1}` over all matched employees.
   - `all_logs`: Pushes all log arrays together.
4. **`$project` with `$reduce` / `$concatArrays`**: Flattens the array of arrays into a single list of logs for the department.
5. **Department Metrics Project**:
   - `present_days`: Maps over flattened logs, checking if status is present and `$isoDayOfWeek <= 5` (Mon–Fri), adding 0.5 for half days or 1.0 for full days.
   - `avg_work_hours`: Runs `$avg` over only present records with `work_hours != null`. This computes the average over records directly, avoiding an average of averages.
6. **`$sort`**: Orders departments alphabetically by name ascending.

### C. Department Trend Pipeline (`department_trend`)
1. **`$densify` (Gap Filling)**:
   ```json
   {
     "$densify": {
       "field": "cur_date",
       "range": {
         "step": 1,
         "unit": "day",
         "bounds": [from_dt, to_bound_dt]
       }
     }
   }
   ```
   Generates a continuous sequence of daily calendar timestamps from `from` to `to`, even if a day has zero attendance logs or is a weekend.
2. **`$isoDayOfWeek`**: Evaluates `is_working_day` directly in MongoDB (`day <= 5` is Mon–Fri).
3. **`$lookup` (Active Employees & Logs)**: Looks up department employees with `joined_on <= date` for daily headcount, and department logs for that day.
4. **`$setWindowFields` (7-Day Moving Average)**:
   ```json
   {
     "$setWindowFields": {
       "sortBy": {"date": 1},
       "output": {
         "moving_avg_7d": {
           "$avg": "$attendance_rate",
           "window": {"documents": [-6, 0]}
         }
       }
     }
   }
   ```
   Averages `attendance_rate` over the current row and up to 6 preceding rows inside the range. MongoDB's `$avg` automatically ignores `null` values (such as weekends).

---

## 5. Attendance Data Model & Audit Trail

### Addressing Documents
* **Employees:** Identified by `emp_code` (e.g. `EMP0001`). MongoDB's `_id` is never used externally.
* **Attendance Records:** Natural composite key `(emp_code, date)`. At most one document per employee per day.

### Audit Trail (`history`)
* Punch-in and punch-out are normal lifecycle events and **never** write to `history`.
* Only manual regularizations (`PATCH`) append to `history`.
* Each history entry contains:
  - `at`: Timestamp of correction (stored as BSON datetime in UTC, returned as epoch ms).
  - `by`: Actor string (e.g. `hr.admin`).
  - `reason`: Explanation string (e.g. `biometric glitch`).
  - `changes`: Key-value map containing **only** fields that actually changed, each with `from` and `to`.

---

## 6. What Each Automated Test Proves

| Test | What it proves |
|---|---|
| `test_truncate_to_seconds` | Verifies Rule R1 whole-second truncation (e.g. `.900` ms is floored to `.000`). |
| `test_round_half_up` | Proves arithmetic half-up rounding works correctly where banker's rounding fails (e.g. 2.525 -> 2.53). |
| `test_determine_attendance_date_normal_shift` | Confirms normal day shifts match the IST calendar date. |
| `test_determine_attendance_date_overnight_shift` | Proves overnight shift punch-ins before `shift_end` are rolled back to the previous day's shift date. |
| `test_rule_r2_late_minutes` | Verifies the 10-minute grace period boundary: `09:40:00 -> 0`, `09:40:01 -> 10`, `10:15:59 -> 45`. |
| `test_rule_r3_overtime_minutes` | Verifies overtime rules: < 30 minutes yields 0; >= 30 minutes yields whole minutes. |
| `test_overtime_overnight_shift` | Proves overnight shift overtime measures from the next calendar day's `shift_end`. |
| `test_rule_r4_and_r5_work_hours_and_half_day` | Proves work hours calculation and half-day flag trigger (`work_hours < 4.50`). |
| `test_rule_r7_working_days` | Verifies Mon–Fri working days for regular and mid-month joiners. |
| `test_health_success` & `test_health_database_failure` | Proves `/health` returns 200 on DB ping and 503 when the database fails. |
| `test_create_and_list_employee` | Proves employee creation, unique index enforcement (409), validation errors (422), and pagination. |
| `test_punch_in_and_punch_out_flow` | Tests happy-path attendance cycle and duplicate punch rejection (409). |
| `test_punch_out_errors` | Verifies error status codes: 404 for unknown employee / missing punch-in, 422 for `punch_out <= punch_in` or duration > 24 hours. |
| `test_attendance_list_and_date_validation` | Verifies `/attendance` query validation (`date_from > date_to` -> 422). |
| `test_regularize_attendance_with_audit_trail` | Proves regularizing attendance updates derived fields, records before/after diffs in `history`, and rejects no-op changes (422). |
| `test_regularize_absent_cannot_have_punch_times` | Confirms marking `ABSENT` with punch times is rejected with 422. |
| `test_analytics_validations_and_404s` | Verifies 404 and 422 validations on analytics parameters. |
| `test_admin_explain_validations` | Verifies required parameter validation for the explain endpoint. |

---

## 7. Likely Interview Questions & Answers

### Q1: Why did you use a database unique index rather than checking `find_one()` before inserting?
**Answer:** "Checking `find_one()` before `insert_one()` creates a classic 'check-then-act' race condition. In a distributed or multi-threaded environment, two concurrent requests arriving at the same millisecond will both execute `find_one()`, both find no existing record, and both proceed to insert duplicate documents. By placing a unique compound index on `(emp_code, date)` directly in MongoDB, the database engine enforces atomicity at the storage layer. One insert succeeds with 201, and the other throws a `DuplicateKeyError`, which we catch and translate to an HTTP 409 Conflict."

### Q2: How did you handle standard competition ranking with ties in MongoDB?
**Answer:** "We used MongoDB's `$setWindowFields` aggregation stage with the `$rank` operator. Unlike `$denseRank` which assigns sequential integers (1, 2, 2, 3), `$rank` performs standard competition ranking where tied entries share a rank and subsequent ranks are skipped (1, 2, 2, 4). Furthermore, because the assignment specifies that `limit` applies *after* ranking (`rank <= limit`), our pipeline filters by `rank <= limit` after the window function. If multiple employees tie at the cutoff rank, all of them are returned."

### Q3: Why does your department summary start with `employees` rather than `attendance_logs`?
**Answer:** "Rule R9 requires that headcount includes all employees active on or before the end of the period, even if they have zero attendance logs. If we started the aggregation from `attendance_logs`, employees who were on leave or never punched in would be completely absent from the pipeline, artificially lowering the department headcount. By starting from `employees` and performing a `$lookup` on `attendance_logs`, every active employee is counted."

### Q4: How does your code handle overnight shifts?
**Answer:** "An overnight shift is detected when `shift_end <= shift_start` (e.g. 22:00 to 06:00). According to Rule R1, if an employee punches in before `shift_end` in IST (e.g. 01:00 AM on July 7th), that punch belongs to the shift that began the previous evening. Our `determine_attendance_date` helper converts the punch instant to IST and subtracts 1 day from the calendar date. When calculating overtime, the function recognizes that `shift_end` falls on the next calendar day (`date + 1 day`), preventing incorrect negative or 16-hour overtime calculations."

### Q5: Why is Python's built-in `round()` problematic for Rule R4?
**Answer:** "Python's built-in `round()` uses round-half-to-even (banker's rounding) on floating point numbers. For example, `round(2.525, 2)` produces `2.52` instead of `2.53`. Rule R4 explicitly specifies arithmetic half-up rounding ('rounded to 2 decimals half-up, not banker's rounding'). To guarantee exact mathematical compliance, we convert values to strings and use the `Decimal` module with `quantize(..., rounding=ROUND_HALF_UP)`."

---

## 8. Small Code Changes to Practise During Your Live Discussion

During the live session, the interviewers will ask you to make a small on-the-spot code change to verify that you understand the implementation. Here are three realistic scenarios you can practise:

### Practice Exercise 1: Add a Grace Period Configuration
* **Prompt:** "What if HR wants to make the grace period configurable per employee instead of a hardcoded 10 minutes?"
* **How to do it:**
  1. Add an optional field `grace_period_minutes: int = 10` to `EmployeeCreate` and the stored document.
  2. Update `compute_late_minutes` to accept `grace_period_minutes: int = 10`:
     ```python
     grace_seconds = grace_period_minutes * 60
     if diff_seconds > grace_seconds:
         return int(diff_seconds // 60)
     return 0
     ```
  3. Pass `emp.get("grace_period_minutes", 10)` in `punch_in` and `regularize_attendance`.

### Practice Exercise 2: Add Minimum Work Hours for Full Day
* **Prompt:** "What if full-day presence requires at least 8.0 hours, and anything between 4.5 and 8.0 is considered a three-quarter day (0.75)?"
* **How to do it:**
  1. In `app/main.py`, define an attendance credit function:
     ```python
     def get_presence_credit(work_hours: float) -> float:
         if work_hours < 4.50:
             return 0.5
         elif work_hours < 8.00:
             return 0.75
         return 1.0
     ```
  2. Update the aggregation pipelines to use `$switch` or conditional `$cond` to add `0.75` when `work_hours < 8.00`.

### Practice Exercise 3: Add an Earliest Punch-In Restriction
* **Prompt:** "Employees cannot punch in more than 2 hours before their shift starts."
* **How to do it:**
  1. In `POST /attendance/punch-in`:
     ```python
     earliest_allowed = shift_start_dt - timedelta(hours=2)
     if punch_in_ist < earliest_allowed:
         raise HTTPException(
             status_code=422,
             detail="Punch-in is not permitted more than 2 hours before shift start"
         )
     ```

You are now fully equipped with all the domain knowledge, architectural details, and answers needed for your technical interview!
