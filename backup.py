"""Daily MongoDB backup - no extra tools needed.

  python backup.py                         backup -> backups/netraattend-YYYY-MM-DD_HHMM.zip
  python backup.py --photos                also include attendance photos (bigger file)
  python backup.py --restore FILE --yes    put a backup back (REPLACES those collections)

The "NetraAttend Backup" scheduled task runs the first form every night.
Old backups beyond config.BACKUP_KEEP (default 30) are deleted.
Copy the backups folder to a pen drive / Google Drive now and then.
"""
import argparse
import glob
import os
import sys
import zipfile
from datetime import datetime

import bson

import config
from db import apply_env_overrides, db, init_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

COLLECTIONS = ["employees", "face_samples", "attendance", "events", "leaves", "corrections",
               "settings", "counters", "api_keys"]


def backup_dir():
    path = getattr(config, "BACKUP_DIR", "backups")
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def make_backup(with_photos=False):
    out_dir = backup_dir()
    os.makedirs(out_dir, exist_ok=True)
    name = os.path.join(out_dir, f"netraattend-{datetime.now():%Y-%m-%d_%H%M}.zip")
    d = db()
    counts = {}
    tmp = name + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for coll in COLLECTIONS + (["photos"] if with_photos else []):
            n = 0
            with z.open(f"{coll}.bson", "w") as fh:          # same layout as mongodump
                for doc in d[coll].find():
                    fh.write(bson.encode(doc))
                    n += 1
            counts[coll] = n
        z.writestr("INFO.txt", f"NetraAttend backup {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                               f"database: {config.MONGO_DB}\n" +
                   "".join(f"{k}: {v}\n" for k, v in counts.items()))
    os.replace(tmp, name)
    keep = int(getattr(config, "BACKUP_KEEP", 30))
    old = sorted(glob.glob(os.path.join(out_dir, "netraattend-*.zip")))[:-keep]
    for f in old:
        os.remove(f)
    size = os.path.getsize(name) / 1e6
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] backup saved: {name} ({size:.1f} MB) {counts}"
          + (f" - removed {len(old)} old" if old else ""), flush=True)
    return name


def restore(path):
    d = db()
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():
            if not member.endswith(".bson"):
                continue
            coll = member[:-5]
            docs = bson.decode_all(z.read(member))
            d[coll].delete_many({})
            for i in range(0, len(docs), 500):
                d[coll].insert_many(docs[i:i + 500])
            print(f"restored {coll}: {len(docs)} documents", flush=True)
    init_db()


if __name__ == "__main__":
    apply_env_overrides()
    ap = argparse.ArgumentParser(description="NetraAttend MongoDB backup / restore")
    ap.add_argument("--photos", action="store_true", help="include attendance photos")
    ap.add_argument("--restore", metavar="FILE", help="backup zip to restore")
    ap.add_argument("--yes", action="store_true", help="confirm restore (replaces data)")
    args = ap.parse_args()
    if args.restore:
        if not args.yes:
            sys.exit("Restore REPLACES the current data in MongoDB. Run again with --yes to confirm.")
        restore(args.restore)
    else:
        make_backup(args.photos)
