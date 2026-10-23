# NetraAttend – CCTV Face Recognition Attendance

Real-time face recognition and automatic attendance from a CCTV camera (RTSP).
The website has a dashboard, live camera, employees, reports (Excel) and settings.

## Deployment (24x7 service on this PC)

1. **One time:** Double-click `deploy\INSTALL.bat` → click "Yes" (admin permission). This does the following:
   - Turns NetraAttend into a **background service**: it starts automatically when the PC turns on, no login
     needed, and it restarts by itself if it stops.
   - Firewall: opens port 5000 for the office network only (local only, not the internet).
   - Disables PC sleep / hibernate (when plugged in).
   - Takes a MongoDB **backup** every night at 23:30 → `backups\` folder (keeps the last 30).
2. **One time in BIOS:** set "Restore on AC Power Loss" = **Power On** (the PC turns itself on as soon as power returns).
3. Links: on this PC http://localhost:5000 · on the office network `http://<this-PC's-IP>:5000` · API docs at `/api/docs`.
4. Logs: `logs\netraattend.log`. After a code update, restart by running `deploy\INSTALL.bat` again.
   To remove the service: `deploy\UNINSTALL.bat` (MongoDB data stays safe).
5. Restoring a backup (this REPLACES the current data):
   `.venv-service\Scripts\python.exe backup.py --restore backups\netraattend-YYYY-MM-DD_HHMM.zip --yes`

On a new PC: install Python (the python.org version – the Microsoft Store version does not work as a service) → `setup.bat` →
fill in `config.py` (camera, MongoDB, admin password) → `deploy\INSTALL.bat`.
To run in a window for testing: `start.bat`.

Admin login: `ADMIN_USER` / `ADMIN_PASSWORD` in `config.py` (be sure to change the default password).

## Cloud deployment – Render (website + API 24x7, fixed link)

The camera and face recognition stay on the office PC; the website, employee panel,
field attendance and API run on Render. Both use the same MongoDB Atlas.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/amit-sharma-ds/NetraAttend)

1. MongoDB Atlas → **Network Access** → "Allow access from anywhere" (0.0.0.0/0).
2. Click the button above → Render will ask for two things: `NETRA_MONGO_URI` (the link from the office PC's `config.py`)
   and `NETRA_ADMIN_PASSWORD` (use a strong password – admin login is exposed to the internet).
3. Deploy (first time takes ~5-10 min). Link: `https://netraattend.onrender.com` (or whatever name Render gives).

Free plan: the service sleeps after 15 min without visitors – `NETRA_KEEP_AWAKE=True` visits itself
every 10 min to stay awake (750 free hours/month, shared across the whole workspace). Live camera view works only
on the office network. Setup file: `render.yaml`.

## How it works

1. Go to the **Employees** page → enter name + Employee ID → **Register face**.
2. The employee stands alone in front of the CCTV camera and follows the on-screen instructions
   (straight, left, right, up, down). Once all poses are done, it saves automatically.
   Alternatively, upload 3–10 photos (front + left + right).
3. Now, as soon as the employee appears on camera → the first time = **IN** (check-in). Any later appearance after a gap of 3+ min
   = a new **OUT** movement (time + photo). Hours = first IN → last OUT.
4. Appearance changed (beard, glasses)? The app learns small changes by itself. For a big change,
   go to Employees → Re-register face, untick "Replace" and scan (the new look gets added).

## Field attendance (work outside the office – e.g. PLC engineers)

1. Admin: Employees → Edit → tick **Field work allowed** (only for those who work outside)
   and set the employee's login password.
2. The employee opens the website on their phone → logs in with Employee ID + password → **Field attendance**:
   take a selfie + live location → **Mark IN** (work starts) / **Mark OUT** (work ends).
3. The selfie face is matched against the registered face. If it doesn't match, the entry is marked "Not verified".
4. Admin: on the **Field work** page, date-wise: selfie, location on map, accuracy, verified or not, note.
5. Same hours rule: first IN to last OUT (camera and field combined).

Important: on phones, **live location only works on an https:// link**. So for use from outside, the
app needs a secure internet link (e.g. Cloudflare Tunnel). For people coming in from the internet,
admin pages stay closed (`ADMIN_LAN_ONLY = True`); only the employee panel opens.

## API (for punching machines / payroll / HR software)

- Admin → **API** page → create a key (name + permission: *Read* and/or *Send punches*). The key is shown only once.
- Documentation: **http://<this-PC's-IP>:5000/api/docs** (give this link to the machine vendor / payroll provider).
- Every request needs the header: `X-API-Key: na_...`

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/employees` | employee list |
| `GET /api/v1/attendance?from=&to=` | day-wise IN, OUT, worked minutes, status, late, leave + summary |
| `GET /api/v1/punches?since=` | every IN/OUT punch (for syncing, use `next_since`) |
| `POST /api/v1/punches` | machine sends a punch: `{"emp_code": "102", "time": "2026-10-16 10:38:00", "device": "gate-1"}` |
| `GET /api/v1/leaves?from=&to=` | leave list |
| `GET /api/v1/health` | system / camera status |

The Employee ID (`emp_code`) must be the same in every system. If the same punch arrives again, it is not counted as a duplicate.
For API access from outside (internet), an https link is needed (Cloudflare Tunnel).

## Attendance rules (change from the Settings page)

| Setting | Default |
|---|---|
| Office start | 10:30 |
| Grace | 20 min → **On Time** until 10:50, **Late** after that (+minutes) |
| Half day | arrives after 13:30, or works less than 4 hours |
| Weekly off | Sunday |
| Holidays | you can add a list |

If the camera misses someone, you can enter the time via **Manual entry** on the dashboard.

## Database – MongoDB Atlas

All data is in MongoDB (`config.py` → `MONGO_URI`, database `netraattend`). Nothing is saved on
this PC. Collections visible in Compass:

| Collection | What it holds |
|---|---|
| `employees` | name, Employee ID, department, active |
| `face_samples` | face data (numeric codes + small face photo) |
| `attendance` | each day: IN, last OUT, photo, source (camera/manual) |
| `events` | every movement: IN / OUT time + photo |
| `photos` | attendance photos – **auto-deleted after `PHOTO_DAYS` (60) days** (storage won't fill up) |
| `settings` | office time, grace, camera link, etc. |

- If the internet goes down briefly, attendance is held in memory and saved to MongoDB with the original
  time once the internet returns (as long as the app isn't closed).
- Download **Excel** from the Reports page.
- This PC's IP must be allowed in Atlas → Network Access. Don't share the MongoDB password with anyone.

## Models (config.py)

- Detection: **SCRFD** (`det_500m`, fast) – InsightFace
- Recognition: **ArcFace** – `w600k_mbf` (fast, for this i3 PC) or `w600k_r50` (best accuracy,
  for i5/i7 or better PCs). When you change the model, saved faces are updated automatically at startup.
- Models are not in the git repo. Download: `python download_models.py` (default) or
  `python download_models.py --large` (det_10g + w600k_r50). `setup.bat` and the Docker build do this automatically.
- **License:** InsightFace's pretrained models are free only for *non-commercial research* use.
  For company / commercial use, get a commercial license from InsightFace (https://insightface.ai)
  or use commercially-licensed models.

## Camera tips (most important for accuracy)

- Currently `/stream1` (640x480) is being used. If faces aren't recognized from a distance, try the
  main stream `/h264/ch1/main/av_stream` (2560x1440) in config.py – the face appears 4x larger, but it uses more CPU.
- **This camera allows only 1 RTSP connection at a time.** So run only ONE copy of NetraAttend,
  and don't open the RTSP link in VLC/other software at the same time (the camera's own web page will still work).
- **If the camera IP changes:** Settings → Camera → new IP → **Save & connect**. No restart needed.
- If the camera gets "stuck" ("404 Stream Not Found" – happens after a PC crash / power loss): the app
  **restarts the camera by itself** after 90 sec (max once per 30 min; `CAMERA_AUTO_REBOOT`).
  This only happens for a link that has worked before.
- On the camera web page (http://192.168.1.181), set the main stream **FPS to 10–12** → CPU usage is halved.
- Best result: a camera at the entrance door at **eye level (~2 m height)** that sees faces of people
  coming in head-on, with good lighting. A camera looking down from above sees fewer faces.
- Be sure to change the camera's default password and fix the camera's IP in the router (DHCP reservation).
- Run only this app on this PC (close Chrome/other heavy apps) – the i3 CPU is small.
