
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing from your .env file")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)

# ---------------------------------------------------------------------
# Database indexes
# ---------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", DESCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    department: str = Field(min_length=1)
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


class AttendanceCorrection(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None
    reason: str = Field(min_length=3)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def now_ist():
    return datetime.now(IST)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "Date must use YYYY-MM-DD format")


def parse_time(value: str):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise HTTPException(400, "Time must use HH:MM format")


def timestamp_from_ms(value: Optional[int]) -> datetime:
    if value is None:
        return now_ist()
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(IST)
    except (ValueError, OSError, OverflowError):
        raise HTTPException(400, "Invalid punched_at timestamp")


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    punch_in = as_ist(punch_in)
    start_time = parse_time(shift_start)
    start = punch_in.replace(
        hour=start_time.hour,
        minute=start_time.minute,
        second=0,
        microsecond=0,
    )
    minutes = int((punch_in - start).total_seconds() / 60)
    return max(0, minutes) if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (as_ist(punch_out) - as_ist(punch_in)).total_seconds()
    return round(max(0, seconds) / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    shift_date = parse_date(date_str)
    end_time = parse_time(shift_end)
    end = datetime.combine(shift_date, end_time, tzinfo=IST)
    return max(0, int((as_ist(punch_out) - end).total_seconds() / 60))


def public_doc(doc: dict) -> dict:
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    return doc


def require_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    return emp


def attendance_response(doc: dict) -> dict:
    return public_doc(doc)


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


# ---------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    parse_date(body.joined_on)
    parse_time(body.shift_start)
    parse_time(body.shift_end)

    doc = body.model_dump()
    doc["created_at"] = now_ist()

    try:
        result = db.employees.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        doc.pop("_id", None)
        return doc
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except PyMongoError as exc:
        print("CREATE EMPLOYEE DATABASE ERROR:", repr(exc))
        raise HTTPException(503, "Could not save employee")


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size

    items = [
        public_doc(doc)
        for doc in db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    ]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------
# Punch in
# ---------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)

    allowed_statuses = {"PRESENT", "ABSENT", "HALF_DAY", "LEAVE"}
    if body.status not in allowed_statuses:
        raise HTTPException(400, "Invalid attendance status")

    ts = timestamp_from_ms(body.punched_at)
    day = ts.date().isoformat()

    existing = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "date": day}
    )
    if existing:
        raise HTTPException(409, "Attendance already exists for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": body.status == "HALF_DAY",
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        doc.pop("_id", None)
        return doc
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance already exists for this date")
    except PyMongoError as exc:
        print("PUNCH IN DATABASE ERROR:", repr(exc))
        raise HTTPException(503, "Could not save attendance")


# ---------------------------------------------------------------------
# Punch out
# ---------------------------------------------------------------------
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = timestamp_from_ms(body.punched_at)
    day = ts.date().isoformat()

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "date": day}
    )
    if not record:
        raise HTTPException(404, "No punch-in found for this date")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_time = as_ist(record["punch_in"])
    if ts < punch_in_time:
        raise HTTPException(400, "Punch-out cannot be before punch-in")

    work_hours = compute_work_hours(punch_in_time, ts)
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, emp["shift_end"], day),
        "half_day": work_hours < 4,
    }

    if work_hours < 4 and record["status"] == "PRESENT":
        update["status"] = "HALF_DAY"

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance changed; retry the request")

    record.update(update)
    return attendance_response(record)


# ---------------------------------------------------------------------
# List attendance
# ---------------------------------------------------------------------
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            parse_date(date_from)
            query["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size

    items = [
        attendance_response(doc)
        for doc in db.attendance_logs.find(query)
        .sort("date", DESCENDING)
        .skip(skip)
        .limit(page_size)
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------
# Manual attendance correction with audit trail
# ---------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(emp_code: str, date: str, body: AttendanceCorrection):
    parse_date(date)
    emp = require_employee(emp_code)

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    changes = body.model_dump(exclude_unset=True)
    reason = changes.pop("reason", "").strip()
    if len(reason) < 3:
        raise HTTPException(400, "A correction reason is required")

    allowed_statuses = {"PRESENT", "ABSENT", "HALF_DAY", "LEAVE"}
    if "status" in changes and changes["status"] not in allowed_statuses:
        raise HTTPException(400, "Invalid attendance status")

    before = {
        key: record.get(key)
        for key in changes
    }

    if "punch_in" in changes and changes["punch_in"] is not None:
        changes["punch_in"] = as_ist(changes["punch_in"])
    if "punch_out" in changes and changes["punch_out"] is not None:
        changes["punch_out"] = as_ist(changes["punch_out"])

    new_in = changes.get("punch_in", record.get("punch_in"))
    new_out = changes.get("punch_out", record.get("punch_out"))

    if new_in is not None and new_out is not None:
        if as_ist(new_out) < as_ist(new_in):
            raise HTTPException(400, "Punch-out cannot be before punch-in")
        changes["work_hours"] = compute_work_hours(new_in, new_out)
        changes["overtime_minutes"] = compute_overtime(
            new_out, emp["shift_end"], date
        )
    elif "punch_out" in changes and changes["punch_out"] is None:
        changes["work_hours"] = None
        changes["overtime_minutes"] = 0

    if new_in is not None:
        changes["late_minutes"] = compute_late_minutes(
            as_ist(new_in), emp["shift_start"]
        )

    if "status" in changes:
        changes["half_day"] = changes["status"] == "HALF_DAY"
    elif changes.get("work_hours") is not None:
        changes["half_day"] = changes["work_hours"] < 4

    history_entry = {
        "corrected_at": now_ist(),
        "reason": reason,
        "before": before,
        "after": changes.copy(),
    }

    result = db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {
            "$set": changes,
            "$push": {"history": history_entry},
        },
    )
    if result.matched_count != 1:
        raise HTTPException(404, "Attendance record not found")

    record.update(changes)
    record.setdefault("history", []).append(history_entry)
    return attendance_response(record)


# ---------------------------------------------------------------------
# Monthly employee analytics (MongoDB aggregation)
# ---------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    require_employee(emp_code)

    month_start = date(year, month, 1)
    next_month = (
        date(year + 1, 1, 1)
        if month == 12
        else date(year, month + 1, 1)
    )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_days": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]
                    }
                },
                "absent_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]
                    }
                },
                "half_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "HALF_DAY"]}, 1, 0]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    if not results:
        return {
            "emp_code": emp_code,
            "year": year,
            "month": month,
            "total_days": 0,
            "present_days": 0,
            "absent_days": 0,
            "half_days": 0,
            "leave_days": 0,
            "total_work_hours": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }

    result = results[0]
    result.pop("_id", None)
    result.update({"emp_code": emp_code, "year": year, "month": month})
    return result


# ---------------------------------------------------------------------
# Department summary (MongoDB aggregation)
# ---------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    match = {}
    if date_from or date_to:
        match["date"] = {}
        if date_from:
            parse_date(date_from)
            match["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            match["date"]["$lte"] = date_to

    pipeline = [
        {"$match": match},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": {"path": "$employee", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"$ifNull": ["$employee.department", "Unknown"]},
                "total_records": {"$sum": 1},
                "present_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}
                },
                "absent_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}
                },
                "half_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "HALF_DAY"]}, 1, 0]}
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "average_late_minutes": {"$avg": "$late_minutes"},
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    return [
        {
            "department": row.pop("_id"),
            **row,
        }
        for row in results
    ]


# ---------------------------------------------------------------------
# Late leaderboard (MongoDB aggregation)
# ---------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    match = {"late_minutes": {"$gt": 0}}
    if date_from or date_to:
        match["date"] = {}
        if date_from:
            parse_date(date_from)
            match["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            match["date"]["$lte"] = date_to

    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": "$emp_code",
                "late_days": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "average_late_minutes": {"$avg": "$late_minutes"},
            }
        },
        {"$sort": {"total_late_minutes": -1}},
        {"$limit": limit},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": {"path": "$employee", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "late_days": 1,
                "total_late_minutes": 1,
                "average_late_minutes": {"$round": ["$average_late_minutes", 2]},
            }
        },
    ]
    return list(db.attendance_logs.aggregate(pipeline))


# ---------------------------------------------------------------------
# Department daily trend (MongoDB aggregation)
# ---------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    employee_codes = [
        emp["emp_code"]
        for emp in db.employees.find(
            {"department": department}, {"_id": 0, "emp_code": 1}
        )
    ]

    if not employee_codes:
        return {"department": department, "trend": []}

    match = {"emp_code": {"$in": employee_codes}}
    if date_from or date_to:
        match["date"] = {}
        if date_from:
            parse_date(date_from)
            match["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            match["date"]["$lte"] = date_to

    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": "$date",
                "total_records": {"$sum": 1},
                "present_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "PRESENT"]}, 1, 0]}
                },
                "absent_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}
                },
                "half_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "HALF_DAY"]}, 1, 0]}
                },
                "average_late_minutes": {"$avg": "$late_minutes"},
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    trend = [
        {"date": row.pop("_id"), **row}
        for row in db.attendance_logs.aggregate(pipeline)
    ]
    return {"department": department, "trend": trend}


# ---------------------------------------------------------------------
# Admin explain endpoint
# ---------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    supported = {
        "employees": {
            "collection": "employees",
            "operation": "find",
            "index_hint": "emp_code unique index",
        },
        "attendance": {
            "collection": "attendance_logs",
            "operation": "find",
            "index_hint": "emp_code + date compound index",
        },
        "monthly": {
            "collection": "attendance_logs",
            "operation": "aggregate",
            "index_hint": "emp_code + date compound index",
        },
        "departments-summary": {
            "collection": "attendance_logs",
            "operation": "aggregate with employee lookup",
            "index_hint": "date index",
        },
        "leaderboard-late": {
            "collection": "attendance_logs",
            "operation": "aggregate",
            "index_hint": "late_minutes and date filters",
        },
        "department-trend": {
            "collection": "attendance_logs",
            "operation": "aggregate",
            "index_hint": "emp_code + date compound index",
        },
    }

    if endpoint not in supported:
        raise HTTPException(
            404,
            detail={
                "message": "Unsupported endpoint name",
                "supported": sorted(supported.keys()),
            },
        )

    details = supported[endpoint]
    try:
        if details["collection"] == "employees":
            plan = db.command(
                "explain",
                {
                    "find": "employees",
                    "filter": {"emp_code": "EXPLAIN_SAMPLE"},
                },
                verbosity="queryPlanner",
            )
        else:
            plan = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": {"emp_code": "EXPLAIN_SAMPLE"},
                },
                verbosity="queryPlanner",
            )

        return {
            "endpoint": endpoint,
            **details,
            "queryPlanner": plan.get("queryPlanner", {}),
        }
    except PyMongoError as exc:
        print("EXPLAIN ERROR:", repr(exc))
        raise HTTPException(503, "Could not explain query")
