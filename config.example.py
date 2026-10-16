"""NetraAttend configuration.

Copy this file to config.py and edit it. Office timings (start time, grace
minutes, half-day rules, weekly off, holidays) are edited from the website's
Settings page, not here.

Any value can also be overridden with an environment variable named
NETRA_<NAME>, e.g. NETRA_CAMERA=0 to test with a webcam.
"""

# ---------------- Camera ----------------
# RTSP URL of the CCTV camera. This camera: /stream1 = 640x480 (light on CPU),
# /h264/ch1/main/av_stream = 2560x1440 (sharper faces from farther away, more CPU).
# Use 0 for a USB webcam / DroidCam, or a video file path for testing.
CAMERA = "rtsp://USERNAME:PASSWORD@192.168.1.181:554/stream1"
CAMERA_ROTATE = 0            # 0, 90, 180 or 270
CAMERA_AUTO_REBOOT = True    # restart the camera if it stays stuck ("404 Stream Not Found")

# Part of the frame to scan for faces, as fractions (x1, y1, x2, y2).
# Cropping to the entrance/door area makes far faces bigger and saves CPU.
# Example: (0.25, 0.0, 0.75, 1.0) = middle half of the image. None = full frame.
SCAN_REGION = None

# ---------------- Database (MongoDB Atlas) ----------------
MONGO_URI = "mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/"
MONGO_DB = "netraattend"     # database name (shown in Compass)
PHOTO_DAYS = 60              # attendance photos are deleted automatically after this many days

# ---------------- Website ----------------
ROLE = "all"                 # "all" = office PC (camera + website + API) - "web" = cloud server (no camera)
HOST = "0.0.0.0"             # 0.0.0.0 = reachable from other PCs/phones on the LAN
PORT = 5000
SECRET_KEY = "change-me-to-a-long-random-string"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "change-me"
ADMIN_LAN_ONLY = True        # admin pages only from the office network (field staff may reach the app from the internet)

# ---------------- Models & speed ----------------
# Detector: "det_500m" (fast, good for faces up to ~4 m) or "det_10g"
#           (more accurate for far/small faces, needs a faster CPU or GPU).
# Recognizer: "w600k_mbf" (fast - right for an i3 without GPU) or "w600k_r50"
#             (most accurate, ~10x slower; use on an i5/i7 or better). Switching
#             is automatic - saved faces are re-computed at startup.
DET_MODEL = "det_500m"
REC_MODEL = "w600k_mbf"
DET_WIDTH = 960              # detector input width; 800-960 finds smaller faces but is slower
DET_THRESHOLD = 0.5
DET_THREADS = 2
REC_THREADS = 2
DET_FPS = 8                  # face detection rate while people are visible
IDLE_DET_FPS = 3             # detection rate when nobody has been seen for a few seconds

STREAM_WIDTH = 960           # live view size in the browser
STREAM_FPS = 10
STREAM_QUALITY = 70

# ---------------- Recognition ----------------
MIN_FACE_PX = 36             # smaller faces are tracked but not recognised ("come closer")
MIN_ENROLL_FACE_PX = 44      # registration needs a slightly bigger face
MATCH_THRESHOLD = 0.40       # cosine similarity needed to count a frame as a match
STRONG_MATCH = 0.55          # a single frame this confident confirms immediately
MATCH_MARGIN = 0.05          # best match must beat the 2nd-best person by this much
VOTES_NEEDED = 2             # matching frames needed to confirm an identity
MAX_YAW = 0.65               # skip extreme side profiles (0 = frontal, ~0.5 = 45 degrees)
MIN_SHARPNESS = 15.0         # skip very blurry face crops

# ---------------- Attendance ----------------
COOLDOWN_SEC = 60            # min seconds between database updates for the same person
