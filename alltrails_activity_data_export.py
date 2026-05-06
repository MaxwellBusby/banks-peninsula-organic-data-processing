from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError, sync_playwright


def _extract_activity_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    match = re.search(r"-([a-f0-9-]{6,})$", tail, flags=re.IGNORECASE)
    if match:
        hex_id = re.sub(r"[^a-f0-9]", "", match.group(1).lower())
        if len(hex_id) >= 6:
            return hex_id
    return ""


def _build_download_target(out_dir: Path, activity_id: str, file_format: str) -> Path:
    base_name = f"{activity_id}.{file_format}"
    target = out_dir / base_name

    counter = 2
    while target.exists():
        target = out_dir / f"{activity_id}_{counter}.{file_format}"
        counter += 1
    return target


def _read_index_rows(index_csv: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not index_csv.exists() or index_csv.stat().st_size == 0:
        raise RuntimeError(f"Index CSV not found or empty: {index_csv}")

    with index_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if "activity_url" not in fieldnames:
        raise RuntimeError("Index CSV must contain an 'activity_url' column.")
    if "activity_id" not in fieldnames:
        fieldnames.append("activity_id")

    for required in [
        "file_name",
        "download_status",
        "download_error",
        "downloaded_at",
        "download_attempts",
    ]:
        if required not in fieldnames:
            fieldnames.append(required)

    for row in rows:
        for field in fieldnames:
            row.setdefault(field, "")

    return rows, fieldnames


def _write_index_rows(index_csv: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _download_activity(
    page,
    url: str,
    out_dir: Path,
    file_format: str,
    menu_timeout_ms: int,
    download_timeout_ms: int,
) -> Path:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    activity_id = _extract_activity_id(page.url or url)

    overflow = page.locator("[data-testid='overflow']").first
    overflow.wait_for(state="visible", timeout=menu_timeout_ms)
    overflow.click(timeout=menu_timeout_ms)

    page.get_by_role("menuitem", name="Export route file").click(timeout=menu_timeout_ms)

    format_select = page.locator("[data-testid='download-type-select']")
    format_select.wait_for(state="visible", timeout=menu_timeout_ms)
    format_select.select_option(file_format)

    with page.expect_download(timeout=download_timeout_ms) as download_info:
        page.locator("[data-testid='OK']").click(timeout=menu_timeout_ms)

    download = download_info.value
    target = _build_download_target(out_dir, activity_id, file_format)
    download.save_as(str(target))
    return target


def _has_recording_controls(page, timeout_ms: int) -> bool:
    try:
        page.locator("[data-testid='overflow']").first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _is_download_limited_page(page) -> bool:
    try:
        # AllTrails limit page shows "An error has occurred" in an <h1> element.
        heading = page.locator("h1", has_text="An error has occurred").first
        return heading.is_visible(timeout=500)
    except Exception:
        return False


def _wait_for_manual_login_if_needed(page, warmup_url: str, wait_seconds: int = 180) -> None:
    page.goto(warmup_url, wait_until="domcontentloaded", timeout=30000)
    if _has_recording_controls(page, timeout_ms=2000):
        return

    print("AllTrails controls are not visible yet.")
    print("If login or slider challenge appears, complete it in the opened browser window.")
    print(f"Waiting up to {wait_seconds} seconds for recording controls...")

    for i in range(wait_seconds):
        if _has_recording_controls(page, timeout_ms=1000):
            print("Recording controls detected. Continuing downloads.")
            return
        if i % 10 == 0:
            print(f"Still waiting for login/challenge completion... ({i}s)")
        time.sleep(1)

    raise RuntimeError(
        "Timed out waiting for AllTrails recording controls. "
        "Complete login/challenge and rerun."
    )


def export_routes_from_index(
    index_csv: Path,
    out_dir: Path,
    file_format: str,
    profile_dir: Path,
    storage_state_file: Path | None,
    max_items: int,
    start_row: int,
    retries: int,
    menu_timeout_ms: int,
    download_timeout_ms: int,
    slow_mo_ms: int,
) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    rows, fieldnames = _read_index_rows(index_csv)
    total_rows = len(rows)
    if total_rows == 0:
        raise RuntimeError("Index CSV has no rows to process.")

    start_idx = max(0, start_row - 1)
    if start_idx >= total_rows:
        raise RuntimeError(f"start-row {start_row} is beyond total rows ({total_rows}).")

    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0

    pending_indexes: list[int] = []
    for idx in range(start_idx, total_rows):
        if max_items > 0 and len(pending_indexes) >= max_items:
            break
        row = rows[idx]
        activity_url = (row.get("activity_url") or "").strip()
        existing_file = (row.get("file_name") or "").strip()
        if not activity_url:
            continue
        if existing_file and (out_dir / existing_file).exists():
            continue
        pending_indexes.append(idx)

    if not pending_indexes:
        print("No pending activity rows to download in the selected range.")
        return {
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            slow_mo=slow_mo_ms,
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        warmup_url = (rows[pending_indexes[0]].get("activity_url") or "").strip()
        _wait_for_manual_login_if_needed(page, warmup_url=warmup_url, wait_seconds=180)

        for idx in range(start_idx, total_rows):
            if max_items > 0 and processed >= max_items:
                break

            row = rows[idx]
            processed += 1
            row_num = idx + 1
            activity_url = (row.get("activity_url") or "").strip()

            if not activity_url:
                row["download_status"] = "failed"
                row["download_error"] = "missing activity_url"
                failed += 1
                continue

            existing_file = (row.get("file_name") or "").strip()
            if existing_file and (out_dir / existing_file).exists():
                row["download_status"] = "already_downloaded"
                row["download_error"] = ""
                skipped += 1
                continue

            # Skip if already downloaded
            if (row.get("download_status") or "").strip().lower() == "downloaded":
                skipped += 1
                continue

            # Skip if already downloaded
            if (row.get("download_status") or "").strip().lower() == "already_downloaded":
                skipped += 1
                continue

            # Skip if activity date is outside 2025
            activity_date = (row.get("activity_date") or "").strip()
            if activity_date:
                try:
                    date_obj = datetime.fromisoformat(activity_date.split("T")[0])
                    if date_obj.year != 2025:
                        row["download_status"] = "skipped"
                        row["download_error"] = "activity date outside 2025"
                        skipped += 1
                        continue
                except (ValueError, IndexError):
                    pass

            activity_id = _extract_activity_id(activity_url)
            if activity_id:
                row["activity_id"] = activity_id

            attempt = 0
            success = False
            last_error = ""

            while attempt <= retries and not success:
                attempt += 1
                row["download_attempts"] = str(attempt)
                print(f"[{row_num}/{total_rows}] Downloading activity (attempt {attempt}): {activity_url}")
                try:
                    saved_path = _download_activity(
                        page=page,
                        url=activity_url,
                        out_dir=out_dir,
                        file_format=file_format,
                        menu_timeout_ms=menu_timeout_ms,
                        download_timeout_ms=download_timeout_ms,
                    )

                    row["file_name"] = saved_path.name
                    row["download_status"] = "downloaded"
                    row["download_error"] = ""
                    row["downloaded_at"] = datetime.now(timezone.utc).isoformat()
                    succeeded += 1
                    success = True
                    print(f"Saved: {saved_path.resolve()}")
                except TimeoutError as ex:
                    if _is_download_limited_page(page):
                        cooldown_seconds = 3600  # 1 hour cooldown for download limit
                        print(
                            "Detected AllTrails download limit page (An error has occurred). "
                            f"Cooling down for {cooldown_seconds} seconds before retrying same URL."
                        )
                        page.wait_for_timeout(cooldown_seconds * 1000)
                        # Retry the same URL without spending a retry attempt.
                        attempt -= 1
                        continue

                    if attempt == 1:
                        try:
                            _wait_for_manual_login_if_needed(page, warmup_url=activity_url, wait_seconds=60)
                        except Exception:
                            pass
                    last_error = f"timeout: {str(ex).splitlines()[0][:180]}"
                except Exception as ex:
                    last_error = f"error: {str(ex).splitlines()[0][:180]}"

                if not success and attempt <= retries:
                    page.wait_for_timeout(400)

            if not success:
                row["download_status"] = "failed"
                row["download_error"] = last_error or "unknown error"
                row["downloaded_at"] = ""
                failed += 1
                print(f"Failed: {activity_url} -> {row['download_error']}")

            if processed % 25 == 0:
                _write_index_rows(index_csv, fieldnames, rows)
                if storage_state_file:
                    storage_state_file.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(storage_state_file))

        _write_index_rows(index_csv, fieldnames, rows)
        if storage_state_file:
            storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_file))
            print(f"Saved session state: {storage_state_file.resolve()}")

        context.close()

    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch export route files from AllTrails activity URLs in an index CSV")
    parser.add_argument("--index-csv", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--format", default="csv", choices=["csv", "gpx", "kml"])
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--max-items", type=int, default=0, help="0 means process all rows")
    parser.add_argument("--start-row", type=int, default=1, help="1-based start row in index CSV")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--menu-timeout-ms", type=int, default=5000)
    parser.add_argument("--download-timeout-ms", type=int, default=16000)
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    storage_state_file = Path(args.storage_state) if args.storage_state else None
    index_csv = Path(args.index_csv)

    summary = export_routes_from_index(
        index_csv=index_csv,
        out_dir=out_dir,
        file_format=args.format,
        profile_dir=Path(args.profile_dir),
        storage_state_file=storage_state_file,
        max_items=max(0, args.max_items),
        start_row=max(1, args.start_row),
        retries=max(0, args.retries),
        menu_timeout_ms=max(1000, args.menu_timeout_ms),
        download_timeout_ms=max(3000, args.download_timeout_ms),
        slow_mo_ms=max(0, args.slow_mo_ms),
    )
    print(
        "Done. "
        f"Processed={summary['processed']} "
        f"Downloaded={summary['succeeded']} "
        f"Failed={summary['failed']} "
        f"Skipped={summary['skipped']}"
    )
    print(f"Updated index: {index_csv.resolve()}")


if __name__ == "__main__":
    main()
