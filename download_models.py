"""Download the face models into models/ (official InsightFace release, checksum-verified).

  python download_models.py           det_500m + w600k_mbf  (default, light - for an i3 PC)
  python download_models.py --large   also det_10g + w600k_r50 (more accurate, needs a faster PC)

License: InsightFace pretrained models are free for non-commercial research use only.
Commercial use needs a license from InsightFace (https://insightface.ai).
"""
import argparse
import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile

RELEASE = "https://github.com/deepinsight/insightface/releases/download/v0.7/"
PACKS = {
    "buffalo_s.zip": {
        "det_500m.onnx": "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a",
        "w600k_mbf.onnx": "9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
    },
    "buffalo_l.zip": {
        "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
        "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
    },
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(pack, wanted, dest):
    missing = {n: s for n, s in wanted.items()
               if not (os.path.exists(os.path.join(dest, n)) and sha256(os.path.join(dest, n)) == s)}
    if not missing:
        print(f"{pack}: already present ({', '.join(wanted)})")
        return
    print(f"{pack}: downloading for {', '.join(missing)} ...", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, pack)
        with urllib.request.urlopen(RELEASE + pack, timeout=60) as resp, open(zpath, "wb") as out:
            shutil.copyfileobj(resp, out)
        with zipfile.ZipFile(zpath) as z:
            for member in z.namelist():
                name = os.path.basename(member)
                if name not in missing:
                    continue
                target = os.path.join(dest, name)
                with z.open(member) as src, open(target + ".part", "wb") as out:
                    shutil.copyfileobj(src, out)
                if sha256(target + ".part") != missing[name]:
                    os.remove(target + ".part")
                    raise SystemExit(f"Checksum mismatch for {name} - download again.")
                os.replace(target + ".part", target)
                print(f"  saved {name}")
    left = [n for n in missing if not os.path.exists(os.path.join(dest, n))]
    if left:
        raise SystemExit(f"{pack} did not contain {', '.join(left)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--large", action="store_true", help="also the accurate models (det_10g, w600k_r50)")
    ap.add_argument("--dest", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
    args = ap.parse_args()
    os.makedirs(args.dest, exist_ok=True)
    fetch("buffalo_s.zip", PACKS["buffalo_s.zip"], args.dest)
    if args.large:
        fetch("buffalo_l.zip", PACKS["buffalo_l.zip"], args.dest)
