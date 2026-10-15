# Design Decisions

Key design decisions made while building the Employee Attendance & Analytics API.

---

## 1. Indexes

| Index | Collection | Type | Rationale |
|-------|-----------|------|-----------|
| `emp_code` | `employees` | Unique | Prevents duplicate employees atomically (race-safe). Speeds up all employee lookups. |
| `(emp_code, date)` | `attendance_logs` | Unique, compound | Prevents duplicate punch-ins for the same employee+date atomically. Supports efficient attendance queries. |
| `date` | `attendance_logs` | Regular | Accelerates date-range filters and analytics aggregations. |
| `(emp_code, date)` sort | `attendance_logs` | Compound | Supports the `GET /attendance` sort order (`date` desc, `emp_code` asc) for server-side sort without COLLSCAN. |

All indexes are created at startup using `create_index()` which is idempotent — safe to run on every restart.

---

## 2. Race Conditions

**Problem:** The starter code used check-then-insert (find → if not found → insert), which allows two concurrent requests to both pass the check and create duplicates.

**Decision:** Replace with **unique index + `DuplicateKeyError`**. The database enforces uniqueness atomically. On conflict, catch the error and return 409. This handles:
- Two concurrent `POST /employees` with the same `emp_code`
- Two concurrent `POST /attendance/punch-in` for the same employee and date
- Two concurrent `POST /attendance/punch-out` (use `find_one_and_update` with `punch_out: None` filter)
- Two concurrent `PATCH` corrections (use history length in the filter to detect concurrent edits)

---

## 3. Tie Handling (Leaderboard)

**Decision:** Standard competition ranking ("1224" ranking).

- Employees with equal `total_late_minutes` share the same rank
- The next rank is skipped by the number of tied employees
- Example: if 2 employees tie at rank 2, the next employee gets rank 4
- `limit` applies to **rank**, not row count — all employees at the cutoff rank are included
- Secondary sort: `emp_code` ascending (deterministic ordering for ties)

---

## 4. Headcount (R9)

**Decision:** Headcount counts employees whose `joined_on <= period_end`, regardless of whether they have any attendance logs.

- For monthly summaries: `joined_on <= last day of the month`
- For daily trend: `joined_on <= that specific day`
- Employees with zero logs still contribute to headcount
- This requires querying the `employees` collection separately (can't rely on `attendance_logs` alone)

---

## 5. Scaling Considerations

| Area | Decision | Why |
|------|----------|-----|
| Pagination | Server-side `skip()`/`limit()` with MongoDB | Starter code loaded all docs into memory and sorted in Python — would fail on 100k records |
| Aggregations | MongoDB aggregation pipelines | All analytics use `$match`, `$group`, `$lookup` in the database, not Python loops |
| `$densify` | Used for gap-filling in the trend endpoint | Generates missing dates in the database, avoiding Python date iteration |
| Explain endpoint | Validates IXSCAN usage | Ensures all queries hit indexes, no COLLSCAN on large datasets |
