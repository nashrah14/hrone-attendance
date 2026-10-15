"""Employee Attendance & Analytics API, contract v2.0."""

import os, re, json, math

from datetime import datetime, date, time, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional, Literal, Any

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

from pymongo.errors import DuplicateKeyError, PyMongoError

from bson import ObjectId

from bson.json_util import dumps as bson_dumps, loads as bson_loads

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)

db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def utc_now():

    return datetime.now(UTC).replace(microsecond=0)

def to_ms(value):

    if value is None: return None

    if value.tzinfo is None: value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)

def from_ms(value):

    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:

        raise ValueError("timestamp must be integer epoch milliseconds")

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)

def stored_dt(value):

    if value is None: return None

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

def parse_date(s: str) -> date:

    try:

        d = date.fromisoformat(s)

        if d.isoformat() != s: raise ValueError()

        return d

    except Exception: raise HTTPException(422, "date must be YYYY-MM-DD")

def parse_month(s: str):

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""):

        raise HTTPException(422, "month must be YYYY-MM")

    y, m = map(int, s.split("-")); start = date(y,m,1)

    end = date(y+1,1,1) - timedelta(days=1) if m == 12 else date(y,m+1,1)-timedelta(days=1)

    return start, end

def iso_utc(d):

    return datetime.combine(d, time.min, tzinfo=UTC)

def shift_minutes(s):

    try:

        h, m = map(int, s.split(":"))

        if len(s) != 5 or not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError()

        return h*60+m

    except Exception: raise HTTPException(422, "shift times must be HH:MM")

def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:

    local = stored_dt(punch_in).astimezone(IST)

    d = local.date()

    if shift_minutes(shift_end) <= shift_minutes(shift_start) and local.hour*60+local.minute < shift_minutes(shift_end):

        d -= timedelta(days=1)

    return d

def shift_start_instant(day: date, shift_start: str):

    h,m = map(int, shift_start.split(":")); return datetime.combine(day, time(h,m), IST).astimezone(UTC)

def shift_end_instant(day: date, shift_start: str, shift_end: str):

    h,m = map(int, shift_end.split(":")); end_day = day + timedelta(days=1) if shift_minutes(shift_end) <= shift_minutes(shift_start) else day

    return datetime.combine(end_day, time(h,m), IST).astimezone(UTC)

def compute_late_minutes(punch_in, shift_start, day=None):

    if punch_in is None: return 0

    local = stored_dt(punch_in).astimezone(IST)

    day = day or local.date()

    start = shift_start_instant(day, shift_start)

    seconds = int((stored_dt(punch_in)-start).total_seconds())

    return seconds // 60 if seconds > 600 else 0

def round_half_up(value, places=2):

    if value is None: return None

    q = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def compute_work_hours(punch_in, punch_out):

    if punch_in is None or punch_out is None: return None

    seconds = int((stored_dt(punch_out)-stored_dt(punch_in)).total_seconds())

    return round_half_up(seconds / 3600, 2)

def compute_overtime(punch_out, shift_start, shift_end, day):

    if punch_out is None: return 0

    end = shift_end_instant(day, shift_start, shift_end)

    seconds = int((stored_dt(punch_out)-end).total_seconds())

    minutes = seconds // 60

    return minutes if minutes >= 30 else 0

def recompute(record, emp):

    pi, po = record.get("punch_in"), record.get("punch_out")

    day = date.fromisoformat(record["date"])

    record["late_minutes"] = compute_late_minutes(pi, emp["shift_start"], day)

    record["work_hours"] = compute_work_hours(pi, po)

    record["overtime_minutes"] = compute_overtime(po, emp["shift_start"], emp["shift_end"], day) if po else 0

    record["half_day"] = bool(po is not None and record["work_hours"] < 4.50)

    return record

def public_record(doc):

    out = {k:v for k,v in doc.items() if k != "_id"}

    if "_id" in doc: out["id"] = str(doc["_id"])

    for key in ("punch_in", "punch_out"):

        out[key] = to_ms(out.get(key))

    history=[]

    for entry in out.get("history", []):

        e=dict(entry); e["at"] = to_ms(e.get("at"))

        changes={}

        for k,v in e.get("changes", {}).items():

            vv=dict(v)

            if k in ("punch_in", "punch_out"):

                vv["from"] = to_ms(vv.get("from")); vv["to"] = to_ms(vv.get("to"))

            changes[k]=vv

        e["changes"]=changes; history.append(e)

    out["history"]=history

    return out

def public_employee(doc):

    out={k:v for k,v in doc.items() if k != "_id"}

    out["created_at"] = to_ms(out.get("created_at"))

    return out

def jsonable_explain(obj):

    return json.loads(bson_dumps(obj))

def ensure_indexes():

    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")

    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")

    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="uq_attendance_employee_date")

    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")

    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_date_status")

@app.on_event("startup")

def startup():

    ensure_indexes()

class EmployeeIn(BaseModel):

    model_config=ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    joined_on: str

    @field_validator("joined_on")

    @classmethod

    def valid_joined(cls,v):

        try:

            if date.fromisoformat(v).isoformat()!=v: raise ValueError()

        except Exception: raise ValueError("joined_on must be YYYY-MM-DD")

        return v

    @model_validator(mode="after")

    def shifts_differ(self):

        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")

        return self

class PunchInIn(BaseModel):

    model_config=ConfigDict(extra="forbid")

    emp_code: str

    punched_at: Optional[StrictInt] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")

    @classmethod

    def valid_ts(cls,v):

        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("punched_at must be epoch milliseconds")

        return v

class PunchOutIn(BaseModel):

    model_config=ConfigDict(extra="forbid")

    emp_code: str

    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")

    @classmethod

    def valid_ts(cls,v):

        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("punched_at must be epoch milliseconds")

        return v

class RegularizeIn(BaseModel):

    model_config=ConfigDict(extra="forbid")

    status: Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]] = None

    punch_in: Optional[StrictInt] = None

    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")

    @classmethod

    def valid_ts(cls,v):

        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("timestamp must be epoch milliseconds")

        return v

@app.get("/health")

def health():

    try: client.admin.command("ping"); return {"status":"ok"}

    except Exception: raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc=body.model_dump(); doc["created_at"]=utc_now()

    try: db.employees.insert_one(doc)

    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")

    return public_employee(doc)

@app.get("/employees")

def list_employees(department: Optional[str]=None, page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):

    q={}

    if department is not None: q["department"]=department

    total=db.employees.count_documents(q)

    docs=db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)

    return {"items":[public_employee(x) for x in docs],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    emp=db.employees.find_one({"emp_code":body.emp_code})

    if not emp: raise HTTPException(404,"Employee not found")

    try: ts=from_ms(body.punched_at) if body.punched_at is not None else utc_now()

    except ValueError as e: raise HTTPException(422,str(e))

    day=attendance_date(ts,emp["shift_start"],emp["shift_end"]); d=day.isoformat()

    doc={"emp_code":body.emp_code,"date":d,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":compute_late_minutes(ts,emp["shift_start"],day),"overtime_minutes":0,"half_day":False,"history":[]}

    try: res=db.attendance_logs.insert_one(doc)

    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")

    doc["_id"]=res.inserted_id

    return public_record(doc)

@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    emp=db.employees.find_one({"emp_code":body.emp_code})

    if not emp: raise HTTPException(404,"Employee not found")

    ts=from_ms(body.punched_at) if body.punched_at is not None else utc_now()

    # Find latest open punch not later than the requested punch-out, including overnight shifts.

    doc=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts},"punch_out":None},sort=[("punch_in",DESCENDING)])

    if doc is None:

        prior=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}},sort=[("punch_in",DESCENDING)])

        if prior and prior.get("punch_out") is not None: raise HTTPException(409,"record already punched out")

        raise HTTPException(404,"No open punch-in found")

    pi=stored_dt(doc["punch_in"])

    secs=int((ts-pi).total_seconds())

    if secs<=0 or secs>86400: raise HTTPException(422,"punched_at must be after punch_in and within 24 hours")

    updated=dict(doc); updated["punch_out"]=ts; recompute(updated,emp)

    result=db.attendance_logs.find_one_and_update({"_id":doc["_id"],"punch_out":None},{"$set":{"punch_out":ts,"work_hours":updated["work_hours"],"overtime_minutes":updated["overtime_minutes"],"half_day":updated["half_day"]}},return_document=ReturnDocument.AFTER)

    if result is None: raise HTTPException(409,"record already punched out")

    return public_record(result)

@app.get("/attendance")

def list_attendance(emp_code: Optional[str]=None,date_from: Optional[str]=None,date_to: Optional[str]=None,status: Optional[str]=None,page: int=Query(1,ge=1),page_size: int=Query(20,ge=1,le=100)):

    q={}

    if emp_code: q["emp_code"]=emp_code

    if date_from:

        parse_date(date_from); q.setdefault("date",{})["$gte"]=date_from

    if date_to:

        parse_date(date_to); q.setdefault("date",{})["$lte"]=date_to

    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")

    if status:

        if status not in STATUSES: raise HTTPException(422,"invalid status")

        q["status"]=status

    total=db.attendance_logs.count_documents(q)

    docs=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)

    return {"items":[public_record(x) for x in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")

def regularize(emp_code: str,date: str,body: RegularizeIn):

    target_day=parse_date(date); emp=db.employees.find_one({"emp_code":emp_code})

    if not emp: raise HTTPException(404,"Employee not found")

    old=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})

    if not old: raise HTTPException(404,"Attendance record not found")

    new={k:old.get(k) for k in ("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")}

    changes_in=body.model_dump(exclude_unset=True,exclude={"reason","regularized_by"})

    for k,v in changes_in.items(): new[k]=from_ms(v) if k in ("punch_in","punch_out") and v is not None else v

    if new["status"] in ("ABSENT","LEAVE"):

        if "punch_in" in changes_in and changes_in["punch_in"] is not None or "punch_out" in changes_in and changes_in["punch_out"] is not None:

            raise HTTPException(422,"ABSENT/LEAVE cannot include punch times")

        new["punch_in"]=None; new["punch_out"]=None

    elif new["punch_in"] is None:

        raise HTTPException(422,"presence status requires punch_in")

    if new["punch_in"] is not None:

        if attendance_date(new["punch_in"],emp["shift_start"],emp["shift_end"]) != target_day: raise HTTPException(422,"punch_in must fall on the attendance date")

    if new["punch_out"] is not None:

        if new["punch_in"] is None: raise HTTPException(422,"punch_out requires punch_in")

        secs=(stored_dt(new["punch_out"])-stored_dt(new["punch_in"])).total_seconds()

        if secs<=0 or secs>86400: raise HTTPException(422,"punch_out must be after punch_in and within 24 hours")

    temp=dict(old); temp.update(new); recompute(temp,emp)

    for k in ("work_hours","late_minutes","overtime_minutes","half_day"): new[k]=temp[k]

    changes={}

    for k,v in new.items():

        oldv=old.get(k)

        if oldv != v:

            changes[k]={"from":oldv,"to":v}

    if not changes: raise HTTPException(422,"request does not change the record")

    entry={"at":utc_now(),"by":body.regularized_by,"reason":body.reason,"changes":changes}

    # Compare-and-set on original fields/history: simultaneous edits cannot overwrite history.

    filt={"_id":old["_id"],"history":old.get("history",[]),"status":old.get("status"),"punch_in":old.get("punch_in"),"punch_out":old.get("punch_out")}

    result=db.attendance_logs.find_one_and_update(filt,{"$set":new,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)

    if result is None: raise HTTPException(409,"record changed concurrently; retry")

    return public_record(result)

# Calendar-day count in O(1), avoiding document-by-document Python processing.

def weekdays_between(start: date, end: date):

    if end < start: return 0

    days=(end-start).days+1; weeks,rem=divmod(days,7); count=weeks*5

    for i in range(rem):

        if (start.weekday()+i)%7 < 5: count+=1

    return count

def month_log_pipeline(emp_code,start,end):

    return [{"$match":{"emp_code":emp_code,"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},{"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},{"$cond":["$half_day",0.5,1]},0]}},"leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}},"total_late_minutes":{"$sum":"$late_minutes"},"total_overtime_minutes":{"$sum":"$overtime_minutes"}}}]

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str,month: str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    emp=db.employees.find_one({"emp_code":emp_code})

    if not emp: raise HTTPException(404,"Employee not found")

    start,end=parse_month(month); active_start=max(start,date.fromisoformat(emp["joined_on"]))

    working=weekdays_between(active_start,end) if active_start<=end else 0

    agg=list(db.attendance_logs.aggregate(month_log_pipeline(emp_code,start,end)))

    a=agg[0] if agg else {}

    present=round_half_up(a.get("present_days",0),2)

    pct=round_half_up(present/working*100,4) if working else None

    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":present,"leave_days":a.get("leave_days",0),"late_count":a.get("late_count",0),"total_late_minutes":a.get("total_late_minutes",0),"total_overtime_minutes":a.get("total_overtime_minutes",0),"attendance_pct":pct}

@app.get("/analytics/departments/summary")

def department_summary(month: str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),department: Optional[str]=None):

    start,end=parse_month(month); end_s=end.isoformat(); start_s=start.isoformat()

    match={"joined_on":{"$lte":end_s}}

    if department is not None: match["department"]=department

    pipeline=[{"$match":match},{"$group":{"_id":"$department","headcount":{"$sum":1},"codes":{"$push":"$emp_code"}}},{"$lookup":{"from":"attendance_logs","let":{"codes":"$codes"},"pipeline":[{"$match":{"$expr":{"$and":[{"$in":["$emp_code","$$codes"]},{"$gte":["$date",start_s]},{"$lte":["$date",end_s]}]}}}],"as":"logs"}},{"$project":{"department":"$_id","headcount":1,"logs":1}},{"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},{"$group":{"_id":"$department","headcount":{"$first":"$headcount"},"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$logs.date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$logs.date"}}},6]}]},{"$cond":["$logs.half_day",0.5,1]},0]}},"avg_work_hours":{"$avg":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":["$logs.work_hours",None]}]},"$logs.work_hours",None]}},"late_count":{"$sum":{"$cond":[{"$gt":["$logs.late_minutes",0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$logs.late_minutes",0]}},"leave_count":{"$sum":{"$cond":[{"$eq":["$logs.status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$logs.status","ON_DUTY"]},1,0]}}}},{"$match":{"headcount":{"$gt":0}}},{"$sort":{"_id":1}},{"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":{"$round":["$present_days",2]},"avg_work_hours":{"$round":["$avg_work_hours",2]},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}}]

    items=list(db.employees.aggregate(pipeline))

    # Normalize absent averages to null and round with decimal half-up.

    for x in items:

        if x.get("avg_work_hours") is not None: x["avg_work_hours"]=round_half_up(x["avg_work_hours"],2)

        else: x["avg_work_hours"]=None

        x["present_days"]=round_half_up(x.get("present_days",0),2)

    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit: int=Query(10,ge=1,le=50),department: Optional[str]=None):

    start,end=parse_month(month)

    pipeline=[{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]

    if department is not None: pipeline.append({"$match":{"employee.department":department}})

    pipeline += [{"$set":{"emp_code":"$_id","name":"$employee.name","department":"$employee.department"}},{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"emp_code":1}},{"$project":{"_id":0,"emp_code":1,"name":1,"department":1,"total_late_minutes":1,"late_count":1,"rank":1}}]

    return {"month":month,"items":list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str,from_: str=Query(...,alias="from"),to: str=Query(...)):

    start=parse_date(from_); end=parse_date(to)

    if end<start: raise HTTPException(422,"to must be >= from")

    if (end-start).days+1>92: raise HTTPException(422,"date range cannot exceed 92 days")

    if not db.employees.find_one({"department":department}): raise HTTPException(404,"Unknown department")

    # Dates and moving averages are generated inside MongoDB; no Python per-day loop.

    pipeline=[{"$match":{"department":department}},{"$group":{"_id":"$department","employees":{"$push":{"emp_code":"$emp_code","joined_on":"$joined_on"}}}},{"$set":{"dates":{"$map":{"input":{"$range":[0,{"$add":[{"$dateDiff":{"startDate":{"$dateFromString":{"dateString":start.isoformat()}},"endDate":{"$dateFromString":{"dateString":end.isoformat()}},"unit":"day"}},1]}]},"as":"n","in":{"$dateToString":{"date":{"$dateAdd":{"startDate":{"$dateFromString":{"dateString":start.isoformat()}},"unit":"day","amount":"$$n"}},"format":"%Y-%m-%d"}}}}}},{"$unwind":"$dates"},{"$set":{"headcount":{"$size":{"$filter":{"input":"$employees","as":"e","cond":{"$lte":["$$e.joined_on","$dates"]}}}},"day_date":{"$dateFromString":{"dateString":"$dates"}}}},{"$lookup":{"from":"attendance_logs","let":{"d":"$dates","codes":{"$map":{"input":"$employees","as":"e","in":"$$e.emp_code"}},"dept":"$_id"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$date","$$d"]},{"$in":["$emp_code","$$codes"]}]}}}],"as":"logs"}},{"$set":{"is_working_day":{"$and":[{"$gte":[{"$dayOfWeek":"$day_date"},2]},{"$lte":[{"$dayOfWeek":"$day_date"},6]}]},"present_count":{"$sum":{"$map":{"input":{"$filter":{"input":"$logs","as":"l","cond":{"$in":["$$l.status",list(PRESENCE)]}}},"as":"l","in":{"$cond":["$$l.half_day",0.5,1]}}}},"late_count":{"$size":{"$filter":{"input":"$logs","as":"l","cond":{"$gt":["$$l.late_minutes",0]}}}}}},{"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$divide":["$present_count","$headcount"]},None]}}},{"$setWindowFields":{"sortBy":{"dates":1},"output":{"moving_avg_raw":{"$avg":"$attendance_rate","window":{"documents":[-6,"current"]}}}}},{"$sort":{"dates":1}},{"$project":{"_id":0,"date":"$dates","is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":{"$cond":[{"$ne":["$attendance_rate",None]},{"$round":["$attendance_rate",4]},None]},"moving_avg_7d":{"$cond":[{"$ne":["$moving_avg_raw",None]},{"$round":["$moving_avg_raw",4]},None]}}}]

    items=list(db.employees.aggregate(pipeline))

    return {"department":department,"items":items}

@app.get("/admin/explain/{endpoint}")

def explain(

    endpoint: str,

    emp_code: Optional[str] = None,

    month: Optional[str] = None,

    department: Optional[str] = None,

    limit: int = Query(10, ge=1, le=50),

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[str] = None,

    from_: Optional[str] = Query(None, alias="from"),

    to: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    allowed = {

        "attendance_list",

        "employee_monthly",

        "department_summary",

        "late_leaderboard",

        "department_trend",

    }

    if endpoint not in allowed:

        raise HTTPException(422, "unknown endpoint")

    # Explain the same find query used by GET /attendance.

    if endpoint == "attendance_list":

        q = {}

        if emp_code:

            q["emp_code"] = emp_code

        if date_from:

            parse_date(date_from)

            q.setdefault("date", {})["$gte"] = date_from

        if date_to:

            parse_date(date_to)

            q.setdefault("date", {})["$lte"] = date_to

        if date_from and date_to and date_from > date_to:

            raise HTTPException(422, "date_from must be <= date_to")

        if status:

            if status not in STATUSES:

                raise HTTPException(422, "invalid status")

            q["status"] = status

        exp = db.command(

            "explain",

            {

                "find": "attendance_logs",

                "filter": q,

                "sort": {"date": -1, "emp_code": 1},

                "skip": (page - 1) * page_size,

                "limit": page_size,

            },

            verbosity="executionStats",

        )

        return {

            "endpoint": endpoint,

            "collection": "attendance_logs",

            "explain": jsonable_explain(exp),

        }

    if endpoint == "department_trend":
        start = end = None
        start_s = end_s = None
    else:
        if not month:
            raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        start_s, end_s = start.isoformat(), end.isoformat()

    # Reuse the actual employee-monthly aggregation helper.

    if endpoint == "employee_monthly":

        if not emp_code:

            raise HTTPException(422, "emp_code is required")

        pipeline = month_log_pipeline(emp_code, start, end)

        coll = "attendance_logs"

    # Mirror GET /analytics/departments/summary.

    elif endpoint == "department_summary":

        match = {"joined_on": {"$lte": end_s}}

        if department is not None:

            match["department"] = department

        pipeline = [

            {

                "$match": match,

            },

            {

                "$group": {

                    "_id": "$department",

                    "headcount": {"$sum": 1},

                    "codes": {"$push": "$emp_code"},

                },

            },

            {

                "$lookup": {

                    "from": "attendance_logs",

                    "let": {"codes": "$codes"},

                    "pipeline": [

                        {

                            "$match": {

                                "$expr": {

                                    "$and": [

                                        {"$in": ["$emp_code", "$$codes"]},

                                        {"$gte": ["$date", start_s]},

                                        {"$lte": ["$date", end_s]},

                                    ]

                                }

                            }

                        }

                    ],

                    "as": "logs",

                }

            },

            {

                "$project": {

                    "department": "$_id",

                    "headcount": 1,

                    "logs": 1,

                }

            },

            {

                "$unwind": {

                    "path": "$logs",

                    "preserveNullAndEmptyArrays": True,

                }

            },

            {

                "$group": {

                    "_id": "$department",

                    "headcount": {"$first": "$headcount"},

                    "present_days": {

                        "$sum": {

                            "$cond": [

                                {

                                    "$and": [

                                        {"$in": ["$logs.status", list(PRESENCE)]},

                                        {

                                            "$gte": [

                                                {

                                                    "$dayOfWeek": {

                                                        "$dateFromString": {

                                                            "dateString": "$logs.date"

                                                        }

                                                    }

                                                },

                                                2,

                                            ]

                                        },

                                        {

                                            "$lte": [

                                                {

                                                    "$dayOfWeek": {

                                                        "$dateFromString": {

                                                            "dateString": "$logs.date"

                                                        }

                                                    }

                                                },

                                                6,

                                            ]

                                        },

                                    ]

                                },

                                {"$cond": ["$logs.half_day", 0.5, 1]},

                                0,

                            ]

                        }

                    },

                    "avg_work_hours": {

                        "$avg": {

                            "$cond": [

                                {

                                    "$and": [

                                        {"$in": ["$logs.status", list(PRESENCE)]},

                                        {"$ne": ["$logs.work_hours", None]},

                                    ]

                                },

                                "$logs.work_hours",

                                None,

                            ]

                        }

                    },

                    "late_count": {

                        "$sum": {

                            "$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]

                        }

                    },

                    "total_late_minutes": {

                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}

                    },

                    "leave_count": {

                        "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}

                    },

                    "on_duty_count": {

                        "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}

                    },

                }

            },

            {"$match": {"headcount": {"$gt": 0}}},

            {"$sort": {"_id": 1}},

            {

                "$project": {

                    "_id": 0,

                    "department": "$_id",

                    "headcount": 1,

                    "present_days": {"$round": ["$present_days", 2]},

                    "avg_work_hours": {"$round": ["$avg_work_hours", 2]},

                    "late_count": 1,

                    "total_late_minutes": 1,

                    "leave_count": 1,

                    "on_duty_count": 1,

                }

            },

        ]

        coll = "employees"

    # Mirror GET /analytics/leaderboard/late.

    elif endpoint == "late_leaderboard":

        pipeline = [

            {

                "$match": {

                    "date": {"$gte": start_s, "$lte": end_s},

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

                    "as": "employee",

                }

            },

            {"$unwind": "$employee"},

        ]

        if department is not None:

            pipeline.append({"$match": {"employee.department": department}})

        pipeline += [

            {

                "$set": {

                    "emp_code": "$_id",

                    "name": "$employee.name",

                    "department": "$employee.department",

                }

            },

            {

                "$setWindowFields": {

                    "sortBy": {"total_late_minutes": -1},

                    "output": {"rank": {"$rank": {}}},

                }

            },

            {"$match": {"rank": {"$lte": limit}}},

            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},

            {

                "$project": {

                    "_id": 0,

                    "emp_code": 1,

                    "name": 1,

                    "department": 1,

                    "total_late_minutes": 1,

                    "late_count": 1,

                    "rank": 1,

                }

            },

        ]

        coll = "attendance_logs"

    # Mirror GET /analytics/departments/{department}/trend.

    else:

        if not department or not from_ or not to:

            raise HTTPException(

                422, "department, from and to are required"

            )

        a, b = parse_date(from_), parse_date(to)

        if b < a or (b - a).days + 1 > 92:

            raise HTTPException(422, "invalid trend range")

        if not db.employees.find_one({"department": department}):

            raise HTTPException(404, "Unknown department")

        pipeline = [

            {"$match": {"department": department}},

            {

                "$group": {

                    "_id": "$department",

                    "employees": {

                        "$push": {

                            "emp_code": "$emp_code",

                            "joined_on": "$joined_on",

                        }

                    },

                }

            },

            {

                "$set": {

                    "dates": {

                        "$map": {

                            "input": {

                                "$range": [

                                    0,

                                    {

                                        "$add": [

                                            {

                                                "$dateDiff": {

                                                    "startDate": {

                                                        "$dateFromString": {

                                                            "dateString": a.isoformat()

                                                        }

                                                    },

                                                    "endDate": {

                                                        "$dateFromString": {

                                                            "dateString": b.isoformat()

                                                        }

                                                    },

                                                    "unit": "day",

                                                }

                                            },

                                            1,

                                        ]

                                    },

                                ]

                            },

                            "as": "n",

                            "in": {

                                "$dateToString": {

                                    "date": {

                                        "$dateAdd": {

                                            "startDate": {

                                                "$dateFromString": {

                                                    "dateString": a.isoformat()

                                                }

                                            },

                                            "unit": "day",

                                            "amount": "$$n",

                                        }

                                    },

                                    "format": "%Y-%m-%d",

                                }

                            },

                        }

                    }

                }

            },

            {"$unwind": "$dates"},

            {

                "$set": {

                    "headcount": {

                        "$size": {

                            "$filter": {

                                "input": "$employees",

                                "as": "e",

                                "cond": {"$lte": ["$$e.joined_on", "$dates"]},

                            }

                        }

                    },

                    "day_date": {"$dateFromString": {"dateString": "$dates"}},

                }

            },

            {

                "$lookup": {

                    "from": "attendance_logs",

                    "let": {

                        "d": "$dates",

                        "codes": {

                            "$map": {

                                "input": "$employees",

                                "as": "e",

                                "in": "$$e.emp_code",

                            }

                        },

                        "dept": "$_id",

                    },

                    "pipeline": [

                        {

                            "$match": {

                                "$expr": {

                                    "$and": [

                                        {"$eq": ["$date", "$$d"]},

                                        {"$in": ["$emp_code", "$$codes"]},

                                    ]

                                }

                            }

                        }

                    ],

                    "as": "logs",

                }

            },

            {

                "$set": {

                    "is_working_day": {

                        "$and": [

                            {"$gte": [{"$dayOfWeek": "$day_date"}, 2]},

                            {"$lte": [{"$dayOfWeek": "$day_date"}, 6]},

                        ]

                    },

                    "present_count": {

                        "$sum": {

                            "$map": {

                                "input": {

                                    "$filter": {

                                        "input": "$logs",

                                        "as": "l",

                                        "cond": {

                                            "$in": ["$$l.status", list(PRESENCE)]

                                        },

                                    }

                                },

                                "as": "l",

                                "in": {"$cond": ["$$l.half_day", 0.5, 1]},

                            }

                        }

                    },

                    "late_count": {

                        "$size": {

                            "$filter": {

                                "input": "$logs",

                                "as": "l",

                                "cond": {"$gt": ["$$l.late_minutes", 0]},

                            }

                        }

                    },

                }

            },

            {

                "$set": {

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

                    }

                }

            },

            {

                "$setWindowFields": {

                    "sortBy": {"dates": 1},

                    "output": {

                        "moving_avg_raw": {

                            "$avg": "$attendance_rate",

                            "window": {"documents": [-6, "current"]},

                        }

                    },

                }

            },

            {"$sort": {"dates": 1}},

            {

                "$project": {

                    "_id": 0,

                    "date": "$dates",

                    "is_working_day": 1,

                    "headcount": 1,

                    "present_count": 1,

                    "late_count": 1,

                    "attendance_rate": {

                        "$cond": [

                            {"$ne": ["$attendance_rate", None]},

                            {"$round": ["$attendance_rate", 4]},

                            None,

                        ]

                    },

                    "moving_avg_7d": {

                        "$cond": [

                            {"$ne": ["$moving_avg_raw", None]},

                            {"$round": ["$moving_avg_raw", 4]},

                            None,

                        ]

                    },

                }

            },

        ]

        coll = "employees"

    exp = db.command(

        "explain",

        {"aggregate": coll, "pipeline": pipeline, "cursor": {}},

        verbosity="executionStats",

    )

    return {

        "endpoint": endpoint,

        "collection": coll,

        "explain": jsonable_explain(exp),

    }