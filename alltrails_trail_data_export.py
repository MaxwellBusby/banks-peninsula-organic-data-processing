from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError, sync_playwright


REQUIRED_COLUMNS = [
    "track_name",
    "track_url",
]

STATUS_COLUMNS = [
    "file_name",
    "download_status",
    "download_error",
    "downloaded_at",
    "download_attempts",
]


def _extract_track_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return (tail or "alltrails-track").strip().lower()


def _build_download_target(out_dir: Path, track_url: str, suggested_filename: str) -> Path:
    slug = _extract_track_slug(track_url)
    ext = Path((suggested_filename or "").strip()).suffix.lower()
    if ext != ".gpx":
        ext = ".gpx"

    target = out_dir / f"{slug}{ext}"
    counter = 2
    while target.exists():
        target = out_dir / f"{slug}_{counter}{ext}"
        counter += 1
    return target


def _find_existing_gpx_for_track(out_dir: Path, track_url: str) -> Path | None:
    slug = _extract_track_slug(track_url)
    exact = out_dir / f"{slug}.gpx"
    if exact.exists():
        return exact

    # Reuse previously downloaded variants like slug_2.gpx, slug_3.gpx, etc.
    for candidate in sorted(out_dir.glob(f"{slug}_*.gpx")):
        if candidate.exists():
            return candidate
    return None


def _read_index_rows(index_csv: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not index_csv.exists() or index_csv.stat().st_size == 0:
        raise RuntimeError(f"Index CSV not found or empty: {index_csv}")

    with index_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    for required in REQUIRED_COLUMNS:
        if required not in fieldnames:
            raise RuntimeError(f"Index CSV must contain '{required}' column.")

    for col in STATUS_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)

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


def _find_overflow_button(page):
    candidates = [
        page.locator("button:has([data-testid='overflow'])"),
        page.locator("[data-testid='overflow']").locator("xpath=ancestor::button[1]"),
        page.locator("svg[data-testid='overflow']"),
        page.locator("[data-testid='overflow']"),
        page.locator("button[class*='overflow']"),
        page.get_by_role("button", name=re.compile(r"more|options", re.IGNORECASE)),
        page.locator("button[aria-haspopup='menu']"),
    ]

    for locator in candidates:
        try:
            btn = locator.first
            if btn.is_visible(timeout=1200):
                return btn
        except Exception:
            continue

    raise RuntimeError("Overflow menu button not found on trail page")


def _is_export_map_option_visible(page, timeout_ms: int = 1000) -> bool:
    candidates = [
        page.get_by_role("button", name=re.compile(r"^export map file$", re.IGNORECASE)),
        page.get_by_role("menuitem", name=re.compile(r"export map file", re.IGNORECASE)),
        page.locator("button:has-text('Export map file')"),
        page.locator("[role='menu'] button:has-text('Export map file')"),
    ]

    for locator in candidates:
        try:
            if locator.first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            continue
    return False


def _open_overflow_menu(page, menu_timeout_ms: int) -> None:
    # Ensure top navigation controls are within viewport.
    page.evaluate("window.scrollTo(0, 0)")

    candidates = [
        page.locator("button:has([data-testid='overflow'])").first,
        page.locator("[data-testid='overflow']").locator("xpath=ancestor::button[1]").first,
        page.locator("svg[data-testid='overflow']").first,
        page.locator("[data-testid='overflow']").first,
        page.locator("button[class*='overflow']").first,
        page.get_by_role("button", name=re.compile(r"more|options", re.IGNORECASE)).first,
        page.locator("button[aria-haspopup='menu']").first,
    ]

    for target in candidates:
        try:
            if not target.is_visible(timeout=1200):
                continue
            try:
                target.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            # Try regular click then force click for stubborn overlays.
            for force in (False, True):
                try:
                    target.click(timeout=menu_timeout_ms, force=force)
                    if _is_export_map_option_visible(page, timeout_ms=1200):
                        return
                except Exception:
                    continue

            # JavaScript fallback click on actual DOM element.
            for js_selector in [
                "button:has([data-testid='overflow'])",
                "[data-testid='overflow']",
                "svg[data-testid='overflow']",
                "button[class*='overflow']",
            ]:
                try:
                    clicked = page.evaluate(
                        """
                        (sel) => {
                            const el = document.querySelector(sel);
                            if (!el) return false;
                            el.click();
                            return true;
                        }
                        """,
                        js_selector,
                    )
                    if clicked and _is_export_map_option_visible(page, timeout_ms=1200):
                        return
                except Exception:
                    continue
        except Exception:
            continue

    raise RuntimeError("Overflow menu was found but could not be opened")


def _find_export_map_button(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"^export map file$", re.IGNORECASE)),
        page.get_by_role("menuitem", name=re.compile(r"export map file", re.IGNORECASE)),
        page.locator("button:has-text('Export map file')"),
        page.locator("[role='menu'] button:has-text('Export map file')"),
    ]

    for locator in candidates:
        try:
            btn = locator.first
            if btn.is_visible(timeout=1500):
                return btn
        except Exception:
            continue

    raise RuntimeError("'Export map file' option not found in overflow menu")


def _find_modal_export_button(page):
    candidates = [
        page.locator("[role='dialog'] button.styles_primary___7R_x"),
        page.locator("[role='dialog'] button:has-text('Export')"),
        page.get_by_role("button", name=re.compile(r"^export$", re.IGNORECASE)),
    ]

    for locator in candidates:
        try:
            btn = locator.first
            if btn.is_visible(timeout=1200):
                return btn
        except Exception:
            continue

    return None


def _download_track_gpx(page, track_url: str, out_dir: Path, menu_timeout_ms: int, download_timeout_ms: int) -> Path:
    page.goto(track_url, wait_until="domcontentloaded", timeout=45000)

    _open_overflow_menu(page, menu_timeout_ms=menu_timeout_ms)

    export_map_button = _find_export_map_button(page)
    export_map_button.click(timeout=menu_timeout_ms)

    modal_export_button = _find_modal_export_button(page)

    if modal_export_button is not None:
        with page.expect_download(timeout=download_timeout_ms) as download_info:
            modal_export_button.click(timeout=menu_timeout_ms)
        download = download_info.value
    else:
        # Fallback: some layouts trigger download directly from the first export click.
        _open_overflow_menu(page, menu_timeout_ms=menu_timeout_ms)
        export_map_button = _find_export_map_button(page)
        with page.expect_download(timeout=download_timeout_ms) as download_info:
            export_map_button.click(timeout=menu_timeout_ms)
        download = download_info.value

    target = _build_download_target(out_dir, track_url, download.suggested_filename)
    download.save_as(str(target))
    return target


def _has_controls(page, timeout_ms: int) -> bool:
    try:
        _find_overflow_button(page)
        return True
    except Exception:
        try:
            page.locator("main").first.wait_for(state="attached", timeout=timeout_ms)
            return True
        except Exception:
            return False


def _wait_for_manual_login_if_needed(page, warmup_url: str, wait_seconds: int = 180) -> None:
    page.goto(warmup_url, wait_until="domcontentloaded", timeout=45000)
    if _has_controls(page, timeout_ms=1500):
        return

    print("AllTrails controls are not visible yet.")
    print("If login/challenge appears, complete it in the opened browser window.")
    print(f"Waiting up to {wait_seconds} seconds...")

    for i in range(wait_seconds):
        if _has_controls(page, timeout_ms=1000):
            print("Page controls detected. Continuing downloads.")
            return
        if i % 15 == 0:
            print(f"Still waiting for login/challenge completion... ({i}s)")
        time.sleep(1)

    raise RuntimeError("Timed out waiting for AllTrails page controls after login/challenge")


def export_trail_gpx_from_index(
    index_csv: Path,
    out_dir: Path,
    profile_dir: Path,
    storage_state_file: Path | None,
    start_row: int,
    max_items: int,
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
        raise RuntimeError("Index CSV has no rows to process")

    start_idx = max(0, start_row - 1)
    if start_idx >= total_rows:
        raise RuntimeError(f"start-row {start_row} is beyond total rows ({total_rows})")

    pending: list[int] = []
    for idx in range(start_idx, total_rows):
        if max_items > 0 and len(pending) >= max_items:
            break
        row = rows[idx]
        track_url = (row.get("track_url") or "").strip()
        if not track_url:
            continue

        existing_file = (row.get("file_name") or "").strip()
        status = (row.get("download_status") or "").strip().lower()
        if existing_file and (out_dir / existing_file).exists():
            continue
        if _find_existing_gpx_for_track(out_dir, track_url) is not None:
            continue
        if status in {"downloaded", "already_downloaded"}:
            continue

        pending.append(idx)

    if not pending:
        print("No pending trail URLs to download in the selected range.")
        return {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}

    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0

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
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        first_url = (rows[pending[0]].get("track_url") or "").strip()
        _wait_for_manual_login_if_needed(page, warmup_url=first_url, wait_seconds=180)

        for idx in pending:
            row = rows[idx]
            row_num = idx + 1
            processed += 1

            track_url = (row.get("track_url") or "").strip()
            if not track_url:
                row["download_status"] = "failed"
                row["download_error"] = "missing track_url"
                failed += 1
                continue

            existing_file = (row.get("file_name") or "").strip()
            if existing_file and (out_dir / existing_file).exists():
                row["download_status"] = "already_downloaded"
                row["download_error"] = ""
                skipped += 1
                continue

            existing_gpx = _find_existing_gpx_for_track(out_dir, track_url)
            if existing_gpx is not None:
                row["file_name"] = existing_gpx.name
                row["download_status"] = "already_downloaded"
                row["download_error"] = ""
                skipped += 1
                continue

            attempt = 0
            success = False
            last_error = ""

            while attempt <= retries and not success:
                attempt += 1
                row["download_attempts"] = str(attempt)
                print(f"[{row_num}/{total_rows}] Downloading GPX (attempt {attempt}): {track_url}")

                try:
                    saved_path = _download_track_gpx(
                        page=page,
                        track_url=track_url,
                        out_dir=out_dir,
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
                    if attempt == 1:
                        try:
                            _wait_for_manual_login_if_needed(page, warmup_url=track_url, wait_seconds=60)
                        except Exception:
                            pass
                    last_error = f"timeout: {str(ex).splitlines()[0][:180]}"
                except Exception as ex:
                    last_error = f"error: {str(ex).splitlines()[0][:180]}"

                if not success and attempt <= retries:
                    page.wait_for_timeout(500)

            if not success:
                row["download_status"] = "failed"
                row["download_error"] = last_error or "unknown error"
                row["downloaded_at"] = ""
                failed += 1
                print(f"Failed: {track_url} -> {row['download_error']}")

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
    parser = argparse.ArgumentParser(description="Download AllTrails trail GPX files from a track URL CSV")
    parser.add_argument(
        "--index-csv",
        default="Data/AllTrails_Downloads/banks-peninsula_track_urls.csv",
        help="CSV containing track_name and track_url columns",
    )
    parser.add_argument("--out-dir", default="Data/AllTrails_Downloads")
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--max-items", type=int, default=0, help="0 means no limit")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--menu-timeout-ms", type=int, default=8000)
    parser.add_argument("--download-timeout-ms", type=int, default=25000)
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    args = parser.parse_args()

    summary = export_trail_gpx_from_index(
        index_csv=Path(args.index_csv),
        out_dir=Path(args.out_dir),
        profile_dir=Path(args.profile_dir),
        storage_state_file=Path(args.storage_state) if args.storage_state else None,
        start_row=max(1, args.start_row),
        max_items=max(0, args.max_items),
        retries=max(0, args.retries),
        menu_timeout_ms=max(1000, args.menu_timeout_ms),
        download_timeout_ms=max(3000, args.download_timeout_ms),
        slow_mo_ms=max(0, args.slow_mo_ms),
    )

    print(
        "Done. "
        f"Processed={summary['processed']} "
        f"Succeeded={summary['succeeded']} "
        f"Failed={summary['failed']} "
        f"Skipped={summary['skipped']}"
    )


if __name__ == "__main__":
    main()
