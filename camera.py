"""Low-latency camera reader.

A background thread reads the stream as fast as the camera sends it and keeps
only the newest frame, so the engine never works on a stale, buffered frame.
RTSP runs over TCP (no grey/smeared frames from lost UDP packets) and the
reader reconnects by itself if the camera or network drops.
"""
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

# must be set before the first VideoCapture is opened
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000")

import cv2  # noqa: E402

import config  # noqa: E402

ROTATIONS = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}
STUCK_REBOOT_AFTER = 90        # seconds of "stream busy" before rebooting the camera
REBOOT_MIN_INTERVAL = 30 * 60  # never reboot more often than this


def mask_url(source):
    """Hide the password in rtsp://user:pass@host URLs for logs and the UI."""
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", str(source))


def rtsp_check(url, timeout=5):
    """Why a camera does not stream: 'ok', 'busy' (stream exists but the camera
    refuses to start it - its connection slot is held by a dead client),
    'missing' (wrong path), 'auth' or 'down' (not reachable)."""
    p = urlsplit(url)
    clean = urlunsplit((p.scheme, f"{p.hostname}:{p.port or 554}", p.path, p.query, ""))
    try:
        sock = socket.create_connection((p.hostname, p.port or 554), timeout=timeout)
    except OSError:
        return "down"

    def request(lines):
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        lines_ = head.decode(errors="replace").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines_[1:])}
        length = int(headers.get("content-length", 0) or 0)
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        return int(lines_[0].split()[1]), headers, body.decode(errors="replace")

    try:
        code, headers, sdp = request([f"DESCRIBE {clean} RTSP/1.0", "CSeq: 1", "Accept: application/sdp"])
        if code == 401:
            return "auth"
        if code != 200:
            return "missing"
        base = headers.get("content-base", clean + "/")
        video = sdp.split("m=video", 1)[-1]
        control = re.search(r"a=control:(\S+)", video)
        control = control.group(1) if control else ""
        target = control if control.startswith("rtsp://") else base.rstrip("/") + "/" + control
        code, headers, _ = request([f"SETUP {target} RTSP/1.0", "CSeq: 2",
                                    "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"])
        if code != 200:
            return "busy"
        session = headers.get("session", "").split(";")[0]
        request([f"TEARDOWN {clean} RTSP/1.0", "CSeq: 3", f"Session: {session}"])  # give it back
        return "ok"
    except (OSError, ValueError, IndexError):
        return "down"
    finally:
        sock.close()


def camera_api(url, form, data=None, timeout=8):
    """POST to the camera's web API (/digest/<form>) with its login from the RTSP
    link. The camera speaks normal MD5 digest but may name the scheme "X-Digest",
    which urllib refuses, so the digest answer is built here."""
    p = urlsplit(url)
    user, pwd = p.username or "", p.password or ""
    path = f"/digest/{form}"
    full = f"http://{p.hostname}{path}"
    body = json.dumps({"Type": 0, "Dev": 1, "Ch": 1, "Data": data or {}}).encode()
    headers = {"Content-Type": "application/json"}

    def post():
        req = urllib.request.Request(full, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")

    try:
        return post()
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        challenge = exc.headers.get("WWW-Authenticate", "")
    scheme, _, params = challenge.partition(" ")
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', params))
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    nc, cnonce, qop = "00000001", secrets.token_hex(8), f.get("qop", "auth")
    answer = md5(f"{md5(f'{user}:{f.get('realm', '')}:{pwd}')}:{f['nonce']}:{nc}:{cnonce}:{qop}:"
                 f"{md5('POST:' + path)}")
    headers["Authorization"] = (
        f'{scheme} username="{user}", realm="{f.get("realm", "")}", nonce="{f["nonce"]}", '
        f'uri="{path}", response="{answer}", opaque="{f.get("opaque", "")}", qop={qop}, '
        f'nc={nc}, cnonce="{cnonce}"')
    return post()


def reboot_camera(url):
    """Restart the camera (same as Setup > Device Maintenance > Reboot)."""
    try:
        camera_api(url, "frmDeviceReboot")
        return True
    except Exception as exc:
        print(f"Camera reboot request failed: {exc}", flush=True)
        return False


class CameraStream:
    def __init__(self, source, rotate=0, known_good=False, on_connect=None):
        if isinstance(source, str) and source.strip().isdigit():
            source = int(source)
        if rotate not in ROTATIONS:
            raise ValueError("CAMERA_ROTATE must be 0, 90, 180 or 270")
        self.source = source
        self.known_good = known_good      # this link has streamed before
        self.on_connect = on_connect
        self.rotate = ROTATIONS[rotate]
        self.is_file = isinstance(source, str) and os.path.isfile(source)
        self._cond = threading.Condition()
        self._frame = None
        self._id = 0
        self._ts = 0.0
        self.running = False
        self.thread = None
        self.connected = False
        self.fps = 0.0
        self.size = None
        self.error = ""
        self.reconnects = 0

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name="camera")
        self.thread.start()

    def stop(self):
        self.running = False
        with self._cond:
            self._cond.notify_all()
        if self.thread:
            self.thread.join(timeout=5)

    def _open(self):
        if isinstance(self.source, int):
            api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
            cap = cv2.VideoCapture(self.source, api)
        else:
            cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _maybe_reboot(self, failing_since):
        """This camera keeps the connection of a client that died (PC crash, app
        killed) and then refuses everyone with "404 Stream Not Found". Detect that
        exact case and restart the camera, instead of waiting for a human."""
        # only for a link that has streamed before: this camera answers DESCRIBE even
        # for a mistyped path, so a typo would otherwise look like a stuck camera
        if (not getattr(config, "CAMERA_AUTO_REBOOT", True) or not self.known_good
                or not isinstance(self.source, str) or not self.source.startswith("rtsp://")
                or time.monotonic() - failing_since < STUCK_REBOOT_AFTER
                or time.monotonic() - CameraStream._last_reboot < REBOOT_MIN_INTERVAL):
            return
        state = rtsp_check(self.source)
        if state != "busy":
            return
        CameraStream._last_reboot = time.monotonic()
        print("Camera answers but will not stream (connection held by a dead client) - "
              "rebooting the camera", flush=True)
        if reboot_camera(self.source):
            self.error = "Camera was stuck - restarting it, back in about 30 seconds"

    _last_reboot = -1e9

    def _loop(self):
        backoff = 1.0
        failing_since = time.monotonic()
        while self.running:
            cap = self._open()
            if cap is None:
                self.connected = False
                self.error = f"Cannot open camera {mask_url(self.source)}; retrying"
                print(self.error, flush=True)
                self._maybe_reboot(failing_since)
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
                continue
            backoff = 1.0
            self.connected = True
            self.error = ""
            file_delay = 0.0
            if self.is_file:
                fps = cap.get(cv2.CAP_PROP_FPS) or 25
                file_delay = 1.0 / max(1.0, min(fps, 60.0))
            print(f"Camera connected: {mask_url(self.source)}", flush=True)
            if not self.known_good and self.on_connect:
                try:
                    self.on_connect(self.source)
                    self.known_good = True
                except Exception as exc:
                    print(f"Could not remember working camera link: {exc}", flush=True)
            count, t0 = 0, time.monotonic()
            while self.running:
                t_read = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    if self.is_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.error = "Camera stream lost; reconnecting"
                    print(self.error, flush=True)
                    break
                if self.rotate is not None:
                    frame = cv2.rotate(frame, self.rotate)
                with self._cond:
                    self._frame = frame
                    self._id += 1
                    self._ts = time.time()
                    self._cond.notify_all()
                self.size = (frame.shape[1], frame.shape[0])
                count += 1
                elapsed = time.monotonic() - t0
                if elapsed >= 2.0:
                    self.fps = count / elapsed
                    count, t0 = 0, time.monotonic()
                if file_delay:
                    time.sleep(max(0.0, file_delay - (time.monotonic() - t_read)))
            cap.release()
            self.connected = False
            self.fps = 0.0
            failing_since = time.monotonic()
            if self.running:
                self.reconnects += 1
                time.sleep(1.0)

    def latest(self):
        """(frame_id, frame, timestamp). Frames are never modified after publishing."""
        with self._cond:
            return self._id, self._frame, self._ts

    def wait_frame(self, last_id, timeout=1.0):
        with self._cond:
            self._cond.wait_for(lambda: self._id != last_id or not self.running, timeout)
            if self._id == last_id:
                return last_id, None, 0.0
            return self._id, self._frame, self._ts

    def status(self):
        return {
            "source": mask_url(self.source),
            "connected": self.connected,
            "fps": round(self.fps, 1),
            "size": f"{self.size[0]}x{self.size[1]}" if self.size else "",
            "error": self.error,
            "reconnects": self.reconnects,
            "age": round(time.time() - self._ts, 1) if self._ts else None,
        }
