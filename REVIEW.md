# Code Review — Starter `app/main.py`

Below are the bugs and issues found in the starter code provided by the departing colleague.

---

## Race Conditions

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 1 | `POST /employees` (line 79) | **Check-then-insert** — `find_one` + `insert_one` is not atomic; two concurrent requests with the same `emp_code` can both pass the check and create duplicates. | Use a unique index on `emp_code` and catch `DuplicateKeyError` for 409. |
| 2 | `POST /attendance/punch-in` (line 104) | **Check-then-insert** — same race as above; two concurrent punch-ins for the same employee and date can both succeed. | Use a compound unique index on `(emp_code, date)` and catch `DuplicateKeyError`. |

---

## Timezone Bugs

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 3 | `punch_in` (line 102) | `datetime.fromtimestamp()` creates a **naive** datetime in local time, not UTC. Behavior varies by server timezone. | Use `datetime.fromtimestamp(ts / 1000, tz=timezone.utc)` for UTC-aware datetimes. |
| 4 | `punch_in` (line 103) | `ts.date().isoformat()` uses the server's local date, not IST. The attendance date must be determined in IST per R1. | Convert to IST before extracting the date. |
| 5 | `punch_in` (line 103) | No handling of **overnight shifts** — a punch-in at 05:30 IST for a 22:00–06:00 shift should belong to the previous day's date (R1). | Check if shift is overnight (`shift_end <= shift_start`) and if IST time is before `shift_end`, subtract one day. |
| 6 | `create_employee` (line 82) | `datetime.now()` is naive — `created_at` should be a UTC-aware datetime (stored as BSON datetime, returned as epoch ms). | Use `datetime.now(timezone.utc)`. |
| 7 | `compute_overtime` (line 46) | `datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)` — adding `tzinfo` to a naive datetime via `.replace()` does not convert; it just labels it. The shift end calculation is incorrect for overnight shifts. | Properly construct the shift end datetime in IST, handling day rollover for overnight shifts. |

---

## Rounding Bugs

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 8 | `compute_work_hours` (line 41) | Uses Python's `round()` which does **banker's rounding** (round-half-to-even), not half-up as required by R4. e.g., `round(9.125, 2)` → `9.12` instead of `9.13`. | Use `Decimal` with `ROUND_HALF_UP`. |
| 9 | `compute_late_minutes` (line 36) | Uses `int()` truncation on `total_seconds() / 60` which can produce wrong results for negative remainders. | Use `math.floor()` explicitly for whole minutes. |

---

## Epoch Milliseconds Issues

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 10 | Response bodies | Punch times are returned as Python `datetime` objects, not **epoch milliseconds** as required by the API contract. | Convert all datetime fields to epoch milliseconds (integers) in responses. |
| 11 | `punch_in` (line 102) | No validation that `punched_at` is a valid epoch milliseconds value (must be ≥ 100000000000). Seconds-range values, floats, and strings should return 422. | Add validation per `EpochMillis` schema. |
| 12 | Responses | `created_at` for employees is returned as a datetime string, not epoch milliseconds. | Convert to epoch ms integer. |
| 13 | `punch_in` (line 102) | Instants are not **truncated to whole seconds** before processing (R1 requires truncation). | Truncate microseconds: `dt.replace(microsecond=0)`. |

---

## Pagination Bugs

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 14 | `list_employees` (line 93) | `skip = page * page_size` — off-by-one error. Page 1 skips `page_size` items, so the first page of results is never returned. | Use `skip = (page - 1) * page_size`. |
| 15 | `list_employees` (line 94) | `total = db.employees.count_documents({})` — counts **all** employees regardless of the `department` filter. | Use `count_documents(q)` with the same filter. |
| 16 | `list_employees` (line 95) | No `sort()` — results are in insertion order, not sorted by `emp_code` ascending as required. | Add `.sort("emp_code", 1)`. |
| 17 | `list_attendance` (line 144-147) | Loads **all** matching documents into memory (`list(find(q))`), sorts in Python, then paginates. This will be extremely slow and memory-intensive on 100k records. | Use MongoDB server-side `.sort()` and `.skip()/.limit()`. |
| 18 | Pagination parameters | No validation that `page >= 1` or `page_size` is between 1–100 (R10). | Add Pydantic/FastAPI validation with `Query(ge=1)` etc. |

---

## Missing Endpoints

| # | Endpoint | Status |
|---|----------|--------|
| 19 | `POST /attendance/punch-out` | **Not implemented** — no way to clock out. |
| 20 | `PATCH /attendance/{emp_code}/{date}` | **Not implemented** — no manual correction / regularization. |
| 21 | `GET /analytics/employees/{emp_code}/monthly` | **Not implemented** — no monthly summary. |
| 22 | `GET /analytics/departments/summary` | **Not implemented** — no department summary. |
| 23 | `GET /analytics/leaderboard/late` | **Not implemented** — no late-comers leaderboard. |
| 24 | `GET /analytics/departments/{department}/trend` | **Not implemented** — no daily trend with moving average. |

---

## Other Issues

| # | Location | Bug |
|---|----------|-----|
| — | `compute_late_minutes` | Does not enforce the 10-minute **grace period** correctly. `minutes > 10` should be `total_seconds > 600` (strictly more than 10:00). At exactly 10:00 the employee is not late. |
| — | `compute_overtime` | No **30-minute threshold** — any time past `shift_end` counts as overtime. Per R3, overtime is 0 unless ≥ 30 minutes. |
| — | `punch_in` | No check that the employee exists (404). If `emp` is `None`, `emp["shift_start"]` will raise a `TypeError`. |
| — | `punch_in` | `half_day` hardcoded to `False` — should remain `False` until punch-out, but the logic to compute it at punch-out is missing. |
| — | `health` endpoint | Does not actually ping MongoDB. Should return 503 if MongoDB is down. |
| — | Indexes | No indexes created at startup. The `emp_code` unique index and `(emp_code, date)` compound index are critical for correctness and performance. |
| — | `_id` field | `_id` leaks into some responses. Per the contract, `_id` must never appear in any response. |
