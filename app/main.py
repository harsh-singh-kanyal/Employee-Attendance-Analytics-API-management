"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import OperationFailure

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# Create indexes at startup (idempotent — skips if index already exists with a different name)
def _ensure_index(collection, keys, **kwargs):
    try:
        collection.create_index(keys, **kwargs)
    except OperationFailure as e:
        if e.code == 85:  # IndexOptionsConflict — index already exists with a different name, that's fine
            pass
        else:
            raise

_ensure_index(db.employees, [("emp_code", ASCENDING)], unique=True)
_ensure_index(db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
_ensure_index(db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)])
_ensure_index(db.attendance_logs, [("emp_code", ASCENDING), ("date", ASCENDING), ("late_minutes", ASCENDING)])

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ABSENCE_STATUSES = {"ABSENT", "LEAVE"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _round_half_up(value: float, decimals: int) -> float:
    """Half-up rounding — not Python's default banker's rounding (R8)."""
    d = Decimal(str(value))
    quantize_str = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


def _epoch_ms(dt: datetime) -> Optional[int]:
    """Convert a naive (stored as UTC) or aware datetime → epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _serialize_record(doc: dict) -> dict:
    """Convert MongoDB attendance doc to API response shape (datetimes → epoch ms)."""
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    for field in ("punch_in", "punch_out"):
        if isinstance(doc.get(field), datetime):
            doc[field] = _epoch_ms(doc[field])
    for entry in doc.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = _epoch_ms(entry["at"])
        for v in (entry.get("changes") or {}).values():
            if isinstance(v.get("from"), datetime):
                v["from"] = _epoch_ms(v["from"])
            if isinstance(v.get("to"), datetime):
                v["to"] = _epoch_ms(v["to"])
    return doc


def _month_range(month: str):
    """Return (date_from_str, date_to_str) inclusive for a 'YYYY-MM' string."""
    import calendar
    year, mon = int(month[:4]), int(month[5:])
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _working_days_in_range(date_from: str, date_to: str, joined_on: str = None) -> int:
    """Count Mon-Fri days in [date_from, date_to], starting no earlier than joined_on (R7)."""
    effective_start = max(date_from, joined_on) if joined_on else date_from
    start = datetime.fromisoformat(effective_start).date()
    end = datetime.fromisoformat(date_to).date()
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """R2: late only if > 10 min after shift_start; value = floor minutes since shift_start."""
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, half-up to 2 decimals."""
    return _round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    """R3: floor minutes past shift_end, counted only when >= 30."""
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0)
    raw = int((punch_out - end).total_seconds() / 60)
    return raw if raw >= 30 else 0


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# System / employee endpoints (original)
# --------------------------------------------------------------------------- #

@app.get("/")
def root():
    """Redirect root to Swagger docs."""
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    """Readiness probe — returns 200 only if MongoDB answers a ping."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is not available")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    ts = ts.replace(microsecond=0)
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-out
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts_raw = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(IST)
    ts = ts_raw.replace(microsecond=0)
    ts_naive = ts.replace(tzinfo=None)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_naive}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no open punch-in found for this employee")

    punch_in_naive = record["punch_in"]
    if hasattr(punch_in_naive, "tzinfo") and punch_in_naive.tzinfo:
        punch_in_naive = punch_in_naive.replace(tzinfo=None)

    if ts_naive <= punch_in_naive:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts_naive - punch_in_naive).total_seconds() > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_naive, ts_naive)
    overtime_minutes = compute_overtime(ts_naive, emp["shift_end"], record["date"])
    half_day = work_hours < 4.50

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts_naive,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "already punched out")

    return _serialize_record(result)


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date} — Regularize (manual correction)
# --------------------------------------------------------------------------- #

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record found for that date")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch times with ABSENT or LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(tzinfo=None)
            if body.punch_in is not None else record.get("punch_in")
        )
        new_punch_out = (
            datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(tzinfo=None)
            if body.punch_out is not None else record.get("punch_out")
        )
        if new_punch_in is None and new_status in PRESENCE_STATUSES:
            raise HTTPException(422, "presence status requires a punch_in")
        if new_punch_in and new_punch_out:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    def _as_ms(val):
        return _epoch_ms(val) if isinstance(val, datetime) else val

    old_pi, old_po = record.get("punch_in"), record.get("punch_out")
    changes = {}

    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if _as_ms(new_punch_in) != _as_ms(old_pi):
        changes["punch_in"] = {"from": _as_ms(old_pi), "to": _as_ms(new_punch_in)}
    if _as_ms(new_punch_out) != _as_ms(old_po):
        changes["punch_out"] = {"from": _as_ms(old_po), "to": _as_ms(new_punch_out)}

    # Recompute derived fields
    if new_status in ABSENCE_STATUSES:
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"]) if new_punch_in else record.get("late_minutes", 0)
        if new_punch_in and new_punch_out:
            new_wh = compute_work_hours(new_punch_in, new_punch_out)
            new_ot = compute_overtime(new_punch_out, emp["shift_end"], date)
            new_hd = new_wh < 4.50
        else:
            new_wh, new_ot, new_hd = None, 0, False

    for field, old_v, new_v in [
        ("late_minutes", record.get("late_minutes"), new_late),
        ("work_hours", record.get("work_hours"), new_wh),
        ("overtime_minutes", record.get("overtime_minutes"), new_ot),
        ("half_day", record.get("half_day"), new_hd),
    ]:
        if old_v != new_v:
            changes[field] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "late_minutes": new_late,
                "work_hours": new_wh,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, "concurrent edit conflict")

    return _serialize_record(result)


# --------------------------------------------------------------------------- #
# GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    date_from, date_to = _month_range(month)
    working_days = _working_days_in_range(date_from, date_to, emp["joined_on"])

    records = list(db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {"$gte": date_from, "$lte": date_to},
    }))

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for r in records:
        is_weekday = datetime.fromisoformat(r["date"]).weekday() < 5
        if r["status"] == "LEAVE":
            leave_days += 1
        if r.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += r["late_minutes"]
        total_overtime_minutes += r.get("overtime_minutes", 0)
        if r["status"] in PRESENCE_STATUSES and is_weekday:
            wh = r.get("work_hours")
            present_days += 0.5 if (wh is not None and wh < 4.50) else 1.0

    attendance_pct = (
        _round_half_up((present_days / working_days) * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# GET /analytics/departments/summary
# --------------------------------------------------------------------------- #

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    date_from, date_to = _month_range(month)

    emp_q = {}
    if department:
        emp_q["department"] = department

    all_employees = list(db.employees.find(emp_q, {"_id": 0}))

    dept_emps = defaultdict(list)
    for e in all_employees:
        if e["joined_on"] <= date_to:  # R9 headcount
            dept_emps[e["department"]].append(e)

    result_items = []
    for dept, emps in sorted(dept_emps.items()):
        headcount = len(emps)
        if headcount == 0:
            continue

        emp_codes = [e["emp_code"] for e in emps]
        records = list(db.attendance_logs.find({
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": date_from, "$lte": date_to},
        }))

        present_days = 0.0
        work_hours_list = []
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0

        for r in records:
            is_weekday = datetime.fromisoformat(r["date"]).weekday() < 5
            if r["status"] == "LEAVE":
                leave_count += 1
            if r["status"] == "ON_DUTY":
                on_duty_count += 1
            if r.get("late_minutes", 0) > 0:
                late_count += 1
                total_late_minutes += r["late_minutes"]
            if r["status"] in PRESENCE_STATUSES and is_weekday:
                wh = r.get("work_hours")
                present_days += 0.5 if (wh is not None and wh < 4.50) else 1.0
                if wh is not None:
                    work_hours_list.append(wh)

        avg_work_hours = (
            _round_half_up(sum(work_hours_list) / len(work_hours_list), 2)
            if work_hours_list else None
        )

        result_items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": result_items}


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    date_from, date_to = _month_range(month)

    emp_q = {}
    if department:
        emp_q["department"] = department

    all_employees = {e["emp_code"]: e for e in db.employees.find(emp_q, {"_id": 0})}

    records = list(db.attendance_logs.find({
        "emp_code": {"$in": list(all_employees.keys())},
        "date": {"$gte": date_from, "$lte": date_to},
        "late_minutes": {"$gt": 0},
    }))

    totals: dict = defaultdict(lambda: {"total_late_minutes": 0, "late_count": 0})
    for r in records:
        totals[r["emp_code"]]["total_late_minutes"] += r["late_minutes"]
        totals[r["emp_code"]]["late_count"] += 1

    rows = []
    for ec, stats in totals.items():
        emp = all_employees.get(ec)
        if not emp:
            continue
        rows.append({
            "emp_code": ec,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": stats["total_late_minutes"],
            "late_count": stats["late_count"],
        })

    # Sort: total_late_minutes desc, emp_code asc
    rows.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))

    # Standard competition ranking (1, 2, 2, 4, ...)
    rank = 1
    for i, row in enumerate(rows):
        if i > 0 and row["total_late_minutes"] < rows[i - 1]["total_late_minutes"]:
            rank = i + 1
        row["rank"] = rank

    return {"month": month, "items": [r for r in rows if r["rank"] <= limit]}


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if not from_date or not to_date:
        raise HTTPException(422, "Both 'from' and 'to' query parameters are required")
    try:
        from_dt = datetime.fromisoformat(from_date).date()
        to_dt = datetime.fromisoformat(to_date).date()
    except ValueError:
        raise HTTPException(422, "Invalid date format; use YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(422, "'to' must be >= 'from'")
    if (to_dt - from_dt).days > 91:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    all_emps = list(db.employees.find({"department": department}, {"_id": 0}))
    if not all_emps:
        raise HTTPException(404, "department not found")

    records = list(db.attendance_logs.find({
        "emp_code": {"$in": [e["emp_code"] for e in all_emps]},
        "date": {"$gte": from_date, "$lte": to_date},
    }))

    by_date: dict = defaultdict(list)
    for r in records:
        by_date[r["date"]].append(r)

    items = []
    non_null_rates = []  # sliding window for moving_avg_7d

    cur = from_dt
    while cur <= to_dt:
        cur_str = cur.isoformat()
        is_working_day = cur.weekday() < 5
        headcount = sum(1 for e in all_emps if e["joined_on"] <= cur_str)
        day_records = by_date.get(cur_str, [])

        present_count = 0.0
        late_count = 0
        for r in day_records:
            if r["status"] in PRESENCE_STATUSES:
                wh = r.get("work_hours")
                present_count += 0.5 if (wh is not None and wh < 4.50) else 1.0
            if r.get("late_minutes", 0) > 0:
                late_count += 1

        if is_working_day and headcount > 0:
            attendance_rate = _round_half_up(present_count / headcount, 4)
            non_null_rates.append(attendance_rate)
        else:
            attendance_rate = None

        window = non_null_rates[-7:]
        moving_avg_7d = _round_half_up(sum(window) / len(window), 4) if window else None

        items.append({
            "date": cur_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, f"endpoint must be one of {sorted(VALID)}")

    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explained = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        df, dt = _month_range(month)
        q = {"emp_code": emp_code, "date": {"$gte": df, "$lte": dt}}
        explained = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        df, dt = _month_range(month)
        q = {"date": {"$gte": df, "$lte": dt}}
        explained = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        df, dt = _month_range(month)
        q = {"date": {"$gte": df, "$lte": dt}, "late_minutes": {"$gt": 0}}
        explained = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        q = {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}
        explained = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}
