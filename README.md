# NetraAttend – CCTV Face Recognition Attendance

CCTV camera (RTSP) se real-time face recognition aur automatic attendance.
Website par dashboard, live camera, employees, reports (Excel) aur settings.

## Deployment (is PC par 24x7 service)

1. **Ek baar:** `deploy\INSTALL.bat` double-click karo → "Yes" (admin permission). Ye karta hai:
   - NetraAttend ko **background service** bana deta hai: PC on hote hi apne aap chalu, login ki zarurat
     nahi, band ho jaye to apne aap restart.
   - Firewall: office network (sirf local, internet nahi) port 5000 khol sake.
   - PC sleep / hibernate band (plugged in hone par).
   - Har raat 23:30 MongoDB **backup** → `backups\` folder (last 30 rakhta hai).
2. **BIOS me ek baar:** "Restore on AC Power Loss" = **Power On** (bijli aate hi PC khud on ho).
3. Link: is PC par http://localhost:5000 · office network par `http://<is-PC-ka-IP>:5000` · API docs `/api/docs`.
4. Log: `logs\netraattend.log`. Code update ke baad restart: `deploy\INSTALL.bat` dobara chalao.
   Service hatana: `deploy\UNINSTALL.bat` (MongoDB data safe rehta hai).
5. Backup wapas lana (current data REPLACE hota hai):
   `.venv-service\Scripts\python.exe backup.py --restore backups\netraattend-YYYY-MM-DD_HHMM.zip --yes`

Naye PC par: Python (python.org wala - Microsoft Store wala service me nahi chalta) → `setup.bat` →
`config.py` bharo (camera, MongoDB, admin password) → `deploy\INSTALL.bat`.
Testing ke liye window me chalana ho to: `start.bat`.

Admin login: `config.py` me `ADMIN_USER` / `ADMIN_PASSWORD` (default password zaroor badlo).

## Cloud deployment – Render (website + API 24x7, fixed link)

Camera aur face recognition office PC par hi rehte hain; Render par website, employee panel,
field attendance aur API chalti hai. Dono same MongoDB Atlas use karte hain.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/amit-sharma-ds/NetraAttend)

1. MongoDB Atlas → **Network Access** → "Allow access from anywhere" (0.0.0.0/0).
2. Upar wala button → Render 2 cheezein poochega: `NETRA_MONGO_URI` (office PC ke `config.py`
   wala link) aur `NETRA_ADMIN_PASSWORD` (strong password – internet se admin login khula hai).
3. Deploy (pehli baar ~5-10 min). Link: `https://netraattend.onrender.com` (ya Render jo naam de).

Free plan: 15 min bina visitor ke so jata hai – `NETRA_KEEP_AWAKE=True` har 10 min khud ko visit
karke jagaye rakhta hai (750 free hours/month poore workspace me shared). Live camera view sirf
office network par. Setup file: `render.yaml`.

## Kaam kaise karta hai

1. **Employees** page → naam + Employee ID daalo → **Register face**.
2. Employee CCTV camera ke saamne akela khada ho; screen par instructions follow kare
   (seedha, left, right, upar, neeche). Sab pose poore hote hi apne aap save.
   Ya 3–10 photos upload karo (front + left + right).
3. Ab camera me jaise hi employee dikhe → pehli baar = **IN** (check-in). Baad me jab bhi 3+ min ke
   gap ke baad dikhe = naya **OUT** movement (time + photo). Hours = pehla IN → aakhri OUT.
4. Shakal badli (beard, chashma)? Chhota badlav app khud seekh leta hai. Bada badlav ho to
   Employees → Re-register face, "Replace" ka tick hata ke scan karo (naya look add hota hai).

## Field attendance (office ke bahar kaam – jaise PLC engineer)

1. Admin: Employees → Edit → **Field work allowed** tick karo (sirf unke liye jo bahar kaam karte hain)
   aur employee ka login password set karo.
2. Employee apne phone par website kholta hai → Employee ID + password se login → **Field attendance**:
   selfie lo + live location → **Mark IN** (kaam shuru) / **Mark OUT** (kaam khatam).
3. Selfie ka chehra registered face se milaya jaata hai. Na mile to entry "Not verified" ho jaati hai.
4. Admin: **Field work** page par date-wise: selfie, map par location, accuracy, verified ya nahi, note.
5. Hours wahi rule: pehla IN se aakhri OUT (camera aur field dono milake).

Zaruri: phone par **live location sirf https:// link par chalti hai**. Isliye bahar se use karne ke liye
app ko internet par secure link chahiye (jaise Cloudflare Tunnel). Internet se aane wale logon ke liye
admin pages band rehte hain (`ADMIN_LAN_ONLY = True`), sirf employee panel khulta hai.

## API (punching machine / payroll / HR software ke liye)

- Admin → **API** page → key banao (naam + permission: *Read* aur/ya *Send punches*). Key sirf ek baar dikhti hai.
- Documentation: **http://<is-PC-ka-IP>:5000/api/docs** (machine vendor / payroll wale ko ye link do).
- Har request me header: `X-API-Key: na_...`

| Endpoint | Kaam |
|---|---|
| `GET /api/v1/employees` | employee list |
| `GET /api/v1/attendance?from=&to=` | din-wise IN, OUT, worked minutes, status, late, leave + summary |
| `GET /api/v1/punches?since=` | har IN/OUT punch (sync ke liye, `next_since` use karo) |
| `POST /api/v1/punches` | machine punch bheje: `{"emp_code": "102", "time": "2026-10-16 10:38:00", "device": "gate-1"}` |
| `GET /api/v1/leaves?from=&to=` | leave list |
| `GET /api/v1/health` | system / camera status |

Employee ID (`emp_code`) har system me same hona chahiye. Ek hi punch dobara aaye to duplicate count nahi hota.
Bahar (internet) se API ke liye https link chahiye (Cloudflare Tunnel).

## Attendance rules (Settings page se badlo)

| Setting | Default |
|---|---|
| Office start | 10:30 |
| Grace | 20 min → 10:50 tak **On Time**, uske baad **Late** (+minutes) |
| Half day | 13:30 ke baad aaye, ya 4 ghante se kam kaam |
| Weekly off | Sunday |
| Holidays | list daal sakte ho |

Camera chuk jaye to dashboard par **Manual entry** se time daal sakte ho.

## Database – MongoDB Atlas

Sab data MongoDB me hai (`config.py` → `MONGO_URI`, database `netraattend`). Is PC par kuch save
nahi hota. Compass me dikhne wale collections:

| Collection | Kya hai |
|---|---|
| `employees` | naam, Employee ID, department, active |
| `face_samples` | face data (number codes + chhoti face photo) |
| `attendance` | har din: IN, last OUT, photo, source (camera/manual) |
| `events` | har movement: IN / OUT time + photo |
| `photos` | attendance photos – **`PHOTO_DAYS` (60) din baad apne aap delete** (storage full nahi hoga) |
| `settings` | office time, grace, camera link wagairah |

- Internet thodi der band ho to attendance memory me rukti hai aur internet aate hi original time
  ke saath MongoDB me save ho jaati hai (app band na ho tab tak).
- Reports page se **Excel download**.
- Atlas → Network Access me is PC ka IP allowed hona chahiye. MongoDB password kisi ko share na karo.

## Models (config.py)

- Detection: **SCRFD** (`det_500m`, fast) – InsightFace
- Recognition: **ArcFace** – `w600k_mbf` (fast, is i3 PC ke liye) ya `w600k_r50` (best accuracy,
  i5/i7 ya better PC par). Model badalne par saved faces startup par apne aap update ho jaate hain.
- Models git repo me nahi hain. Download: `python download_models.py` (default) ya
  `python download_models.py --large` (det_10g + w600k_r50). `setup.bat` aur Docker build ye khud karte hain.
- **License:** InsightFace ke pretrained models sirf *non-commercial research* use ke liye free hain.
  Company / commercial use ke liye InsightFace se commercial license lo (https://insightface.ai)
  ya commercially-licensed models use karo.

## Camera tips (accuracy ke liye sabse zaruri)

- Abhi `/stream1` (640x480) use ho raha hai. Agar door se chehra pehchan na aaye to config.py me
  main stream `/h264/ch1/main/av_stream` (2560x1440) try karo - chehra 4x bada dikhega, CPU zyada lagega.
- **Ye camera ek time par sirf 1 RTSP connection deta hai.** Isliye NetraAttend ki sirf EK copy chalao,
  aur saath me VLC/doosra software RTSP link na khole (camera ka apna web page chalega).
- **Camera IP badle** to: Settings → Camera → naya IP → **Save & connect**. Restart ki zarurat nahi.
- Camera "atak" jaye ("404 Stream Not Found" – PC crash / bijli jaane ke baad hota hai): app 90 sec
  baad **camera ko khud restart** kar deta hai (30 min me max 1 baar; `CAMERA_AUTO_REBOOT`).
  Ye sirf us link ke liye hota hai jo pehle chal chuka ho.
- Camera web page (http://192.168.1.181) me main stream **FPS 10–12** kar do → CPU aadha lagega.
- Best result: entrance door par **eye-level (~2 m height)** camera jo aate hue logon ka chehra
  seedha dekhe, achhi light ke saath. Upar se neeche dekhne wale camera me chehra kam dikhta hai.
- Camera ka default password zaroor badlo aur router me camera ka IP fix (DHCP reservation) kar do.
- Is PC par sirf yahi app chalao (Chrome/doosre heavy apps band) – i3 CPU chhota hai.
