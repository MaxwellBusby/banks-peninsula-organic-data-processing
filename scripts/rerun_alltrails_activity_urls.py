import csv
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

root = Path(__file__).resolve().parents[1]
track_csv = root / "Data" / "AllTrails" / "Downloads" / "banks-peninsula_track_urls.csv"
out_dir = root / "Data" / "AllTrails" / "Downloads"
profile_dir = root / "Data" / "AllTrails" / "alltrails_profile"
storage_state = root / "Data" / "AllTrails" / "storage_state.json"
index_csv = out_dir / "activity_index.csv"
id_salt_env = "ALLTRAILS_ID_SALT"


def slug_from_url(url: str) -> str:
    path = urlparse((url or "").strip()).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return (tail or "alltrails-track").strip().lower()


with track_csv.open("r", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = list(reader)
    fieldnames = list(reader.fieldnames or [])

if "Extracted" not in fieldnames:
    fieldnames.append("Extracted")

for i, row in enumerate(rows):
    extracted = str(row.get("Extracted", "")).strip().lower() in {"true", "1", "yes"}
    if extracted:
        continue

    trail_url = (row.get("track_url") or "").strip()
    track_name = (row.get("track_name") or "").strip()
    if not trail_url:
        print(f"[{i + 1}/{len(rows)}] Missing track_url; skipping")
        continue

    slug = slug_from_url(trail_url)
    geometry_path = out_dir / f"{slug}.gpx"
    if not geometry_path.exists():
        print(
            f"[{i + 1}/{len(rows)}] Skipping {track_name}: geometry missing ({geometry_path.name})"
        )
        continue

    print(f"[{i + 1}/{len(rows)}] Exporting activity URLs for {track_name}")
    cmd = [
        sys.executable,
        "alltrails_activity_urls_export.py",
        "--url",
        trail_url,
        "--out-dir",
        str(out_dir),
        "--profile-dir",
        str(profile_dir),
        "--storage-state",
        str(storage_state),
        "--index-csv",
        str(index_csv),
        "--id-salt-env",
        id_salt_env,
    ]

    result = subprocess.run(
        cmd,
        cwd=str(root),
        text=True,
    )

    if result.returncode != 0:
        raise SystemExit(result.returncode)

    row["Extracted"] = "True"
    with track_csv.open("w", newline="", encoding="utf-8") as wf:
        writer = csv.DictWriter(wf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

print("Full activity URL rerun completed.")
