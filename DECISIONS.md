# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to guarantee employee code uniqueness and prevent duplicate daily logs under concurrent operations. To avoid in-memory sorts and full scans on large datasets, I added compound and single-field indexes on `employees(department, emp_code)` for filtered listings, `employees(department, joined_on)` and `employees(joined_on)` for monthly headcount filtering, `attendance_logs(date, emp_code)` for chronological attendance pagination, `attendance_logs(emp_code, punch_in)` and `attendance_logs(emp_code, date, punch_in)` for punch-out lookups across midnight, and `attendance_logs(date, late_minutes, emp_code)` for monthly leaderboard aggregation. I considered and rejected indexing `status` alone, as low cardinality yields poor selectivity compared to date-scoped indexes.

2. **Punch-in race.**
Both concurrent requests validate employee existence and compute identical attendance dates in parallel. Both issue `insert_one`. MongoDB's unique compound index on `(emp_code, date)` acts as the concurrency barrier: the database allows exactly one insert to commit (returning 201 Created) while immediately throwing `DuplicateKeyError` (E11000) on the other. Our exception handler catches this error and returns HTTP 409 Conflict.

3. **Ties.**
The pipeline calculates standard competition ranking (1, 2, 2, 4) using MongoDB's `$setWindowFields` with the `$rank` operator. Because `limit` applies after ranking (`rank <= limit`), any employees tied at the cutoff rank share that rank and are all included. Rows are deterministically sorted by `total_late_minutes` descending, then `emp_code` ascending. Consequently, the returned array may legitimately exceed `limit` items.

4. **Headcount.**
The department summary aggregation pipeline begins from the `employees` collection rather than `attendance_logs`. It filters active employees whose `joined_on` is on or before the month's end, and performs a `$lookup` to join their logs. When grouping by department, `headcount` is computed as `$sum: 1` over the employees, ensuring every active employee is counted even if their joined log array is completely empty.

5. **One thing you would change for 100x data.**
At 100x scale (10M+ attendance logs), I would maintain pre-aggregated daily and monthly departmental roll-up documents updated asynchronously via MongoDB Change Streams, eliminating expensive real-time cross-collection `$lookup` and `$densify` aggregations.
