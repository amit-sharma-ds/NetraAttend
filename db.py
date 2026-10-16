"""MongoDB storage (Atlas). Everything lives in the database config.MONGO_DB:

  employees     {_id: int, emp_code, name, department, password_hash, active, created_at}
  face_samples  {emp_id, pose, source, crop (jpeg), embedding (float32 x512), model, created_at}
  attendance    {emp_id, date, check_in, check_out, in_photo, out_photo, in_score, source, note}
  events        {emp_id, ts, kind (IN/OUT), score, photo}       - every movement
  photos        {_id: "<date>/<emp>_<kind>.jpg", emp_id, data, created_at}
                attendance photos expire after config.PHOTO_DAYS (TTL index);
                profile photos ("profile/<emp>.jpg") have no created_at and never expire
  leaves        {emp_id, type, from, to, half_day, days, reason, status, applied_at, decided_at, admin_note}
  corrections   {emp_id, date, check_in, check_out, reason, status, applied_at, decided_at, admin_note}
                (status: pending / approved / rejected / cancelled)
  api_keys      {name, key_hash (sha256), prefix, scopes, active, created_at, last_used_at, uses}
  settings      {_id: key, value}
  counters      {_id: "employees", seq}
"""
import ast
import os
import threading
import time
from datetime import datetime, timezone

import config

DEFAULT_SETTINGS = {
    "company_name": "NetraAttend",
    "office_start": "10:30",      # office time
    "grace_min": "20",            # check-in up to office_start + grace = On Time
    "half_day_after": "13:30",    # check-in after this = Half Day
    "half_day_hours": "4",        # (past days) worked less than this = Half Day
    "min_out_gap_min": "30",      # sightings this soon after check-in don't count as check-out
    "weekly_off": "6",            # 0=Mon ... 6=Sun, comma separated
    "holidays": "",               # YYYY-MM-DD, comma or newline separated
    "leave_types": "Casual Leave:12, Sick Leave:6, Unpaid Leave:0",   # name:days per year (0 = no limit)
    "camera_url": "",             # set from the Settings page; empty = config.CAMERA
    "camera_ok_url": "",          # last link that streamed (allows auto-reboot when stuck)
}

_db = None
_lock = threading.Lock()
_settings_cache = (0.0, None)


def apply_env_overrides():
    """NETRA_<NAME>=value overrides config.<NAME> (numbers/tuples parsed)."""
    for key, raw in os.environ.items():
        if not key.startswith("NETRA_"):
            continue
        name = key[len("NETRA_"):]
        if isinstance(getattr(config, name, None), str):
            value = raw          # text settings (passwords, links) stay text, e.g. "12345678"
        else:
            try:
                value = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                value = raw
        setattr(config, name, value)


def _fix_dns():
    """mongodb+srv:// needs a DNS SRV lookup. On some Windows PCs dnspython finds
    no DNS server at all (Windows itself still resolves fine), so fall back to
    public resolvers instead of failing."""
    try:
        import dns.resolver
        resolver = dns.resolver.get_default_resolver()
        if not resolver.nameservers:
            resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
    except Exception:
        import dns.resolver
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
        dns.resolver.default_resolver = resolver


def db():
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                _fix_dns()
                from pymongo import MongoClient
                client = MongoClient(config.MONGO_URI, appname="NetraAttend", retryWrites=True,
                                     serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
                                     socketTimeoutMS=20000)
                _db = client[config.MONGO_DB]
    return _db


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    from pymongo.errors import OperationFailure
    d = db()
    d.command("ping")
    d.employees.create_index("emp_code", unique=True)
    d.face_samples.create_index("emp_id")
    d.attendance.create_index([("emp_id", 1), ("date", 1)], unique=True)
    d.attendance.create_index("date")
    d.events.create_index("ts")
    d.events.create_index("emp_id")
    d.photos.create_index("emp_id")
    d.leaves.create_index([("emp_id", 1), ("from", 1)])
    d.leaves.create_index("status")
    d.corrections.create_index([("emp_id", 1), ("date", 1)])
    d.corrections.create_index("status")
    d.api_keys.create_index("key_hash", unique=True)
    d.events.create_index([("emp_id", 1), ("ts", 1), ("source", 1)])
    ttl =int(getattr(config, "PHOTO_DAYS", 60)) * 86400
    try:
        d.photos.create_index("created_at", expireAfterSeconds=ttl, name="photo_ttl")
    except OperationFailure:   # PHOTO_DAYS changed since the index was made
        d.command("collMod", "photos", index={"name": "photo_ttl", "expireAfterSeconds": ttl})
    for k, v in DEFAULT_SETTINGS.items():
        d.settings.update_one({"_id": k}, {"$setOnInsert": {"value": v}}, upsert=True)


def get_settings():
    """Settings with a 10 s cache (read on every page). If the database is
    unreachable the last known values are used so pages still render."""
    global _settings_cache
    from pymongo.errors import PyMongoError
    at, cached = _settings_cache
    if cached is not None and time.monotonic() - at < 10:
        return dict(cached)
    try:
        out = dict(DEFAULT_SETTINGS)
        out.update({d["_id"]: d["value"] for d in db().settings.find()})
    except PyMongoError:
        return dict(cached or DEFAULT_SETTINGS)
    _settings_cache = (time.monotonic(), out)
    return dict(out)


def save_settings(values):
    global _settings_cache
    for k, v in values.items():
        db().settings.update_one({"_id": k}, {"$set": {"value": str(v)}}, upsert=True)
    _settings_cache = (0.0, None)


def next_employee_id():
    from pymongo import ReturnDocument
    doc = db().counters.find_one_and_update({"_id": "employees"}, {"$inc": {"seq": 1}},
                                            upsert=True, return_document=ReturnDocument.AFTER)
    return int(doc["seq"])


def put_photo(key, jpg_bytes, emp_id, expires=True):
    from bson.binary import Binary
    doc = {"emp_id": emp_id, "data": Binary(jpg_bytes)}
    if expires:
        doc["created_at"] = datetime.now(timezone.utc)
    db().photos.replace_one({"_id": key}, doc, upsert=True)
    return key


def get_photo(key):
    doc = db().photos.find_one({"_id": key}, {"data": 1})
    return bytes(doc["data"]) if doc else None
