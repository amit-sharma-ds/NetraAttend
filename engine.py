"""Real-time attendance engine.

Pipeline (each stage in its own thread so the live view never waits on AI):

  camera thread   -> newest frame only (camera.py)
  detect thread   -> SCRFD face detection + tracking, ~8 fps; picks the best
                     (biggest, sharpest, most frontal) crop of every face track
  recognise thread-> ArcFace embedding only for tracks that still need it,
                     votes over frames, marks attendance once confirmed
  stream thread   -> draws boxes/names on the newest frame for the browser
"""
import threading
import time
import traceback
from collections import Counter, deque
from datetime import datetime

import cv2
import numpy as np

import config
from attendance import Recorder
from camera import CameraStream
from bson.binary import Binary

from db import db, get_settings, now_str, put_photo, save_settings
from faces import Detector, Embedder, align, sharpness

cv2.setNumThreads(2)

# Enrollment steps, done in this order: (pose, samples needed, instruction).
# Only the current step's pose is captured, so every sample lands in the right pose.
POSES = [
    ("front", 5, "Look straight at the camera"),
    ("left", 3, "Slowly turn your head to YOUR LEFT"),
    ("right", 3, "Slowly turn your head to YOUR RIGHT"),
    ("up", 2, "Lift your chin - look slightly UP"),
    ("down", 2, "Lower your chin - look slightly DOWN"),
]
POSE_NEED = {p: n for p, n, _ in POSES}
SKIPPABLE = {"up", "down"}   # hard to get with a camera mounted high
MIN_ENROLL_SAMPLES = 6
MAX_SAMPLES_PER_EMP = 40
MIN_ENROLL_FACE_PX = getattr(config, "MIN_ENROLL_FACE_PX", 60)
ENROLL_IDLE_TIMEOUT = 180

TRACK_EXPIRY = 1.5     # seconds a track survives without a detection
REVERIFY_SEC = 4.0     # re-check confirmed faces this often (also refreshes "last seen")
SLOW_RETRY_SEC = 1.5   # retry pace for faces that did not match yet
UNKNOWN_AFTER = 6      # attempts without any match before showing "Unknown"
UNKNOWN_SNAP_GAP = 10  # at most one unknown-face photo per camera this often (seconds)

# Weaker matches (e.g. the day after shaving a beard) still count when several
# frames in a row agree on the same person and nobody else is close.
WEAK_MATCH = 0.34
WEAK_MARGIN = 0.12

# Auto-learning: confident recognitions occasionally add a fresh sample, so the
# stored face follows slow changes (beard, glasses, haircut, lighting).
AUTO_LEARN_SCORE = 0.45
AUTO_LEARN_MARGIN = 0.20
AUTO_LEARN_NOVELTY = 0.80    # only add views that differ from what is stored
AUTO_LEARN_EVERY = 600       # seconds between auto samples per person
AUTO_SAMPLES_MAX = 15        # oldest auto samples are replaced

# Field attendance selfies come from a phone camera while the stored faces come
# from the CCTV, so the match is a little looser than on the camera itself.
FIELD_FACE_MIN = 0.35

GREEN, AMBER, RED, BLUE, GREY = (80, 200, 60), (0, 170, 255), (60, 60, 230), (230, 140, 40), (160, 160, 160)


def pose_bin(yaw, pitch):
    if abs(yaw) <= 0.15 and abs(pitch) <= 0.12:
        return "front"
    if yaw >= 0.2:
        return "left"
    if yaw <= -0.2:
        return "right"
    if abs(yaw) < 0.2 and pitch <= -0.10:
        return "up"
    if abs(yaw) < 0.2 and pitch >= 0.10:
        return "down"
    return None


def pose_hint(step, yaw, pitch):
    """What to tell the person when their head is not in the pose we need."""
    if step == "front":
        if abs(yaw) > 0.15:
            return "Face the camera straight - don't turn your head"
        return "Lower your chin a little" if pitch < 0 else "Lift your chin a little - look AT the camera"
    if step == "left":
        return "Other side - turn to YOUR LEFT" if yaw < -0.1 else "Turn a bit more to the LEFT"
    if step == "right":
        return "Other side - turn to YOUR RIGHT" if yaw > 0.1 else "Turn a bit more to the RIGHT"
    if step == "up":
        return "Keep face straight, lift your chin more" if abs(yaw) >= 0.2 else "Lift your chin a bit more"
    if step == "down":
        return "Keep face straight, lower your chin more" if abs(yaw) >= 0.2 else "Lower your chin a bit more"
    return ""


def make_snapshot(frame, bbox, height=300):
    """Face with some context (head + shoulders) for evidence photos."""
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox]
    w, h = x2 - x1, y2 - y1
    cx1, cy1 = max(0, int(x1 - 0.6 * w)), max(0, int(y1 - 0.5 * h))
    cx2, cy2 = min(W, int(x2 + 0.6 * w)), min(H, int(y2 + 0.8 * h))
    snap = frame[cy1:cy2, cx1:cx2]
    if snap.size == 0:
        return None
    scale = height / float(snap.shape[0])
    return cv2.resize(snap, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)


def _iou(a, b):
    xx1, yy1 = max(a[0], b[0]), max(a[1], b[1])
    xx2, yy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, xx2 - xx1) * max(0.0, yy2 - yy1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _ascii(text):
    return text.encode("ascii", "replace").decode()


class Gallery:
    """All enrolled face embeddings, grouped per employee, for fast matching."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ids = np.zeros(0, dtype=np.int64)
        self.starts = np.zeros(0, dtype=np.int64)
        self.matrix = np.zeros((0, 512), dtype=np.float32)
        self.names = {}
        self.codes = {}

    def set(self, groups):
        """groups: list of (emp_id, name, code, matrix[n,512])"""
        ids, starts, mats, names, codes = [], [], [], {}, {}
        pos = 0
        for emp_id, name, code, mat in groups:
            ids.append(emp_id)
            starts.append(pos)
            mats.append(mat)
            pos += len(mat)
            names[emp_id], codes[emp_id] = name, code
        with self.lock:
            self.ids = np.asarray(ids, dtype=np.int64)
            self.starts = np.asarray(starts, dtype=np.int64)
            self.matrix = np.concatenate(mats) if mats else np.zeros((0, 512), np.float32)
            self.names, self.codes = names, codes

    def match(self, emb, exclude=None):
        """(emp_id, best score, margin over 2nd best person)."""
        with self.lock:
            ids, starts, matrix = self.ids, self.starts, self.matrix
        if not len(ids):
            return None, 0.0, 0.0
        per_person = np.maximum.reduceat(matrix @ emb, starts)
        if exclude is not None:
            per_person = np.where(ids == exclude, -1.0, per_person)
        order = np.argsort(per_person)[::-1]
        best = float(per_person[order[0]])
        second = float(per_person[order[1]]) if len(order) > 1 else 0.0
        if best < 0:
            return None, best, 0.0
        return int(ids[order[0]]), best, best - max(second, 0.0)

    def name(self, emp_id):
        return self.names.get(emp_id, "?")

    def best_own(self, emp_id, emb):
        """Highest similarity of emb to this employee's stored samples."""
        with self.lock:
            ids, starts, matrix = self.ids, self.starts, self.matrix
        hit = np.where(ids == emp_id)[0]
        if not len(hit):
            return 0.0
        i = int(hit[0])
        end = int(starts[i + 1]) if i + 1 < len(starts) else len(matrix)
        return float((matrix[int(starts[i]):end] @ emb).max())


class Track:
    def __init__(self, tid, bbox, now):
        self.id = tid
        self.bbox = bbox
        self.last_seen = now
        self.cand = None
        self.votes = deque(maxlen=6)
        self.attempts = 0
        self.last_rec = 0.0
        self.emp_id = None
        self.name = None
        self.score = 0.0
        self.mismatch = 0
        self.unknown = False
        self.unknown_saved = False   # photo of this unknown face already taken
        self.result = None
        self.enrolling = False


class EnrollSession:
    def __init__(self, emp_id, name, replace):
        self.emp_id, self.name, self.replace = emp_id, name, replace
        self.samples = []
        self.cand = None
        self.skipped = set()
        self.started = self.last_activity = time.monotonic()
        self.warning = ""
        self.hint = ""
        self.detected = None     # pose the person's head is in right now

    def counts(self):
        return Counter(s["pose"] for s in self.samples)

    def next_step(self):
        counts = self.counts()
        for pose, need, text in POSES:
            if counts[pose] < need and pose not in self.skipped:
                return pose, text
        return None, "All poses captured - saving"

    def skip(self):
        step, _ = self.next_step()
        if step in SKIPPABLE:
            self.skipped.add(step)
            self.cand = None

    def offer(self, cand):
        step, _ = self.next_step()
        self.detected = cand["pose"]
        if step is None:
            return
        if cand["pose"] != step:
            self.hint = pose_hint(step, cand["yaw"], cand["pitch"])
            return
        self.hint = "Good - hold still"
        if self.cand is None or cand["q"] > self.cand["q"]:
            self.cand = cand


class Streamer:
    """Renders the annotated live view once and shares it with every viewer."""

    def __init__(self, engine):
        self.engine = engine
        self.cond = threading.Condition()
        self.jpg = None
        self.seq = 0
        self.last_client = 0.0
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name="stream").start()

    def _loop(self):
        while self.running:
            if time.monotonic() - self.last_client > 5:
                time.sleep(0.2)   # nobody watching: don't spend CPU encoding
                continue
            t0 = time.monotonic()
            try:
                jpg = self._render()
            except Exception:
                traceback.print_exc()
                time.sleep(1)
                continue
            with self.cond:
                self.jpg, self.seq = jpg, self.seq + 1
                self.cond.notify_all()
            time.sleep(max(0.0, 1.0 / config.STREAM_FPS - (time.monotonic() - t0)))

    @staticmethod
    def _text(img, text, org, scale, color, thick=1, bg=None):
        (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        x, y = org
        if bg is not None:
            cv2.rectangle(img, (x - 4, y - th - 6), (x + tw + 4, y + base), bg, -1)
        cv2.putText(img, text, (x, y - 2), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)

    def _render(self):
        eng = self.engine
        cam = eng.camera
        _, frame, ts = cam.latest()
        width = int(config.STREAM_WIDTH)
        if frame is None or (not cam.connected and time.time() - ts > 3):
            img = np.full((width * 9 // 16, width, 3), 30, np.uint8)
            self._text(img, "Camera offline - reconnecting...", (30, img.shape[0] // 2), 0.9, (255, 255, 255), 2)
            self._text(img, _ascii(cam.status()["source"]), (30, img.shape[0] // 2 + 40), 0.55, (180, 180, 180))
            self._text(img, "Camera is not sending video. If this lasts over 1 minute: switch the camera",
                       (30, img.shape[0] // 2 + 80), 0.55, (120, 200, 255))
            self._text(img, "OFF and ON, and close other apps/NVR viewing it. It reconnects by itself.",
                       (30, img.shape[0] // 2 + 106), 0.55, (120, 200, 255))
        else:
            scale = width / float(frame.shape[1])
            # INTER_LINEAR: ~5x faster than INTER_AREA on 4MP frames, fine for display
            img = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR) \
                if scale < 1 else frame.copy()
            scale = min(scale, 1.0)
            if config.SCAN_REGION:
                h, w = img.shape[:2]
                fx1, fy1, fx2, fy2 = config.SCAN_REGION
                cv2.rectangle(img, (int(fx1 * w), int(fy1 * h)), (int(fx2 * w), int(fy2 * h)),
                              (200, 200, 200), 1)
            with eng.lock:
                overlay = list(eng.overlay)
                overlay_age = time.monotonic() - eng.overlay_ts
                banners = [b for b in eng.banners if b[0] > time.monotonic()]
            if overlay_age < 1.5:
                for bbox, label, color in overlay:
                    x1, y1, x2, y2 = [int(v * scale) for v in bbox]
                    cv2.rectangle(img, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
                    self._text(img, _ascii(label), (x1, max(18, y1 - 4)), 0.55, (255, 255, 255), 1, color)
            for i, (_, text, color) in enumerate(banners[-3:]):
                self._text(img, _ascii(text), (14, 34 + i * 36), 0.75, (255, 255, 255), 2, color)
            tag = {"in": "IN camera  ", "out": "OUT camera  "}.get(eng.direction, "")
            st = f"{tag}{datetime.now():%H:%M:%S}  cam {cam.fps:.0f}fps  ai {eng.det_fps:.1f}fps"
            self._text(img, st, (10, img.shape[0] - 8), 0.45, (255, 255, 255), 1, (40, 40, 40))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(config.STREAM_QUALITY)])
        return buf.tobytes() if ok else None

    def frames(self):
        last = -1
        while True:
            self.last_client = time.monotonic()
            with self.cond:
                self.cond.wait_for(lambda: self.seq != last, timeout=2.0)
                jpg, last = self.jpg, self.seq
            if jpg is None:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")


def jpeg(img, quality=85):
    if img is None:
        return None
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


class Engine:
    def __init__(self, main=None, url=None):
        """main=None: the main camera (the only one, or the entrance camera).
        main=<Engine>: the exit camera - its own camera, tracking and live view, sharing
        the main engine's models, registered faces and attendance recorder."""
        self.main = main
        self.out = None              # exit camera pipeline (main engine only)
        self.out_url = ""            # exit camera link in use ("" = only one camera)
        self.camera_lock = threading.Lock()
        if main is None:
            self.camera = self._make_camera(self.camera_url())
            self.detector = Detector(config.DET_MODEL, config.DET_THREADS)
            self.embedder = Embedder(config.REC_MODEL, config.REC_THREADS)
            self.gallery = Gallery()
            self.recorder = Recorder()
            self.recent = deque(maxlen=40)
            self.last_learn = {}     # emp_id -> monotonic time of last auto sample
        else:
            self.camera = self._make_camera(url, "out_camera_ok_url")
            self.detector, self.embedder = main.detector, main.embedder
            self.gallery, self.recorder = main.gallery, main.recorder
            self.recent, self.last_learn = main.recent, main.last_learn
        self.streamer = Streamer(self)
        self.lock = threading.Lock()
        self.tracks = {}
        self.next_tid = 1
        self.overlay = []
        self.overlay_ts = 0.0
        self.banners = deque(maxlen=6)
        self.enroll = None
        self.work = threading.Event()
        self.running = False
        self.det_fps = 0.0
        self.det_ms = 0.0
        self.rec_ms = 0.0
        self.last_face_time = 0.0
        self.last_unknown = 0.0
        if main is None:
            self.load_gallery()

    @property
    def direction(self):
        """None = one camera marks IN and OUT; "in"/"out" = entrance/exit camera."""
        if self.main is not None:
            return "out"
        return "in" if self.out_url else None

    # ------------------------------------------------------------ lifecycle
    def start(self, with_camera=True):
        """with_camera=False is the cloud "web" role: website, API and selfie checks
        only - the CCTV camera is handled by the office PC on the same database."""
        self.running = True
        self.with_camera = with_camera
        if with_camera:
            self.camera.start()
            self.streamer.start()
            threading.Thread(target=self._det_loop, daemon=True, name="detect").start()
            threading.Thread(target=self._rec_loop, daemon=True, name="recognise").start()
        if self.main is None:
            threading.Thread(target=self._flush_loop, daemon=True, name="db-retry").start()
            if with_camera and self.out_camera_url():
                self.set_out_camera(self.out_camera_url())

    def stop(self):
        self.running = False
        self.streamer.running = False
        self.work.set()
        self.camera.stop()
        if self.out is not None:
            self.out.stop()

    def _flush_loop(self):
        last_sync = time.monotonic()
        while self.running:
            time.sleep(10)
            self.recorder.flush_pending()
            if time.monotonic() - last_sync < 60:
                continue
            last_sync = time.monotonic()
            try:
                if not getattr(self, "with_camera", True):
                    self.load_gallery(reset_tracks=False)   # faces registered on the office PC
                else:
                    if str(self.camera_url()) != str(self.camera.source):
                        print("Camera link changed in Settings - switching", flush=True)
                        self.set_camera(self.camera_url())
                    out_now = str(self.out.camera.source) if self.out is not None else ""
                    if str(self.out_camera_url()) != out_now:
                        print("OUT camera changed in Settings - switching", flush=True)
                        self.set_out_camera(self.out_camera_url())
            except Exception:
                traceback.print_exc()

    # ------------------------------------------------------------ camera
    @staticmethod
    def camera_url():
        """Camera saved from the Settings page, else config.CAMERA."""
        return get_settings().get("camera_url") or config.CAMERA

    @staticmethod
    def out_camera_url():
        """Exit (OUT) camera from the Settings page, else config.OUT_CAMERA; "" = none."""
        return get_settings().get("out_camera_url") or getattr(config, "OUT_CAMERA", "") or ""

    @staticmethod
    def _make_camera(url, ok_key="camera_ok_url"):
        def remember(working_url):
            save_settings({ok_key: working_url})
        return CameraStream(url, config.CAMERA_ROTATE,
                            known_good=get_settings().get(ok_key) == str(url),
                            on_connect=remember)

    def set_camera(self, url):
        """Switch to another camera/link without restarting the app."""
        # one switch at a time: two at once (Settings page + background sync) could
        # leave an old connection running forever
        with self.camera_lock:
            old = self.camera
            old.stop()    # release first: this camera allows only one RTSP connection
            self.camera = self._make_camera(url)
            self.camera.start()
        with self.lock:
            self.tracks.clear()
            self.overlay = []

    def set_out_camera(self, url):
        """Add, switch or remove (url "") the exit camera without restarting the app."""
        with self.camera_lock:
            old, self.out = self.out, None
            self.out_url = url or ""
            if old is not None:
                old.stop()
            if url:
                out = Engine(main=self, url=url)
                out.start()
                self.out = out
        with self.lock:
            for tr in self.tracks.values():   # IN/OUT meaning of this camera changed
                tr.result = None

    # ------------------------------------------------------------ gallery
    def load_gallery(self, reset_tracks=True):
        d = db()
        stale = list(d.face_samples.find({"model": {"$ne": config.REC_MODEL}}, {"crop": 1}))
        if stale:
            print(f"Re-computing {len(stale)} face samples for model {config.REC_MODEL} ...", flush=True)
            for i in range(0, len(stale), 16):
                chunk = stale[i:i + 16]
                crops = [cv2.imdecode(np.frombuffer(r["crop"], np.uint8), cv2.IMREAD_COLOR) for r in chunk]
                embs = self.embedder.embed_crops(crops)
                for r, e in zip(chunk, embs):
                    d.face_samples.update_one({"_id": r["_id"]}, {"$set": {
                        "embedding": Binary(e.astype(np.float32).tobytes()), "model": config.REC_MODEL}})
        active = {e["_id"]: e for e in d.employees.find({"active": True}, {"name": 1, "emp_code": 1})}
        rows = list(d.face_samples.find({"emp_id": {"$in": list(active)}}, {"emp_id": 1, "embedding": 1})
                    .sort([("emp_id", 1), ("_id", 1)]))
        groups, current = [], None
        for r in rows:
            emb = np.frombuffer(r["embedding"], dtype=np.float32)
            if emb.size != 512:
                continue
            if current is None or current[0] != r["emp_id"]:
                emp = active[r["emp_id"]]
                current = (r["emp_id"], emp["name"], emp["emp_code"], [])
                groups.append(current)
            current[3].append(emb)
        self.gallery.set([(i, n, c, np.stack(m)) for i, n, c, m in groups])
        if not reset_tracks:
            return
        for eng in (self, self.out):
            if eng is None:
                continue
            with eng.lock:
                for tr in eng.tracks.values():   # names/ids may have changed
                    tr.emp_id, tr.name, tr.result = None, None, None
                    tr.votes.clear()
        print(f"Face gallery: {len(groups)} employees, {len(rows)} samples", flush=True)

    def _auto_learn(self, emp_id, cand, emb, score, margin):
        """Store a fresh sample of a confidently recognised person (see AUTO_LEARN_*)."""
        now = time.monotonic()
        if (score < AUTO_LEARN_SCORE or margin < AUTO_LEARN_MARGIN or cand["q"] < 0.4
                or now - self.last_learn.get(emp_id, -1e9) < AUTO_LEARN_EVERY
                or self.gallery.best_own(emp_id, emb) >= AUTO_LEARN_NOVELTY):
            return
        self.last_learn[emp_id] = now
        samples = db().face_samples
        samples.insert_one({"emp_id": emp_id, "pose": pose_bin(cand["yaw"], cand["pitch"]) or "other",
                            "source": "auto", "crop": Binary(jpeg(cand["crop"], 95)),
                            "embedding": Binary(emb.astype(np.float32).tobytes()),
                            "model": config.REC_MODEL, "created_at": now_str()})
        old = [s["_id"] for s in samples.find({"emp_id": emp_id, "source": "auto"}, {"_id": 1})
               .sort("_id", -1).skip(AUTO_SAMPLES_MAX)]
        if old:
            samples.delete_many({"_id": {"$in": old}})
        self.load_gallery(reset_tracks=False)
        print(f"Auto-learned a new face sample for {self.gallery.name(emp_id)} "
              f"(score {score:.2f})", flush=True)

    # ------------------------------------------------------------ detection
    def _det_loop(self):
        last_id, last_run, n, t0 = -1, 0.0, 0, time.monotonic()
        while self.running:
            fid, frame, _ = self.camera.wait_frame(last_id, 1.0)
            now = time.monotonic()
            if self.enroll and now - self.enroll.last_activity > ENROLL_IDLE_TIMEOUT:
                self.cancel_enroll()
            if frame is None:
                if not self.camera.connected:
                    with self.lock:
                        self.tracks.clear()
                        self.overlay = []
                continue
            last_id = fid
            busy = now - self.last_face_time < 4.0 or self.enroll is not None
            if now - last_run < 1.0 / (config.DET_FPS if busy else config.IDLE_DET_FPS):
                continue
            last_run = now
            try:
                self._process(frame, now)
            except Exception:
                traceback.print_exc()
                time.sleep(0.2)
            n += 1
            if now - t0 >= 2.0:
                self.det_fps, n, t0 = n / (now - t0), 0, now

    def _process(self, frame, now):
        h, w = frame.shape[:2]
        x0 = y0 = 0
        region = frame
        if config.SCAN_REGION:
            fx1, fy1, fx2, fy2 = config.SCAN_REGION
            x0, y0 = int(fx1 * w), int(fy1 * h)
            region = frame[y0:int(fy2 * h), x0:int(fx2 * w)]
        t = time.perf_counter()
        faces = self.detector.detect(region, config.DET_WIDTH, config.DET_THRESHOLD)
        self.det_ms = 0.8 * self.det_ms + 0.2 * (time.perf_counter() - t) * 1000
        for f in faces:
            f.bbox += np.array([x0, y0, x0, y0], dtype=np.float32)
            f.kps += np.array([x0, y0], dtype=np.float32)
        if faces:
            self.last_face_time = now

        with self.lock:
            tracks = self._assign(faces, now)
            enroll = self.enroll
            target = self._enroll_target(faces, enroll) if enroll else None
            overlay = []
            for i, (face, tr) in enumerate(zip(faces, tracks)):
                tr.enrolling = target == i
                yaw, pitch = face.pose()
                if (face.width >= config.MIN_FACE_PX and face.score >= 0.55
                        and abs(yaw) <= config.MAX_YAW):
                    crop = align(frame, face.kps)
                    sharp = sharpness(crop) if crop is not None else 0.0
                    if crop is not None and sharp >= config.MIN_SHARPNESS:
                        q = (min(face.width, 160.0) / 160.0) * (1.2 - abs(yaw)) * min(sharp / 80.0, 1.0)
                        cand = {"crop": crop, "q": q, "frame": frame, "bbox": face.bbox.copy(),
                                "yaw": yaw, "pitch": pitch}
                        if tr.enrolling:
                            if face.width >= MIN_ENROLL_FACE_PX:
                                cand["pose"] = pose_bin(yaw, pitch)
                                enroll.offer(cand)
                        elif tr.cand is None or q > tr.cand["q"]:
                            tr.cand = cand
                overlay.append((face.bbox.copy(), *self._label(tr, face, enroll)))
            self.overlay = overlay
            self.overlay_ts = now
        self.work.set()

    def _assign(self, faces, now):
        for tid in [t for t, tr in self.tracks.items() if now - tr.last_seen > TRACK_EXPIRY]:
            del self.tracks[tid]
        pairs = []
        for i, f in enumerate(faces):
            fc = ((f.bbox[0] + f.bbox[2]) / 2, (f.bbox[1] + f.bbox[3]) / 2)
            for tid, tr in self.tracks.items():
                iou = _iou(f.bbox, tr.bbox)
                tc = ((tr.bbox[0] + tr.bbox[2]) / 2, (tr.bbox[1] + tr.bbox[3]) / 2)
                dist = np.hypot(fc[0] - tc[0], fc[1] - tc[1]) / max(f.width, 1.0)
                if iou > 0.15 or dist < 0.7:
                    pairs.append((iou - 0.3 * dist, i, tid))
        pairs.sort(reverse=True)
        assigned, used = {}, set()
        for _, i, tid in pairs:
            if i in assigned or tid in used:
                continue
            assigned[i] = self.tracks[tid]
            used.add(tid)
        out = []
        for i, f in enumerate(faces):
            tr = assigned.get(i)
            if tr is None:
                tr = Track(self.next_tid, f.bbox.copy(), now)
                self.tracks[tr.id] = tr
                self.next_tid += 1
            tr.bbox = f.bbox.copy()
            tr.last_seen = now
            out.append(tr)
        return out

    @staticmethod
    def _enroll_target(faces, enroll):
        if not faces:
            enroll.warning = "No face visible"
            return None
        big = faces[0]   # sorted largest first
        if len(faces) > 1 and faces[1].width >= 0.7 * big.width:
            enroll.warning = "Only ONE person should be in front of the camera"
            return None
        if big.width < MIN_ENROLL_FACE_PX:
            enroll.warning = "Come closer to the camera"
            return None
        enroll.warning = ""
        return 0

    def _label(self, tr, face, enroll):
        if tr.enrolling:
            return f"{enroll.name}: {enroll.hint or enroll.next_step()[1]}", BLUE
        if face.width < config.MIN_FACE_PX:
            return "Come closer", GREY
        if tr.emp_id is not None:
            res = tr.result
            if res and res["kind"] == "IN":
                return f"{tr.name} | IN {res['time']} {res['status']}", GREEN
            if res and res["kind"] == "OUT":
                if self.direction == "out":
                    return f"{tr.name} | OUT {res['time']}", BLUE
                return f"{tr.name} | seen {res['time']}", GREEN
            if res and res["kind"] == "SEEN":
                return f"{tr.name} | in since {res['time']}", GREEN
            if res and res["kind"] == "NOT_IN":
                return f"{tr.name} | no IN today", GREY
            return tr.name, GREEN
        if tr.unknown:
            return "Unknown", RED
        return "Identifying...", AMBER

    # ------------------------------------------------------------ recognition
    def _rec_loop(self):
        while self.running:
            self.work.wait(0.5)
            self.work.clear()
            while self.running:
                job = self._take_job()
                if job is None:
                    break
                kind, ref, cand = job
                try:
                    t = time.perf_counter()
                    emb = self.embedder.embed_crops([cand["crop"]])[0]
                    self.rec_ms = 0.7 * self.rec_ms + 0.3 * (time.perf_counter() - t) * 1000
                    if kind == "enroll":
                        self._enroll_add(ref, cand, emb)
                    else:
                        self._apply(ref, cand, emb)
                except Exception:
                    traceback.print_exc()

    def _take_job(self):
        now = time.monotonic()
        with self.lock:
            if self.enroll and self.enroll.cand:
                cand, self.enroll.cand = self.enroll.cand, None
                return "enroll", self.enroll, cand
            best, best_key = None, None
            for tr in self.tracks.values():
                if tr.cand is None or tr.enrolling:
                    continue
                if tr.emp_id is not None:
                    if now - tr.last_rec < REVERIFY_SEC:
                        continue
                    pri = 2
                elif tr.attempts >= UNKNOWN_AFTER:
                    if now - tr.last_rec < SLOW_RETRY_SEC:
                        continue
                    pri = 1
                else:
                    pri = 0
                key = (pri, tr.attempts, -tr.cand["q"])
                if best_key is None or key < best_key:
                    best, best_key = tr, key
            if best is None:
                return None
            cand, best.cand = best.cand, None
            best.last_rec = now
            best.attempts += 1
            return "track", best, cand

    def _apply(self, tr, cand, emb):
        emp_id, score, margin = self.gallery.match(emb)
        level = 0    # 2 = normal match, 1 = weak match, 0 = no match
        if emp_id is not None:
            if score >= config.MATCH_THRESHOLD and margin >= config.MATCH_MARGIN:
                level = 2
            elif score >= WEAK_MATCH and margin >= WEAK_MARGIN:
                level = 1
        vote = emp_id if level else None
        to_record = None
        learn = False
        new_unknown = False
        with self.lock:
            tr.score = score
            tr.votes.append((vote, level))
            normal = Counter(v for v, lv in tr.votes if lv == 2)
            if tr.emp_id is None:
                anyv = Counter(v for v, lv in tr.votes if lv >= 1)
                best = None
                if level == 2 and score >= config.STRONG_MATCH and margin >= 2 * config.MATCH_MARGIN:
                    best = emp_id
                elif normal and normal.most_common(1)[0][1] >= config.VOTES_NEEDED:
                    best = normal.most_common(1)[0][0]
                elif anyv:
                    cand_id, n = anyv.most_common(1)[0]
                    others = sum(c for k, c in normal.items() if k != cand_id)
                    if n >= config.VOTES_NEEDED + 1 and not others:
                        best = cand_id
                if best is not None:
                    tr.emp_id, tr.name = best, self.gallery.name(best)
                    tr.unknown, tr.mismatch = False, 0
                    to_record = best
                else:
                    tr.unknown = tr.attempts >= UNKNOWN_AFTER and not anyv
                    if tr.unknown and not tr.unknown_saved:
                        tr.unknown_saved = new_unknown = True
            elif vote == tr.emp_id:
                tr.mismatch = 0
                to_record = vote
            else:
                tr.mismatch += 1
                if tr.mismatch >= 2:   # someone else is now in this box
                    tr.emp_id = tr.name = tr.result = None
                    tr.votes.clear()
                    tr.mismatch = 0
            learn = (to_record is not None and vote == to_record and level == 2
                     and normal.get(to_record, 0) >= 2)
        if new_unknown:
            self._save_unknown(cand, score)
        if to_record is None:
            return
        snap = jpeg(make_snapshot(cand["frame"], cand["bbox"], height=240), 80)
        res = self.recorder.record(to_record, datetime.now(), round(score, 3), snap, self.direction)
        name = self.gallery.name(to_record)
        with self.lock:
            tr.result = res
            if res["fresh"] and res["kind"] in ("IN", "OUT", "SEEN"):
                if res["kind"] == "IN":
                    late = f" (+{res['late_min']}m)" if res["late_min"] else ""
                    text = f"{name}: IN {res['time']} - {res['status']}{late}"
                    color = {"On Time": GREEN, "Late": AMBER}.get(res["status"], BLUE)
                elif res["kind"] == "OUT":
                    text, color = f"{name}: OUT {res['time']} - hours {res.get('hours', '')}", BLUE
                else:
                    text, color = f"{name}: already IN since {res['time']}", GREEN
                self.banners.append((time.monotonic() + 5, text, color))
                self.recent.appendleft({
                    "emp_id": to_record, "name": name, "code": self.gallery.codes.get(to_record, ""),
                    "kind": res["kind"], "time": datetime.now().strftime("%H:%M:%S"),
                    "status": res["status"], "late_min": res["late_min"],
                    "score": round(score, 2), "photo": res.get("photo")})
        if res["fresh"]:
            print(f"[{datetime.now():%H:%M:%S}] {name}: {res['kind']} score={score:.2f} "
                  f"margin={margin:.2f}", flush=True)
        if learn:
            try:
                self._auto_learn(to_record, cand, emb, score, margin)
            except Exception:
                traceback.print_exc()

    def _save_unknown(self, cand, score):
        """A face nobody matched: keep a photo and list it with the recognised people on
        the live page. Stored as unknown/<date>/<time>.jpg (deleted after PHOTO_DAYS)."""
        now = time.monotonic()
        if now - self.last_unknown < UNKNOWN_SNAP_GAP:
            return
        self.last_unknown = now
        snap = jpeg(make_snapshot(cand["frame"], cand["bbox"], height=240), 80)
        if snap is None:
            return
        when = datetime.now()
        key = f"unknown/{when:%Y-%m-%d}/{when:%H%M%S}{'_out' if self.direction == 'out' else ''}.jpg"
        try:
            put_photo(key, snap, None)
        except Exception as exc:
            print(f"Could not save unknown face photo: {exc}", flush=True)
            key = None
        with self.lock:
            self.recent.appendleft({
                "emp_id": None, "name": "Unknown person", "code": "", "kind": "UNKNOWN",
                "time": when.strftime("%H:%M:%S"), "status": "", "late_min": 0,
                "score": round(score, 2), "photo": key})
        print(f"[{when:%H:%M:%S}] Unknown person score={score:.2f}", flush=True)

    # ------------------------------------------------------------ enrollment
    def start_enroll(self, emp_id, name, replace=True):
        with self.lock:
            self.enroll = EnrollSession(emp_id, name, replace)

    def cancel_enroll(self):
        with self.lock:
            self.enroll = None

    def enroll_status(self):
        with self.lock:
            s = self.enroll
            if s is None:
                return {"active": False}
            counts = s.counts()
            step, text = s.next_step()
            return {
                "active": True, "emp_id": s.emp_id, "name": s.name,
                "poses": [{"pose": p, "need": n, "have": counts[p], "text": t,
                           "skipped": p in s.skipped} for p, n, t in POSES],
                "step": step, "message": text, "warning": s.warning,
                "hint": "" if s.warning else s.hint, "detected": s.detected,
                "can_skip": step in SKIPPABLE,
                "total": len(s.samples), "complete": step is None,
                "can_save": len(s.samples) >= MIN_ENROLL_SAMPLES and counts["front"] >= 2,
            }

    def skip_enroll_step(self):
        with self.lock:
            if self.enroll:
                self.enroll.skip()

    def _enroll_add(self, session, cand, emb):
        with self.lock:
            if self.enroll is not session:
                return
            samples = session.samples
            if len(samples) >= 2:
                centroid = np.mean([s["emb"] for s in samples], axis=0)
                centroid /= np.linalg.norm(centroid) + 1e-9
                if float(centroid @ emb) < 0.30:
                    session.warning = "A different face was seen - only the employee should be in view"
                    return
            same = [s for s in samples if s["pose"] == cand["pose"]]
            if len(same) >= POSE_NEED[cand["pose"]]:
                return
            if any(float(s["emb"] @ emb) > 0.95 for s in same):
                session.hint = "Move your head very slightly"   # same as a stored sample
                return
            cand = dict(cand, emb=emb, snap=make_snapshot(cand["frame"], cand["bbox"]))
            cand.pop("frame", None)
            samples.append(cand)
            session.last_activity = time.monotonic()

    def finish_enroll(self, emp_id):
        with self.lock:
            s = self.enroll
            if s is None or s.emp_id != emp_id:
                return None, True
            self.enroll = None
            return list(s.samples), s.replace

    def faces_from_image(self, img):
        """Largest face in an uploaded photo -> sample dict, or error string."""
        h, w = img.shape[:2]
        if max(h, w) > 1600:
            s = 1600.0 / max(h, w)
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        for rot in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180):
            src = img if rot is None else cv2.rotate(img, rot)
            faces = self.detector.detect(src, 640, 0.5)
            if faces:
                break
        if not faces:
            return "no face found"
        face = faces[0]
        if len(faces) > 1 and faces[1].width >= 0.7 * face.width:
            return "more than one face in the photo"
        if face.width < 50:
            return "face too small - use a closer photo"
        crop = align(src, face.kps)
        if crop is None:
            return "face could not be aligned"
        emb = self.embedder.embed_crops([crop])[0]
        yaw, pitch = face.pose()
        return {"crop": crop, "emb": emb, "pose": pose_bin(yaw, pitch) or "other",
                "q": float(face.width), "snap": make_snapshot(src, face.bbox)}

    def verify_selfie(self, emp_id, img):
        """Field attendance: is the selfie this employee? Returns (result, error)."""
        res = self.faces_from_image(img)
        if isinstance(res, str):
            return None, res
        if emp_id not in set(int(i) for i in self.gallery.ids):
            self.load_gallery(reset_tracks=False)   # registered moments ago on another PC
        own = self.gallery.best_own(emp_id, res["emb"])
        best_id, best_score, _ = self.gallery.match(res["emb"])
        verified = own >= FIELD_FACE_MIN and (best_id == emp_id or own >= best_score - 0.02)
        return {"score": round(own, 3), "verified": bool(verified)}, None

    def save_samples(self, emp_id, samples, source, replace):
        """Validate and store face samples. Returns (ok, message)."""
        if not samples:
            return False, "No usable face samples."
        embs = np.stack([s["emb"] for s in samples])
        centroid = embs.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        keep = [s for s, e in zip(samples, embs) if float(e @ centroid) >= 0.30]
        if len(keep) < min(3, len(samples)):
            return False, "The photos don't look like the same person. Please retake."
        votes = Counter()
        for s in keep:
            other, score, _ = self.gallery.match(s["emb"], exclude=emp_id)
            if other is not None and score >= config.MATCH_THRESHOLD + 0.1:
                votes[other] += 1
        if votes:
            other, n = votes.most_common(1)[0]
            if n >= max(2, len(keep) // 3):
                return False, (f"This face matches the already registered employee "
                               f"'{self.gallery.name(other)}' ({self.gallery.codes.get(other, '')}).")
        coll = db().face_samples
        if replace:
            coll.delete_many({"emp_id": emp_id})
        room = max(0, MAX_SAMPLES_PER_EMP - coll.count_documents({"emp_id": emp_id}))
        keep.sort(key=lambda s: -s["q"])
        stored = keep[:room]
        if stored:
            coll.insert_many([{
                "emp_id": emp_id, "pose": s["pose"], "source": source,
                "crop": Binary(jpeg(s["crop"], 95)),
                "embedding": Binary(s["emb"].astype(np.float32).tobytes()),
                "model": config.REC_MODEL, "created_at": now_str()} for s in stored])
        fronts = [s for s in keep if s["pose"] == "front" and s.get("snap") is not None] or \
                 [s for s in keep if s.get("snap") is not None]
        if fronts:
            best = max(fronts, key=lambda s: s["q"])
            put_photo(f"profile/{emp_id}.jpg", jpeg(best["snap"], 85), emp_id, expires=False)
        self.load_gallery()
        return True, f"Saved {len(stored)} face samples."

    # ------------------------------------------------------------ status
    def stats(self):
        with self.lock:
            tracks = len(self.tracks)
            recent = list(self.recent)[:15]
        out = self.out
        return {
            "camera": self.camera.status(),
            "det_fps": round(self.det_fps, 1),
            "det_ms": round(self.det_ms),
            "rec_ms": round(self.rec_ms),
            "faces": tracks,
            "employees": len(self.gallery.ids),
            "models": f"{config.DET_MODEL} + {config.REC_MODEL}",
            "recent": recent,
            # exit camera: None when only one camera is used
            "out_camera": out.camera.status() if out is not None else None,
            "out_det_fps": round(out.det_fps, 1) if out is not None else None,
            "out_faces": len(out.tracks) if out is not None else None,
        }
