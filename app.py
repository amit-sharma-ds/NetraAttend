"""NetraAttend - CCTV face recognition attendance.

Run:  python app.py      (website + camera engine in one process)
Open: http://<this-pc-ip>:5000
"""
import atexit
import hmac
import ipaddress
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit, urlunsplit

import cv2
import numpy as np
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from bson import ObjectId
from bson.binary import Binary
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

import config
from db import (apply_env_overrides, db, get_photo, get_settings, init_db, next_employee_id, put_photo,
                now_str, save_settings)

apply_env_overrides()

import api  # noqa: E402
import attendance as att  # noqa: E402
from camera import mask_url  # noqa: E402
from engine import POSES, Engine  # noqa: E402

# ROLE "all" = office PC (camera + website + API); "web" = cloud server (website + API,
# no camera). Both use the same MongoDB.
ROLE = str(getattr(config, "ROLE", "all")).lower()
WEB_ONLY = ROLE == "web"
app = Flask(__name__)
app.register_blueprint(api.bp)
app.secret_key = config.SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  MAX_CONTENT_LENGTH=60 * 1024 * 1024, TEMPLATES_AUTO_RELOAD=True,
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
engine = None
_failed_logins = {}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ------------------------------------------------------------------ helpers
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def status_class(status):
    return "b-" + str(status).lower().replace(" ", "-")


app.jinja_env.filters["sc"] = status_class


def fmt_minutes(minutes):
    """472 -> '7h 52m', 45 -> '45m', 0 -> '-'."""
    try:
        minutes = int(round(float(minutes)))
    except (TypeError, ValueError):
        return "-"
    if minutes <= 0:
        return "-"
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_hours(hours):
    """5.0 -> '5h 00m', 4.97 -> '4h 58m', '' / 0 -> '-'."""
    try:
        return fmt_minutes(float(hours) * 60)
    except (TypeError, ValueError):
        return "-"


app.jinja_env.filters["hm"] = fmt_minutes
app.jinja_env.filters["hhm"] = fmt_hours


@app.before_request
def check_csrf():
    # login is exempt: a stale/overwritten cookie (e.g. another app on the same
    # IP) must never stop someone from signing in
    # API v1 uses API keys, not the browser session
    if request.method == "POST" and request.endpoint != "login" and not request.path.startswith("/api/v1/"):
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
        if not hmac.compare_digest(token, session.get("csrf", "")):
            print(f"CSRF check failed: {request.method} {request.path} from {request.remote_addr}",
                  flush=True)
            abort(400, "Session expired - reload the page and try again.")


@app.context_processor
def inject():
    pending = 0
    if session.get("admin"):
        try:
            pending = (db().leaves.count_documents({"status": "pending"})
                       + db().corrections.count_documents({"status": "pending"}))
        except Exception:
            pending = 0
    return {"company": get_settings()["company_name"], "is_admin": bool(session.get("admin")),
            "emp_name": session.get("emp_name"), "endpoint": request.endpoint,
            "pending_requests": pending}


def client_ip():
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def is_external():
    """True when the request came from the internet (through a tunnel / proxy or a
    public IP) rather than from the office network."""
    if request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For"):
        return True
    try:
        ip = ipaddress.ip_address(request.remote_addr or "127.0.0.1")
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback)


def admin_blocked():
    """Admin pages stay inside the office network once the app is on the internet
    for field staff (config.ADMIN_LAN_ONLY)."""
    return getattr(config, "ADMIN_LAN_ONLY", True) and is_external()


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, msg="Login required"), 401
            return redirect(url_for("login"))
        if admin_blocked():
            abort(403, "Admin pages open only on the office network.")
        return f(*a, **k)
    return wrapper


def employee_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("emp_id"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapper


def parse_day(value, default):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def get_employee(emp_id):
    emp = db().employees.find_one({"_id": emp_id})
    return dict(emp, id=emp["_id"]) if emp else None


def report_range():
    today = date.today()
    start = parse_day(request.args.get("start"), today.replace(day=1))
    end = parse_day(request.args.get("end"), today)
    if end < start:
        start, end = end, start
    return start, end


# ------------------------------------------------------------------ auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        fails, until = _failed_logins.get(ip, (0, 0.0))
        if until > time.time():
            flash(f"Too many attempts. Try again in {int(until - time.time()) // 60 + 1} min.", "error")
            return render_template("login.html")
        user = request.form.get("u", "").strip()
        pwd = request.form.get("p", "")
        # phones auto-capitalise the first letter and keyboards add stray spaces
        user_ok = hmac.compare_digest(user.lower(), str(config.ADMIN_USER).lower())
        pwd_ok = hmac.compare_digest(pwd.strip(), str(config.ADMIN_PASSWORD))
        if user_ok and pwd_ok and admin_blocked():
            flash("Admin login works only on the office network.", "error")
            return render_template("login.html")
        if user_ok and pwd_ok:
            _failed_logins.pop(ip, None)
            session.clear()
            session.permanent = True
            session["admin"] = True
            return redirect(url_for("dashboard"))
        emp = db().employees.find_one({"emp_code": user, "active": True})
        if emp and emp.get("password_hash") and check_password_hash(emp["password_hash"], pwd):
            _failed_logins.pop(ip, None)
            session.clear()
            session.permanent = True
            session["emp_id"], session["emp_name"] = emp["_id"], emp["name"]
            return redirect(url_for("my"))
        fails += 1
        _failed_logins[ip] = (0, time.time() + 120) if fails >= 10 else (fails, 0.0)
        print(f"[{datetime.now():%H:%M:%S}] Login failed from {ip} for user {user!r}", flush=True)
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ dashboard & live
@app.route("/")
def dashboard():
    if session.get("emp_id"):
        return redirect(url_for("my"))
    if not session.get("admin"):
        return redirect(url_for("login"))
    return render_template("dashboard.html", emps=att.employees({"active": True}),
                           today=date.today().isoformat())


@app.route("/live")
@admin_required
def live():
    return render_template("live.html", has_out=bool(engine.out_camera_url()))


def still_jpg(line1, line2):
    img = np.full((360, 640, 3), 30, np.uint8)
    cv2.putText(img, line1, (60, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(img, line2, (100, 215), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (170, 170, 170), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", img)[1].tobytes()


def still_image(line1, line2):
    return Response(still_jpg(line1, line2), mimetype="image/jpeg")


# The cloud server cannot reach the office camera. While a live view is open there it
# asks the office PC for it through MongoDB (collection "live"), and the office PC
# writes its newest frames and camera status there until nobody is watching.
LIVE_FPS = 2          # frames per second sent to the cloud
LIVE_WIDTH = 640      # smaller than on the LAN: every frame goes over the internet twice
_live_asked = 0.0


def live_fresh(seconds):
    """Filter: written in the last few seconds, by the database's clock (the office
    PC's and the cloud server's clocks never have to agree)."""
    return {"$expr": {"$gt": ["$at", {"$subtract": ["$$NOW", int(seconds * 1000)]}]}}


def live_put(key, **values):
    values = {k: {"$literal": v} for k, v in values.items()}
    db().live.update_one({"_id": key}, [{"$set": dict(values, at="$$NOW")}], upsert=True)


def live_ask():
    """Cloud: tell the office PC that someone is watching (a write every 5 s is enough)."""
    global _live_asked
    if time.monotonic() - _live_asked > 5:
        _live_asked = time.monotonic()
        live_put("watch")


def live_stats():
    """Camera status and recognitions; on the cloud server they come from the office PC."""
    if not WEB_ONLY:
        return engine.stats()
    try:
        live_ask()
        doc = db().live.find_one(dict({"_id": "status"}, **live_fresh(10)))
    except PyMongoError:
        doc = None
    return doc["stats"] if doc else engine.stats()


def cloud_frames(key):
    """Cloud: the office PC's live view as an MJPEG stream."""
    seq, last_new, last_sent = None, time.monotonic(), 0.0
    jpg = still_jpg("Connecting to the office PC...", "Live view comes from there")
    while True:
        if jpg is not None:
            last_sent = time.monotonic()
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
        time.sleep(0.5 / LIVE_FPS)
        jpg = None
        try:
            live_ask()
            doc = db().live.find_one(dict({"_id": key, "seq": {"$ne": seq}}, **live_fresh(10)))
        except PyMongoError:
            doc = None
        now = time.monotonic()
        if doc is not None:
            seq, last_new, jpg = doc["seq"], now, bytes(doc["jpg"])
        elif now - last_new > 15 and now - last_sent > 2:
            jpg = still_jpg("Office PC is not sending video", "Check that it is on and online")


def live_sender():
    """Office PC: while a live view is open on the cloud server, send it the live view
    and the camera status through MongoDB."""
    def shrink(jpg):
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return jpg
        if img.shape[1] > LIVE_WIDTH:
            img = cv2.resize(img, (LIVE_WIDTH, img.shape[0] * LIVE_WIDTH // img.shape[1]),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        return buf.tobytes() if ok else jpg

    def loop():
        sent = {}
        watched, last_check, last_stats = False, 0.0, 0.0
        while True:
            time.sleep(1.0 / LIVE_FPS)
            try:
                now = time.monotonic()
                if now - last_check >= 3:
                    last_check = now
                    watched = db().live.find_one(dict({"_id": "watch"}, **live_fresh(15)),
                                                 {"_id": 1}) is not None
                if not watched:
                    sent.clear()
                    continue
                if now - last_stats >= 2:
                    last_stats = now
                    live_put("status", stats=engine.stats())
                for key, eng in (("frame_in", engine), ("frame_out", engine.out)):
                    if eng is None:
                        continue
                    eng.streamer.last_client = now    # keep its live view rendering
                    with eng.streamer.cond:
                        jpg, seq = eng.streamer.jpg, eng.streamer.seq
                    # skip the frame that was there before watching started (may be old)
                    if jpg is None or sent.setdefault(key, seq) == seq:
                        continue
                    sent[key] = seq
                    live_put(key, jpg=Binary(shrink(jpg)), seq=seq)
            except Exception as exc:
                print(f"Live view for the cloud: {exc}", flush=True)
                time.sleep(10)

    threading.Thread(target=loop, name="live-to-cloud", daemon=True).start()


@app.route("/video_feed")
@admin_required
def video_feed():
    if WEB_ONLY:   # the camera is on the office network; the office PC sends its view
        return Response(cloud_frames("frame_in"), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})
    return Response(engine.streamer.frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


@app.route("/video_feed/out")
@admin_required
def video_feed_out():
    out = engine.out
    if WEB_ONLY:
        return Response(cloud_frames("frame_out"), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})
    if out is None:
        return still_image("No OUT camera", "Add one in Settings")
    return Response(out.streamer.frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


@app.route("/api/today")
@admin_required
def api_today():
    data = att.today_overview()
    data["engine"] = live_stats()
    return jsonify(data)


@app.route("/api/status")
@admin_required
def api_status():
    return jsonify(live_stats())


@app.route("/attendance/manual", methods=["POST"])
@admin_required
def manual_attendance():
    try:
        emp_id = int(request.form["emp_id"])
        day = parse_day(request.form.get("date"), None)
        if day is None:
            raise ValueError("Pick a valid date.")
        att.manual_entry(emp_id, day.isoformat(), request.form.get("check_in", "").strip(),
                         request.form.get("check_out", "").strip(),
                         request.form.get("note", "").strip()[:200])
        flash("Attendance updated.", "success")
    except (KeyError, ValueError) as exc:
        flash(f"Could not save: {exc}", "error")
    return redirect(request.form.get("next") or url_for("dashboard"))


# ------------------------------------------------------------------ employees
@app.route("/employees")
@admin_required
def employees():
    stats = {s["_id"]: s for s in db().face_samples.aggregate([
        {"$group": {"_id": "$emp_id", "samples": {"$sum": 1}, "poses": {"$addToSet": "$pose"}}}])}
    has_pwd = {e["_id"]: bool(e.get("password_hash"))
               for e in db().employees.find({}, {"password_hash": 1})}
    today = {r["emp_id"]: r for r in att.today_overview()["rows"]}
    rows = []
    for e in att.employees():
        s = stats.get(e["id"], {})
        t = today.get(e["id"], {})
        rows.append(dict(e, samples=s.get("samples", 0), poses=sorted(s.get("poses", [])),
                         has_password=has_pwd.get(e["id"], False), today_status=t.get("status", ""),
                         today_in=t.get("check_in", ""), late_min=t.get("late_min", 0)))
    rows.sort(key=lambda e: (not e.get("active", True), e["name"].lower()))
    active = [e for e in rows if e.get("active", True)]
    counts = {"total": len(rows), "active": len(active),
              "registered": sum(1 for e in active if e["samples"]),
              "not_registered": sum(1 for e in active if not e["samples"]),
              "field": sum(1 for e in active if e.get("field_allowed")),
              "in_today": sum(1 for e in active if e["today_in"]),
              "no_password": sum(1 for e in active if not e["has_password"])}
    return render_template("employees.html", emps=rows, counts=counts)


@app.route("/employees/add", methods=["POST"])
@admin_required
def employee_add():
    name = request.form.get("name", "").strip()
    code = request.form.get("emp_code", "").strip()
    dept = request.form.get("department", "").strip()
    pwd = request.form.get("password", "")
    if not name or not code:
        flash("Name and Employee ID are required.", "error")
        return redirect(url_for("employees"))
    if db().employees.find_one({"emp_code": code}, {"_id": 1}):
        flash(f"Employee ID '{code}' already exists.", "error")
        return redirect(url_for("employees"))
    emp_id = next_employee_id()
    try:
        db().employees.insert_one({
            "_id": emp_id, "emp_code": code, "name": name, "department": dept,
            "password_hash": generate_password_hash(pwd) if pwd else None,
            "field_allowed": request.form.get("field_allowed") == "1",
            "active": True, "created_at": now_str()})
    except DuplicateKeyError:
        flash(f"Employee ID '{code}' already exists.", "error")
        return redirect(url_for("employees"))
    flash(f"{name} added. Now register their face.", "success")
    return redirect(url_for("enroll_page", emp_id=emp_id))


@app.route("/employees/<int:emp_id>/edit", methods=["POST"])
@admin_required
def employee_edit(emp_id):
    name = request.form.get("name", "").strip()
    code = request.form.get("emp_code", "").strip()
    dept = request.form.get("department", "").strip()
    pwd = request.form.get("password", "")
    if not name or not code:
        flash("Name and Employee ID are required.", "error")
        return redirect(url_for("employees"))
    changes = {"name": name, "emp_code": code, "department": dept,
               "field_allowed": request.form.get("field_allowed") == "1"}
    if pwd:
        changes["password_hash"] = generate_password_hash(pwd)
    try:
        db().employees.update_one({"_id": emp_id}, {"$set": changes})
        flash(f"{name} updated.", "success")
    except DuplicateKeyError:
        flash(f"Employee ID '{code}' already exists.", "error")
    engine.load_gallery()
    return redirect(url_for("employees"))


@app.route("/employees/<int:emp_id>/toggle", methods=["POST"])
@admin_required
def employee_toggle(emp_id):
    emp = get_employee(emp_id)
    if emp:
        db().employees.update_one({"_id": emp_id}, {"$set": {"active": not emp.get("active", True)}})
    engine.load_gallery()
    return redirect(url_for("employees"))


@app.route("/employees/<int:emp_id>/delete", methods=["POST"])
@admin_required
def employee_delete(emp_id):
    d = db()
    d.employees.delete_one({"_id": emp_id})
    for coll in (d.face_samples, d.attendance, d.events, d.photos):
        coll.delete_many({"emp_id": emp_id})
    engine.load_gallery()
    flash("Employee and their attendance history deleted.", "success")
    return redirect(url_for("employees"))


@app.route("/employees/<int:emp_id>/enroll")
@admin_required
def enroll_page(emp_id):
    emp = get_employee(emp_id)
    if emp is None:
        abort(404)
    samples = {r["_id"]: r["n"] for r in db().face_samples.aggregate([
        {"$match": {"emp_id": emp_id}}, {"$group": {"_id": "$pose", "n": {"$sum": 1}}}])}
    return render_template("enroll.html", emp=emp, samples=samples, poses=POSES)


POSE_ORDER = ["front", "left", "right", "up", "down", "other"]


@app.route("/employees/<int:emp_id>/faces")
@admin_required
def face_photos(emp_id):
    emp = get_employee(emp_id)
    if emp is None:
        abort(404)
    groups = {}
    for s in db().face_samples.find({"emp_id": emp_id}, {"crop": 0, "embedding": 0}).sort("_id", 1):
        s["id"] = str(s["_id"])
        groups.setdefault(s.get("pose") or "other", []).append(s)
    ordered = [(p, groups[p]) for p in POSE_ORDER if p in groups] + \
              [(p, v) for p, v in groups.items() if p not in POSE_ORDER]
    return render_template("faces.html", emp=emp, groups=ordered,
                           total=sum(len(v) for v in groups.values()))


@app.route("/face/<sample_id>")
@admin_required
def face_image(sample_id):
    try:
        oid = ObjectId(sample_id)
    except InvalidId:
        abort(404)
    doc = db().face_samples.find_one({"_id": oid}, {"crop": 1})
    if doc is None:
        abort(404)
    return Response(bytes(doc["crop"]), mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.route("/api/enroll/<int:emp_id>/start", methods=["POST"])
@admin_required
def enroll_start(emp_id):
    emp = get_employee(emp_id)
    if emp is None:
        return jsonify(ok=False, msg="Employee not found"), 404
    if WEB_ONLY:
        return jsonify(ok=False, msg="Camera registration works on the office PC's website. "
                                     "Here you can register with photo upload (Option 2).")
    replace = bool((request.get_json(silent=True) or {}).get("replace", True))
    engine.start_enroll(emp_id, emp["name"], replace)
    return jsonify(ok=True)


@app.route("/api/enroll/status")
@admin_required
def enroll_status():
    return jsonify(engine.enroll_status())


@app.route("/api/enroll/skip", methods=["POST"])
@admin_required
def enroll_skip():
    engine.skip_enroll_step()
    return jsonify(ok=True)


@app.route("/api/enroll/cancel", methods=["POST"])
@admin_required
def enroll_cancel():
    engine.cancel_enroll()
    return jsonify(ok=True)


@app.route("/api/enroll/<int:emp_id>/save", methods=["POST"])
@admin_required
def enroll_save(emp_id):
    status = engine.enroll_status()
    if not status.get("active") or status.get("emp_id") != emp_id:
        return jsonify(ok=False, msg="No capture running for this employee.")
    if not status.get("can_save"):
        return jsonify(ok=False, msg="Not enough face samples yet - keep following the instructions.")
    samples, replace = engine.finish_enroll(emp_id)
    ok, msg = engine.save_samples(emp_id, samples, "cctv", replace)
    return jsonify(ok=ok, msg=msg)


@app.route("/api/enroll/<int:emp_id>/upload", methods=["POST"])
@admin_required
def enroll_upload(emp_id):
    if get_employee(emp_id) is None:
        return jsonify(ok=False, msg="Employee not found"), 404
    files = request.files.getlist("photos")
    if not files:
        return jsonify(ok=False, msg="Choose at least one photo.")
    replace = request.form.get("mode") == "replace"
    samples, notes = [], []
    for f in files[:30]:
        data = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
        if img is None:
            notes.append(f"{f.filename}: not an image")
            continue
        res = engine.faces_from_image(img)
        if isinstance(res, str):
            notes.append(f"{f.filename}: {res}")
        else:
            samples.append(res)
            notes.append(f"{f.filename}: OK ({res['pose']})")
    if not samples:
        return jsonify(ok=False, msg="No usable face found in the photos.", notes=notes)
    ok, msg = engine.save_samples(emp_id, samples, "upload", replace)
    return jsonify(ok=ok, msg=msg, notes=notes)


@app.route("/photo/<int:emp_id>")
def employee_photo(emp_id):
    if not session.get("admin") and session.get("emp_id") != emp_id:
        abort(403)
    jpg = get_photo(f"profile/{emp_id}.jpg")
    if jpg:
        return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" '
           'fill="#e2e8f0"/><circle cx="20" cy="15" r="7" fill="#94a3b8"/><path d="M6 36c2-8 8-11 14-11'
           's12 3 14 11" fill="#94a3b8"/></svg>')
    return Response(svg, mimetype="image/svg+xml")


@app.route("/photos")
@admin_required
def photos_page():
    """Every attendance photo stored in MongoDB (photos collection) for a day."""
    day = parse_day(request.args.get("date"), date.today()).isoformat()
    d = db()
    names = {e["_id"]: e["name"] for e in d.employees.find({}, {"name": 1})}
    stored = {p["_id"] for p in d.photos.find({"_id": {"$regex": f"^{day}/"}}, {"_id": 1})}
    items = []
    for ev in d.events.find({"ts": {"$gte": f"{day} 00:00:00", "$lte": f"{day} 23:59:59"}}).sort("ts", 1):
        if ev.get("photo") in stored:
            items.append({"photo": ev["photo"], "name": names.get(ev["emp_id"], f"#{ev['emp_id']}"),
                          "kind": ev["kind"], "time": ev["ts"][11:16]})
    shown = {i["photo"] for i in items}
    for key in sorted(stored - shown):    # e.g. an older OUT photo kept for the day
        emp = int(key.split("/")[1].split("_")[0])
        items.append({"photo": key, "name": names.get(emp, f"#{emp}"), "kind": "PHOTO", "time": ""})
    stats = d.command("collStats", "photos")
    days = sorted({p["_id"][:10] for p in d.photos.find({"_id": {"$regex": r"^\d{4}-"}}, {"_id": 1})},
                  reverse=True)[:30]
    return render_template("photos.html", day=day, items=items, days=days, total=stats["count"],
                           size_kb=round(stats["size"] / 1024), keep_days=config.PHOTO_DAYS)


@app.route("/unknown")
@admin_required
def unknown_page():
    """Photos of people the camera could not recognise (new visitors, covered faces),
    stored by the engine as unknown/<date>/<HHMMSSmmm>[_out].jpg."""
    day = parse_day(request.args.get("date"), date.today()).isoformat()
    d = db()
    items = []
    for p in d.photos.find({"_id": {"$regex": f"^unknown/{day}/"}}, {"_id": 1}).sort("_id", -1):
        name = p["_id"].rsplit("/", 1)[1][:-4]
        items.append({"photo": p["_id"], "time": f"{name[:2]}:{name[2:4]}:{name[4:6]}",
                      "out": name.endswith("_out")})
    days = sorted({p["_id"][8:18] for p in d.photos.find({"_id": {"$regex": "^unknown/"}}, {"_id": 1})},
                  reverse=True)[:30]
    return render_template("unknown.html", day=day, items=items, days=days,
                           has_out=bool(engine.out_camera_url()), keep_days=config.PHOTO_DAYS)


@app.route("/snap/<path:rel>")
def snapshot(rel):
    if not session.get("admin") or admin_blocked():
        own = rf"^[\d-]+/{session.get('emp_id')}_(in|out|\d{{6}}|field_\d{{6}})\.jpg$"
        if not session.get("emp_id") or not re.match(own, rel):
            abort(403)
    jpg = get_photo(rel)
    if jpg is None:
        abort(404, "Photo not found (photos are kept for a limited number of days).")
    return Response(jpg, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ reports
@app.route("/reports")
@admin_required
def reports():
    start, end = report_range()
    emp_id = request.args.get("emp", type=int)
    rows, summary = att.build_report(start, end, emp_id)
    return render_template("reports.html", rows=rows, summary=summary, start=start.isoformat(),
                           end=end.isoformat(), emps=att.employees(), emp_id=emp_id)


@app.route("/reports/excel")
@admin_required
def reports_excel():
    start, end = report_range()
    emp_id = request.args.get("emp", type=int)
    rows, summary = att.build_report(start, end, emp_id)
    buf = att.export_excel(rows, summary, start, end, get_settings()["company_name"])
    return send_file(buf, as_attachment=True, download_name=f"attendance_{start}_to_{end}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------ settings
@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        f = request.form
        errors = []
        values = {"company_name": f.get("company_name", "").strip()[:60] or "NetraAttend"}
        for key in ("office_start", "half_day_after"):
            v = f.get(key, "").strip()
            if not HM_RE.match(v):
                errors.append(f"{key.replace('_', ' ')} must be HH:MM (24-hour)")
            values[key] = v
        for key, lo, hi in (("grace_min", 0, 240), ("min_out_gap_min", 0, 720)):
            try:
                n = int(f.get(key, ""))
                if not lo <= n <= hi:
                    raise ValueError
                values[key] = n
            except ValueError:
                errors.append(f"{key.replace('_', ' ')} must be a number {lo}-{hi}")
        try:
            values["half_day_hours"] = float(f.get("half_day_hours", ""))
        except ValueError:
            errors.append("half day hours must be a number")
        values["leave_types"] = ", ".join(
            f"{name}:{quota:g}" for name, quota in att.leave_types({"leave_types": f.get("leave_types", "")}))
        values["weekly_off"] = ",".join(d for d in f.getlist("weekly_off") if d.isdigit())
        values["holidays"] = ", ".join(sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", f.get("holidays", "")))))
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            save_settings(values)
            engine.recorder.invalidate_rules()
            flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    s = get_settings()
    off = {int(x) for x in s["weekly_off"].split(",") if x.strip().isdigit()}
    cam = engine.camera_url()
    out_cam = engine.out_camera_url()
    shown_out = out_cam or cam   # not set yet: start from the IN camera's link - only the IP needs changing
    return render_template("settings.html", s=s, off=off, weekdays=WEEKDAYS, stats=engine.stats(),
                           camera=mask_url(cam), camera_ip=camera_host(cam), db_name=config.MONGO_DB,
                           out_set=bool(out_cam), out_camera=mask_url(shown_out),
                           out_camera_ip=camera_host(shown_out))


def camera_host(url):
    try:
        return urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""


def with_host(url, host):
    """Same RTSP link with another camera IP (user, password, port and path kept)."""
    parts = urlsplit(url)
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (userinfo + "@" if userinfo else "") + host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@app.route("/settings/camera", methods=["POST"])
@admin_required
def camera_settings():
    current = str(engine.camera_url())
    ip = request.form.get("camera_ip", "").strip()
    link = request.form.get("camera_link", "").strip()
    if ip == request.form.get("shown_ip", "").strip():
        ip = ""    # IP box not touched: the link decides (editing only the link must not be undone)
    keep = url_for("settings", keep="in") + "#camera"   # failed: the page shows what was typed again
    new = current
    if link and link != mask_url(current):
        saved_pwd = re.search(r"://[^:/@]+:([^@]*)@", current)
        if ":***@" in link and saved_pwd:   # password left masked: keep the saved one
            link = link.replace(":***@", f":{saved_pwd.group(1)}@", 1)
        new = link
    if ip and ip != camera_host(new):
        if not re.fullmatch(r"[A-Za-z0-9.\-]+", ip):
            flash("Camera IP looks wrong, e.g. 192.168.1.181", "error")
            return redirect(keep)
        if not new.startswith("rtsp://"):
            flash("To change only the IP, the camera link must be an rtsp:// link.", "error")
            return redirect(keep)
        new = with_host(new, ip)
    if not (new.startswith(("rtsp://", "rtsps://", "http://", "https://")) or new.isdigit()):
        flash("Camera link must start with rtsp://", "error")
        return redirect(keep)
    if new == current:
        flash("Camera unchanged.", "info")
        return redirect(url_for("settings") + "#camera")
    save_settings({"camera_url": new})
    if WEB_ONLY:
        flash(f"Camera link saved ({mask_url(new)}). The office PC switches to it within a minute.", "success")
    else:
        engine.set_camera(new)
        flash(f"Camera changed to {mask_url(new)} - connecting...", "success")
    return redirect(url_for("settings") + "#camera")


def same_camera(a, b):
    """Two links to the same camera stream (host + port + path)."""
    try:
        pa, pb = urlsplit(str(a)), urlsplit(str(b))
        return (pa.hostname, pa.port, pa.path) == (pb.hostname, pb.port, pb.path) and bool(pa.hostname)
    except ValueError:
        return False


@app.route("/settings/out-camera", methods=["POST"])
@admin_required
def out_camera_settings():
    """Second camera at the exit: it marks OUT, the main camera then marks only IN.
    Empty = one camera marks both, as before."""
    back = url_for("settings") + "#out-camera"
    keep = url_for("settings", keep="out") + "#out-camera"   # failed: show what was typed again
    current = str(engine.out_camera_url())
    if request.form.get("remove"):
        save_settings({"out_camera_url": ""})
        if not WEB_ONLY:
            engine.set_out_camera("")
        flash("OUT camera removed - the main camera marks IN and OUT again.", "success")
        return redirect(back)
    in_cam = str(engine.camera_url())
    ip = request.form.get("camera_ip", "").strip()
    link = request.form.get("camera_link", "").strip()
    if ip == request.form.get("shown_ip", "").strip():
        ip = ""    # IP box left as pre-filled: the link decides (so editing only the link works too)
    new = current
    if link and link != mask_url(current):
        # password left masked (***): keep the saved one - this camera's, else the IN camera's
        saved_pwd = re.search(r"://[^:/@]+:([^@]*)@", current) or re.search(r"://[^:/@]+:([^@]*)@", in_cam)
        if ":***@" in link and saved_pwd:
            link = link.replace(":***@", f":{saved_pwd.group(1)}@", 1)
        new = link
    if ip and ip != camera_host(new):
        if not re.fullmatch(r"[A-Za-z0-9.\-]+", ip):
            flash("Camera IP looks wrong, e.g. 192.168.1.182", "error")
            return redirect(keep)
        base = new or in_cam   # only an IP typed: same user, password, port and path as the IN camera
        if not base.startswith("rtsp://"):
            flash("Type the full RTSP link of the OUT camera.", "error")
            return redirect(keep)
        new = with_host(base, ip)
    if not new:
        flash("Type the OUT camera's IP or its full RTSP link.", "error")
        return redirect(keep)
    if not (new.startswith(("rtsp://", "rtsps://", "http://", "https://")) or new.isdigit()):
        flash("Camera link must start with rtsp://", "error")
        return redirect(keep)
    if new == in_cam or same_camera(new, in_cam):
        flash("The OUT camera must be a different camera from the IN camera.", "error")
        return redirect(keep)
    if new == current:
        flash("OUT camera unchanged.", "info")
        return redirect(back)
    save_settings({"out_camera_url": new})
    if WEB_ONLY:
        flash(f"OUT camera saved ({mask_url(new)}). The office PC connects to it within a minute.", "success")
    else:
        engine.set_out_camera(new)
        flash(f"OUT camera set to {mask_url(new)} - connecting...", "success")
    return redirect(back)


# ------------------------------------------------------------------ employee self-service
def month_calendar(emp_id, first):
    """Weeks (Mon..Sun) of day cells for one month, plus that month's summary."""
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    today = date.today()
    rows, summary = att.build_report(first, last, emp_id)
    by_date = {r["date"]: r for r in rows}
    rules = att.Rules()
    leaves = att.approved_leaves(first, last, emp_id)
    pending = set()
    for lv in db().leaves.find({"emp_id": emp_id, "status": "pending", "from": {"$lte": last.isoformat()},
                                "to": {"$gte": first.isoformat()}}):
        d = max(first, att.parse_date(lv["from"]))
        while d <= min(last, att.parse_date(lv["to"])):
            pending.add(d.isoformat())
            d += timedelta(days=1)
    cells = [None] * first.weekday()
    d = first
    while d <= last:
        ds = d.isoformat()
        r = by_date.get(ds)
        if r:
            status, time_ = r["status"], r["check_in"]
        else:
            off = rules.day_off(d)
            status = off or ("Leave" if (emp_id, ds) in leaves else
                             "Leave pending" if ds in pending else "")
            time_ = ""
        cells.append({"day": d.day, "date": ds, "status": status, "time": time_,
                      "today": d == today, "future": d > today})
        d += timedelta(days=1)
    cells += [None] * (-len(cells) % 7)
    weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    ins = [r["check_in"] for r in rows if r["check_in"]]
    avg_in = ""
    if ins:
        mins = sum(int(t[:2]) * 60 + int(t[3:5]) for t in ins) // len(ins)
        avg_in = f"{mins // 60:02d}:{mins % 60:02d}"
    return weeks, (summary[0] if summary else None), avg_in


@app.route("/my")
@employee_required
def my():
    emp_id = session["emp_id"]
    start, end = report_range()
    rows, _ = att.build_report(start, end, emp_id)
    emp = get_employee(emp_id) or {}
    try:
        month = datetime.strptime(request.args.get("month", ""), "%Y-%m").date()
    except ValueError:
        month = date.today().replace(day=1)
    weeks, month_sum, avg_in = month_calendar(emp_id, month)
    rules = att.Rules()
    today = date.today()
    holidays = sorted(h for h in rules.holidays if h >= today.isoformat())[:8]
    return render_template(
        "my.html", rows=rows, start=start.isoformat(), end=end.isoformat(), emp=emp,
        today=att.today_status(emp_id), weeks=weeks, ms=month_sum, avg_in=avg_in,
        month=month, prev_month=(month - timedelta(days=1)).strftime("%Y-%m"),
        next_month=(month + timedelta(days=32)).strftime("%Y-%m"),
        balance=att.leave_balance(emp_id, today.year), leave_types=att.leave_types(),
        leaves=list(db().leaves.find({"emp_id": emp_id}).sort("applied_at", -1).limit(15)),
        corrections=list(db().corrections.find({"emp_id": emp_id}).sort("applied_at", -1).limit(15)),
        holidays=[(h, datetime.strptime(h, "%Y-%m-%d").strftime("%a, %d %b")) for h in holidays],
        weekly_off=", ".join(WEEKDAYS[d] for d in sorted(rules.weekly_off)),
        office={"start": rules.office_start.strftime("%H:%M"), "grace": rules.grace,
                "late_after": rules.late_after.strftime("%H:%M"),
                "half_after": rules.half_day_after.strftime("%H:%M")},
        field_allowed=bool(emp.get("field_allowed")) and emp.get("active", True))


@app.route("/my/leave", methods=["POST"])
@employee_required
def my_leave_apply():
    f = request.form
    try:
        start = parse_day(f.get("from"), None)
        end = parse_day(f.get("to") or f.get("from"), None)
        if start is None or end is None:
            raise ValueError("Pick the leave dates.")
        days = att.apply_leave(session["emp_id"], f.get("type", ""), start, end,
                               f.get("half_day") == "1", f.get("reason", ""))
        flash(f"Leave request sent ({days:g} day{'s' if days != 1 else ''}). You'll see the status here.",
              "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("my") + "#leave")


@app.route("/my/leave/<rid>/cancel", methods=["POST"])
@employee_required
def my_leave_cancel(rid):
    try:
        oid = ObjectId(rid)
    except InvalidId:
        abort(404)
    res = db().leaves.update_one({"_id": oid, "emp_id": session["emp_id"], "status": "pending"},
                                 {"$set": {"status": "cancelled", "decided_at": now_str()}})
    flash("Leave request cancelled." if res.modified_count else "Only pending requests can be cancelled.",
          "success" if res.modified_count else "error")
    return redirect(url_for("my") + "#leave")


@app.route("/my/correction", methods=["POST"])
@employee_required
def my_correction():
    f = request.form
    try:
        day = parse_day(f.get("date"), None)
        if day is None:
            raise ValueError("Pick the date.")
        att.request_correction(session["emp_id"], day, f.get("check_in", "").strip(),
                               f.get("check_out", "").strip(), f.get("reason", ""))
        flash("Correction request sent to the admin.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("my") + "#correction")


@app.route("/my/password", methods=["POST"])
@employee_required
def my_password():
    emp = db().employees.find_one({"_id": session["emp_id"]})
    current, new, again = (request.form.get(k, "") for k in ("current", "new", "again"))
    if not emp or not emp.get("password_hash") or not check_password_hash(emp["password_hash"], current):
        flash("Current password is wrong.", "error")
    elif len(new) < 6:
        flash("New password must be at least 6 characters.", "error")
    elif new != again:
        flash("The two new passwords don't match.", "error")
    else:
        db().employees.update_one({"_id": emp["_id"]}, {"$set": {"password_hash": generate_password_hash(new)}})
        flash("Password changed.", "success")
    return redirect(url_for("my") + "#profile")


@app.route("/my/export")
@employee_required
def my_export():
    start, end = report_range()
    rows, summary = att.build_report(start, end, session["emp_id"])
    buf = att.export_excel(rows, summary, start, end, get_settings()["company_name"])
    return send_file(buf, as_attachment=True, download_name=f"my_attendance_{start}_to_{end}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------ leave / correction approval
@app.route("/requests")
@admin_required
def requests_page():
    names = {e["_id"]: e for e in db().employees.find({}, {"name": 1, "emp_code": 1})}

    def load(coll):
        pending = list(coll.find({"status": "pending"}).sort("applied_at", 1))
        done = list(coll.find({"status": {"$ne": "pending"}}).sort("applied_at", -1).limit(30))
        for r in pending + done:
            who = names.get(r["emp_id"], {})
            r.update(id=str(r["_id"]), name=who.get("name", f"#{r['emp_id']}"), code=who.get("emp_code", ""))
        return pending, done

    leaves_p, leaves_d = load(db().leaves)
    corr_p, corr_d = load(db().corrections)
    year = date.today().year
    balances = {r["emp_id"]: {b["type"]: b for b in att.leave_balance(r["emp_id"], year)} for r in leaves_p}
    return render_template("requests.html", leaves_p=leaves_p, leaves_d=leaves_d, corr_p=corr_p,
                           corr_d=corr_d, balances=balances)


@app.route("/requests/<kind>/<rid>/<action>", methods=["POST"])
@admin_required
def decide_request(kind, rid, action):
    coll = {"leave": db().leaves, "correction": db().corrections}.get(kind)
    if coll is None or action not in ("approve", "reject"):
        abort(404)
    try:
        req = coll.find_one({"_id": ObjectId(rid), "status": "pending"})
    except InvalidId:
        abort(404)
    if req is None:
        flash("This request was already decided or cancelled.", "error")
        return redirect(url_for("requests_page"))
    if action == "approve" and kind == "correction":
        try:
            att.apply_correction(req)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("requests_page"))
    coll.update_one({"_id": req["_id"]}, {"$set": {
        "status": "approved" if action == "approve" else "rejected",
        "decided_at": now_str(), "admin_note": request.form.get("note", "").strip()[:200]}})
    flash(f"{kind.capitalize()} request {'approved' if action == 'approve' else 'rejected'}.", "success")
    return redirect(url_for("requests_page"))


@app.route("/api/my/today")
@employee_required
def my_today():
    return jsonify(att.today_status(session["emp_id"]))


@app.route("/api/field/mark", methods=["POST"])
@employee_required
def field_mark():
    """Employee marks IN/OUT from outside the office: live selfie + GPS location."""
    emp_id = session["emp_id"]
    emp = get_employee(emp_id)
    if not emp or not emp.get("active", True) or not emp.get("field_allowed"):
        return jsonify(ok=False, msg="Field attendance is not enabled for you. Ask the admin."), 403
    kind = request.form.get("kind", "")
    if kind not in ("IN", "OUT"):
        return jsonify(ok=False, msg="Choose IN or OUT."), 400
    try:
        lat, lng = float(request.form["lat"]), float(request.form["lng"])
        accuracy = round(float(request.form.get("accuracy") or 0))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
            raise ValueError
    except (KeyError, ValueError):
        return jsonify(ok=False, msg="Location missing. Allow location access and try again."), 400
    photo = request.files.get("photo")
    data = np.frombuffer(photo.read(), np.uint8) if photo else np.zeros(0, np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR) if 0 < data.size <= 10 * 1024 * 1024 else None
    if img is None:
        return jsonify(ok=False, msg="Photo missing. Take a selfie and try again."), 400
    check, error = engine.verify_selfie(emp_id, img)
    if error:
        return jsonify(ok=False, msg=f"Selfie not usable ({error}). Retake it with your face clearly visible."), 400
    note = request.form.get("note", "").strip()[:120]
    when = datetime.now()
    scale = min(1.0, 640.0 / max(img.shape[:2]))
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else img
    ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    key = put_photo(f"{when:%Y-%m-%d}/{emp_id}_field_{when:%H%M%S}.jpg", jpg.tobytes(), emp_id)
    try:
        clock = att.record_field(emp_id, when, kind, key, round(lat, 6), round(lng, 6), accuracy,
                                 note, check["verified"], check["score"])
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    msg = f"{kind} marked at {clock}."
    if not check["verified"]:
        msg += " Your face could not be verified - the admin will review this entry."
    return jsonify(ok=True, msg=msg, verified=check["verified"], today=att.today_status(emp_id))


@app.route("/field")
@admin_required
def field_page():
    """Admin review of attendance marked from outside the office."""
    day = parse_day(request.args.get("date"), date.today()).isoformat()
    names = {e["_id"]: e for e in db().employees.find({}, {"name": 1, "emp_code": 1})}
    items = []
    for ev in db().events.find({"source": "field", "ts": {"$gte": f"{day} 00:00:00",
                                                         "$lte": f"{day} 23:59:59"}}).sort("ts", 1):
        who = names.get(ev["emp_id"], {})
        items.append(dict(ev, name=who.get("name", f"#{ev['emp_id']}"), code=who.get("emp_code", "")))
    days = sorted({e["ts"][:10] for e in db().events.find({"source": "field"}, {"ts": 1})},
                  reverse=True)[:30]
    return render_template("field.html", day=day, items=items, days=days)


# ------------------------------------------------------------------ API keys + docs
@app.route("/api-keys", methods=["GET", "POST"])
@admin_required
def api_keys():
    new_key = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Give the key a name, e.g. 'eSSL gate machine' or 'Payroll'.", "error")
        else:
            new_key = api.create_key(name, request.form.getlist("scopes"))
    return render_template("api_keys.html", keys=api.list_keys(), new_key=new_key,
                           base_url=request.host_url.rstrip("/"))


@app.route("/api-keys/<key_id>/revoke", methods=["POST"])
@admin_required
def api_key_revoke(key_id):
    flash("Key revoked - it stops working immediately." if api.revoke_key(key_id) else "Key not found.",
          "success")
    return redirect(url_for("api_keys"))


@app.route("/api-keys/<key_id>/delete", methods=["POST"])
@admin_required
def api_key_delete(key_id):
    if api.delete_key(key_id):
        flash("Key deleted.", "success")
    else:
        flash("Only revoked keys can be deleted - revoke it first.", "error")
    return redirect(url_for("api_keys"))


@app.route("/api/docs")
def api_docs():
    return render_template("api_docs.html", base_url=request.host_url.rstrip("/"),
                           rate=api.RATE_LIMIT, max_days=api.MAX_RANGE_DAYS,
                           max_batch=api.MAX_PUNCHES_PER_POST, past_days=api.PUNCH_PAST_DAYS)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(err):
    if request.path.startswith("/api/v1/"):
        return jsonify(ok=False, error="Unknown API endpoint - see /api/docs." if err.code == 404
                       else err.description), err.code
    if request.path.startswith("/api/"):
        return jsonify(ok=False, msg=err.description), err.code
    return render_template("error.html", err=err), err.code


@app.route("/api/camera/release", methods=["POST"])
@admin_required
def camera_release():
    """Hand the camera's single RTSP connection back before restarting the app
    (only from this PC)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    engine.camera.stop()
    if engine.out is not None:
        engine.out.camera.stop()
    return jsonify(ok=True)


_close_handler = None


def release_camera_on_window_close(stop):
    """Closing the start.bat window kills Python without running atexit; this
    camera then keeps the dead connection and refuses everyone. Windows gives a
    console a few seconds on close, enough to send the RTSP TEARDOWN."""
    global _close_handler
    if sys.platform != "win32":
        return
    import ctypes
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def on_event(_kind):
        stop()
        return False    # continue with the normal shutdown

    _close_handler = handler_type(on_event)   # keep a reference alive
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_close_handler, True)


def raise_priority():
    """Task Scheduler starts the service at "below normal" CPU priority, so any other busy
    program on this PC (e.g. another camera app) slows face detection right down and
    people walk past unseen. Run above normal so attendance gets the CPU first."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    if kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x8000):   # ABOVE_NORMAL_PRIORITY_CLASS
        print("CPU priority: above normal", flush=True)
    else:
        print(f"Could not raise CPU priority (error {ctypes.get_last_error()})", flush=True)


def keep_awake():
    """Render's free plan sleeps after 15 min without visitors and the next visit then
    takes about a minute. Visiting our own public link every 10 min keeps it answering
    straight away (punching machines, payroll). Render sets RENDER_EXTERNAL_URL itself."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not (url and getattr(config, "KEEP_AWAKE", False)):
        return

    def loop():
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(url + "/login", timeout=60).read(64)
            except Exception:
                pass

    threading.Thread(target=loop, name="keep-awake", daemon=True).start()
    print(f"Keep-awake: visiting {url} every 10 min", flush=True)


def main():
    global engine
    if not WEB_ONLY:
        raise_priority()
    init_db()
    engine = Engine()
    engine.start(with_camera=not WEB_ONLY)
    app.extensions["netra_engine"] = engine
    # close the RTSP session properly on exit; this camera allows only one
    atexit.register(engine.stop)
    release_camera_on_window_close(engine.stop)
    if WEB_ONLY:
        print("Role: web (cloud) - website + API, camera handled by the office PC", flush=True)
    else:
        live_sender()
    keep_awake()
    from waitress import serve
    print(f"NetraAttend running: http://127.0.0.1:{config.PORT}  (LAN: http://<this-pc-ip>:{config.PORT})",
          flush=True)
    serve(app, host=config.HOST, port=config.PORT, threads=24, channel_timeout=60,
          connection_limit=200, ident="NetraAttend")


if __name__ == "__main__":
    main()
