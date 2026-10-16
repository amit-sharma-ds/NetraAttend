"""REST API v1 for other systems (punching machines, payroll, HR software).

Every request needs an API key (created on the admin "API" page) in the header
    X-API-Key: na_...        (or  Authorization: Bearer na_...)
Keys have scopes: "read" (GET endpoints) and "punch" (POST /punches).
Only a SHA-256 hash of each key is stored (collection api_keys).
All times are the office's local time, "YYYY-MM-DD HH:MM:SS".
"""
import hashlib
import secrets
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from functools import wraps

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, g, jsonify, request

import attendance as att
from db import db, now_str

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

SCOPES = ("read", "punch")
RATE_LIMIT = 300            # requests per key per minute
MAX_RANGE_DAYS = 62
MAX_PUNCHES_PER_POST = 500
PUNCH_PAST_DAYS = 60        # machines may send old logs, but not older than this
_hits = {}
_hits_lock = threading.Lock()


# ------------------------------------------------------------------ keys
def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(name, scopes):
    """Returns the plain key - shown once, never stored."""
    scopes = [s for s in scopes if s in SCOPES] or ["read"]
    key = "na_" + secrets.token_urlsafe(32)
    db().api_keys.insert_one({"name": name.strip()[:60] or "API key", "key_hash": _hash(key),
                              "prefix": key[:10], "scopes": scopes, "active": True,
                              "created_at": now_str(), "last_used_at": None, "uses": 0})
    return key


def list_keys():
    return [dict(k, id=str(k["_id"])) for k in db().api_keys.find({}, {"key_hash": 0}).sort("created_at", -1)]


def revoke_key(key_id):
    try:
        return db().api_keys.update_one({"_id": ObjectId(key_id)}, {"$set": {"active": False}}).modified_count
    except InvalidId:
        return 0


def delete_key(key_id):
    """Remove a key from the list for good - only once it is revoked."""
    try:
        return db().api_keys.delete_one({"_id": ObjectId(key_id), "active": False}).deleted_count
    except InvalidId:
        return 0


def error(status, message):
    return jsonify(ok=False, error=message), status


def require(scope):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            key = request.headers.get("X-API-Key", "")
            auth = request.headers.get("Authorization", "")
            if not key and auth.lower().startswith("bearer "):
                key = auth[7:].strip()
            if not key:
                return error(401, "Missing API key. Send it in the X-API-Key header.")
            doc = db().api_keys.find_one({"key_hash": _hash(key), "active": True})
            if doc is None:
                return error(401, "Invalid or revoked API key.")
            if scope not in doc.get("scopes", []):
                return error(403, f"This API key does not have the '{scope}' permission.")
            now = time.monotonic()
            with _hits_lock:
                q = _hits.setdefault(str(doc["_id"]), deque())
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= RATE_LIMIT:
                    return error(429, f"Too many requests - limit is {RATE_LIMIT} per minute.")
                q.append(now)
            db().api_keys.update_one({"_id": doc["_id"]}, {"$set": {"last_used_at": now_str()},
                                                           "$inc": {"uses": 1}})
            g.api_key = doc
            return f(*a, **k)
        return wrapper
    return deco


# ------------------------------------------------------------------ helpers
def _day(value, default=None):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def _moment(value):
    """'2026-10-16', '2026-10-16T10:30', '2026-10-16 10:30:05' -> datetime (or None)."""
    if not value:
        return None
    value = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _range():
    today = date.today()
    start = _day(request.args.get("from"), today)
    end = _day(request.args.get("to"), start)
    if end < start:
        start, end = end, start
    if (end - start).days >= MAX_RANGE_DAYS:
        raise ValueError(f"Date range too long - max {MAX_RANGE_DAYS} days per request.")
    return start, end


def _employees_by_code():
    return {e["emp_code"]: e for e in db().employees.find({}, {"password_hash": 0})}


def _minutes(hours):
    return int(round(float(hours) * 60)) if hours not in ("", None) else None


# ------------------------------------------------------------------ endpoints
@bp.get("/health")
@require("read")
def health():
    eng = current_app.extensions.get("netra_engine")
    cam = eng.camera.status() if eng else {}
    return jsonify(ok=True, time=now_str(), database="ok",
                   camera={"connected": bool(cam.get("connected")), "fps": cam.get("fps")})


@bp.get("/employees")
@require("read")
def employees():
    face = {s["_id"] for s in db().face_samples.aggregate([{"$group": {"_id": "$emp_id"}}])}
    query = {}
    if request.args.get("active") in ("true", "1"):
        query["active"] = True
    out = [{"emp_code": e["emp_code"], "name": e["name"], "department": e.get("department", ""),
            "active": e.get("active", True), "field_allowed": bool(e.get("field_allowed")),
            "face_registered": e["_id"] in face}
           for e in db().employees.find(query, {"password_hash": 0}).sort("name", 1)]
    return jsonify(ok=True, count=len(out), employees=out)


@bp.get("/attendance")
@require("read")
def attendance():
    try:
        start, end = _range()
    except ValueError as exc:
        return error(400, str(exc))
    emp_id = None
    code = request.args.get("emp_code")
    if code:
        emp = db().employees.find_one({"emp_code": code}, {"_id": 1})
        if emp is None:
            return error(404, f"No employee with emp_code '{code}'.")
        emp_id = emp["_id"]
    rows, summary = att.build_report(start, end, emp_id)
    out = []
    for r in sorted(rows, key=lambda x: (x["date"], x["name"])):
        worked = _minutes(r["hours"])
        out.append({"emp_code": r["code"], "name": r["name"], "department": r["department"],
                    "date": r["date"], "in": r["check_in"] or None, "out": r["check_out"] or None,
                    "worked_minutes": worked, "status": r["status"], "late_minutes": r["late_min"],
                    "leave": r.get("leave"), "punches": len(r["moves"]), "source": r["source"] or None})
    worked = {}
    for row in out:   # exact sum of the day values (the report's own total is rounded)
        worked[row["emp_code"]] = worked.get(row["emp_code"], 0) + (row["worked_minutes"] or 0)
    totals = [{"emp_code": s["code"], "name": s["name"], "present": s["present"], "on_time": s["on_time"],
               "late": s["late"], "half_day": s["half_day"], "leave": s["leave"], "absent": s["absent"],
               "late_minutes": s["late_min"], "worked_minutes": worked.get(s["code"], 0)}
              for s in summary]
    return jsonify(ok=True, **{"from": start.isoformat(), "to": end.isoformat()}, days=out, summary=totals)


@bp.get("/punches")
@require("read")
def punches():
    since = _moment(request.args.get("since")) or datetime.combine(date.today(), datetime.min.time())
    until = _moment(request.args.get("until"))
    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 1000))
    except ValueError:
        limit = 500
    query = {"ts": {"$gt": since.strftime("%Y-%m-%d %H:%M:%S")}}
    if until:
        query["ts"]["$lte"] = until.strftime("%Y-%m-%d %H:%M:%S")
    by_id = {e["_id"]: e for e in db().employees.find({}, {"emp_code": 1, "name": 1})}
    code = request.args.get("emp_code")
    if code:
        match = [i for i, e in by_id.items() if e["emp_code"] == code]
        if not match:
            return error(404, f"No employee with emp_code '{code}'.")
        query["emp_id"] = match[0]
    docs = list(db().events.find(query).sort([("ts", 1), ("_id", 1)]).limit(limit))
    if len(docs) == limit:   # never cut a second in half, or a sync would skip punches
        same = db().events.find(dict(query, ts=docs[-1]["ts"], _id={"$nin": [x["_id"] for x in docs]}))
        docs += list(same)
    out = []
    for ev in docs:
        e = by_id.get(ev["emp_id"], {})
        item = {"id": str(ev["_id"]), "emp_code": e.get("emp_code"), "name": e.get("name"),
                "time": ev["ts"], "kind": ev["kind"], "source": ev.get("source", "camera")}
        if ev.get("device"):
            item["device"] = ev["device"]
        if ev.get("source") == "field":
            item.update(lat=ev.get("lat"), lng=ev.get("lng"), accuracy_m=ev.get("accuracy"),
                        face_verified=ev.get("verified"))
        out.append(item)
    return jsonify(ok=True, count=len(out), punches=out,
                   next_since=out[-1]["time"] if out else since.strftime("%Y-%m-%d %H:%M:%S"))


@bp.post("/punches")
@require("punch")
def add_punches():
    """Body: one punch {"emp_code", "time"?, "device"?, "kind"?} or {"punches": [...]}."""
    body = request.get_json(silent=True)
    if body is None:
        return error(400, "Send JSON (Content-Type: application/json).")
    items = body.get("punches") if isinstance(body, dict) and "punches" in body else \
        (body if isinstance(body, list) else [body])
    if not isinstance(items, list) or not items:
        return error(400, "No punches in the request.")
    if len(items) > MAX_PUNCHES_PER_POST:
        return error(400, f"Max {MAX_PUNCHES_PER_POST} punches per request.")
    staff = _employees_by_code()
    now = datetime.now()
    results = []
    for i, p in enumerate(items):
        if not isinstance(p, dict):
            results.append({"index": i, "ok": False, "error": "Each punch must be an object."})
            continue
        code = str(p.get("emp_code", "")).strip()
        emp = staff.get(code)
        when = _moment(str(p["time"])) if p.get("time") else now.replace(microsecond=0)
        kind = str(p.get("kind", "")).upper() or None
        device = str(p.get("device", "") or g.api_key["name"])[:60]
        problem = None
        if emp is None:
            problem = f"Unknown emp_code '{code}'."
        elif not emp.get("active", True):
            problem = f"Employee '{code}' is disabled."
        elif when is None:
            problem = "Bad time - use 'YYYY-MM-DD HH:MM:SS'."
        elif when > now + timedelta(minutes=5):
            problem = "Time is in the future - check the machine's clock."
        elif when < now - timedelta(days=PUNCH_PAST_DAYS):
            problem = f"Time is older than {PUNCH_PAST_DAYS} days."
        elif kind not in (None, "IN", "OUT"):
            problem = "kind must be IN or OUT (or leave it out)."
        if problem:
            results.append({"index": i, "emp_code": code, "ok": False, "error": problem})
            continue
        res = att.record_punch(emp["_id"], when, device, kind)
        results.append({"index": i, "emp_code": code, "ok": True, "time": when.strftime("%Y-%m-%d %H:%M:%S"),
                        "kind": res["kind"], "result": res["result"]})
    saved = sum(1 for r in results if r.get("result") == "saved")
    dup = sum(1 for r in results if r.get("result") == "duplicate")
    failed = sum(1 for r in results if not r["ok"])
    status = 200 if failed < len(results) else 400
    return jsonify(ok=failed == 0, saved=saved, duplicates=dup, failed=failed, results=results), status


@bp.get("/leaves")
@require("read")
def leaves():
    try:
        start, end = _range()
    except ValueError as exc:
        return error(400, str(exc))
    query = {"from": {"$lte": end.isoformat()}, "to": {"$gte": start.isoformat()}}
    if request.args.get("status"):
        query["status"] = request.args["status"]
    by_id = {e["_id"]: e for e in db().employees.find({}, {"emp_code": 1, "name": 1})}
    out = [{"id": str(lv["_id"]), "emp_code": by_id.get(lv["emp_id"], {}).get("emp_code"),
            "name": by_id.get(lv["emp_id"], {}).get("name"), "type": lv["type"], "from": lv["from"],
            "to": lv["to"], "half_day": bool(lv.get("half_day")), "days": lv.get("days"),
            "status": lv["status"], "reason": lv.get("reason", ""), "applied_at": lv.get("applied_at"),
            "decided_at": lv.get("decided_at")}
           for lv in db().leaves.find(query).sort("from", 1)]
    return jsonify(ok=True, count=len(out), leaves=out)


@bp.errorhandler(404)
def not_found(_e):
    return error(404, "Unknown API endpoint - see /api/docs.")


@bp.errorhandler(405)
def bad_method(_e):
    return error(405, "Method not allowed for this endpoint - see /api/docs.")
