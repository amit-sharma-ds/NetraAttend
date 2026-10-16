# NetraAttend on a free cloud server (24x7 API + website)

Result: **https://YOURNAME.duckdns.org** - website, employee panel, field attendance and the API
run 24x7 on Oracle Cloud (free). The office PC keeps running the camera (deploy\INSTALL.bat).
Both use the same MongoDB, so everything stays in sync.

```
OFFICE PC (camera)  --->  MongoDB Atlas  <---  ORACLE CLOUD (website + API, https)
```

Total cost: Rs 0 (Oracle Always Free + DuckDNS + Let's Encrypt + MongoDB Atlas free).

---

## 1. Oracle Cloud account (10 min, you do this)
1. Go to **https://www.oracle.com/cloud/free/** -> *Start for free*.
2. **Home region: India West (Mumbai)** or India South (Hyderabad). It cannot be changed later.
3. Card details are asked only to verify - nothing is charged on Always Free.
4. Recommended after sign-up: *Upgrade to Pay As You Go*. Free resources stay free (Rs 0),
   and Oracle no longer deletes "idle" free servers.

## 2. Create the server (5 min)
1. Menu -> **Compute -> Instances -> Create instance**.
2. Image: **Canonical Ubuntu 24.04**.
3. Shape: **Ampere -> VM.Standard.A1.Flex**, 1 OCPU, 6 GB memory (Always Free).
   If it says "out of capacity", try again later or pick another availability domain.
4. Networking: keep "Assign a public IPv4 address" ON.
5. SSH keys: **Generate a key pair** -> **Save private key** (e.g. `netra.key`). Keep it safe.
6. Create. Copy the **Public IP address** shown on the instance page.

## 3. Open ports 80 and 443 (2 min)
Instance page -> **Subnet** -> **Security Lists** -> default list -> **Add Ingress Rules**:
- Source CIDR `0.0.0.0/0`, TCP, destination port `80`
- Source CIDR `0.0.0.0/0`, TCP, destination port `443`

## 4. Free name with DuckDNS (2 min)
1. **https://www.duckdns.org** -> sign in (Google / GitHub).
2. Add a sub domain, e.g. `netraattend` -> put the server's **Public IP** in *current ip* -> *update ip*.
   Your address is now `netraattend.duckdns.org`.

## 5. Allow the server in MongoDB Atlas (1 min)
Atlas -> **Network Access** -> **Add IP Address** -> the server's **Public IP** -> Confirm.

## 6. Put NetraAttend on the server (10 min)
On this office PC, in PowerShell, in the folder where `netra.key` and `NetraAttend-cloud.zip` are:

```powershell
icacls netra.key /inheritance:r /grant:r "$($env:USERNAME):R"
scp -i netra.key NetraAttend-cloud.zip ubuntu@PUBLIC_IP:~
ssh -i netra.key ubuntu@PUBLIC_IP
```

Now you are on the server:

```bash
sudo apt-get update && sudo apt-get install -y unzip
unzip NetraAttend-cloud.zip && cd NetraAttend
bash deploy/cloud_install.sh        # 1st run: installs Docker, creates .env
nano .env                           # fill DOMAIN, NETRA_MONGO_URI, NETRA_SECRET_KEY, NETRA_ADMIN_PASSWORD
bash deploy/cloud_install.sh        # 2nd run: builds and starts (5-10 min the first time)
```

In `nano`: edit, then **Ctrl+O, Enter** to save and **Ctrl+X** to exit.
`NETRA_ADMIN_PASSWORD` must be strong - this website is on the internet.

## 7. Done
- Website: **https://netraattend.duckdns.org**
- API docs (send to sir): **https://netraattend.duckdns.org/api/docs**
- Employees (field / leave / attendance) use the same link on their phones - location works on https.

## Later
- Update after a code change: upload the new zip, unzip over the old folder, run `bash deploy/cloud_install.sh`.
- Logs: `sudo docker compose logs -f app`   - Restart: `sudo docker compose restart`
- Camera registration and the live camera view work on the office PC's website
  (http://<office-pc-ip>:5000). Photo-upload registration works on the cloud website too.
