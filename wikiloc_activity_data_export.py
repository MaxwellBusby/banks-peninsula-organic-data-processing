from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError, sync_playwright


def _extract_activity_id(url: str) -> str:
    parsed = urlparse(url)
    query_id = parse_qs(parsed.query).get("id", [""])[0].strip()
    if query_id:
        return query_id

    match = re.search(r"id=(\d+)", url)
    return match.group(1) if match else "activity"


def _build_download_target(out_dir: Path, activity_id: str, suggested_filename: str) -> Path:
    suggested = (suggested_filename or "").strip()
    extension = Path(suggested).suffix if suggested else ""
    if not extension:
        extension = ".gpx"

    base_name = f"{activity_id}{extension}"
    target = out_dir / base_name
    counter = 2
    while target.exists():
        target = out_dir / f"{activity_id}_{counter}{extension}"
        counter += 1
    return target


def _normalize_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    # Generic ISO-like fallback.
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if iso_match:
        return iso_match.group(1)

    return ""


def _extract_activity_date_from_page(page) -> str:
    candidates = page.evaluate(
        """
        () => {
            const values = [];

            const push = (v) => {
                if (!v) return;
                const t = String(v).trim();
                if (t) values.push(t);
            };

            document.querySelectorAll('time[datetime]').forEach(el => push(el.getAttribute('datetime')));
            document.querySelectorAll("meta[property='article:published_time']").forEach(el => push(el.getAttribute('content')));
            document.querySelectorAll("meta[name='date'], meta[itemprop='datePublished']").forEach(el => push(el.getAttribute('content')));

            const dateLike = document.querySelectorAll("[class*='date'], [id*='date']");
            dateLike.forEach(el => push(el.textContent));

            return values.slice(0, 60);
        }
        """
    )

    for raw in candidates:
        normalized = _normalize_date(str(raw))
        if normalized:
            return normalized

    return ""


def _extract_activity_date_from_file(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix not in {".gpx", ".kml", ".xml"}:
        return ""

    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    for pattern in (r"<time>([^<]+)</time>", r"<when>([^<]+)</when>"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        normalized = _normalize_date(match.group(1))
        if normalized:
            return normalized

    return ""


def _update_index_row(
    index_csv: Path,
    activity_id: str,
    activity_url: str,
    file_name: str,
    activity_date: str,
) -> None:
    if not index_csv.exists() or index_csv.stat().st_size == 0:
        return

    with index_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    for required in ["activity_id", "activity_url", "activity_date", "file_name", "Extracted"]:
        if required not in fieldnames:
            fieldnames.append(required)

    updated = False
    for row in rows:
        row_id = (row.get("activity_id") or "").strip()
        row_url = (row.get("activity_url") or "").strip()
        if (activity_id and row_id == activity_id) or (activity_url and row_url == activity_url):
            row["activity_id"] = activity_id or row_id
            row["activity_url"] = activity_url or row_url
            row["file_name"] = file_name
            if activity_date:
                row["activity_date"] = activity_date
            row["Extracted"] = "True"
            updated = True
            break

    if not updated:
        new_row = {field: "" for field in fieldnames}
        new_row["activity_id"] = activity_id
        new_row["activity_url"] = activity_url
        new_row["activity_date"] = activity_date
        new_row["file_name"] = file_name
        new_row["Extracted"] = "True"
        rows.append(new_row)

    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _open_file_tab(page, timeout_ms: int) -> None:
    tab = page.locator("a[href='#download-file'][data-toggle='tab']").first
    tab.wait_for(state="attached", timeout=timeout_ms)

    try:
        tab.click(timeout=timeout_ms)
    except Exception:
        page.evaluate(
            """
            () => {
                const el = document.querySelector("a[href='#download-file'][data-toggle='tab']");
                if (!el) throw new Error("File tab not found");
                el.click();
            }
            """
        )

    page.locator("#download-file").first.wait_for(state="visible", timeout=timeout_ms)


def _select_original_file_type(page, timeout_ms: int) -> None:
    try:
        page.wait_for_function(
            """
            () => {
                const hasOriginal = !!document.querySelector("input[type='radio'][name='filter'][value='original']");
                const hasDownloadBtn = !!document.querySelector("#btn-download-file");
                return hasOriginal || hasDownloadBtn;
            }
            """,
            timeout=timeout_ms,
        )
    except Exception:
        pass

    radio = page.locator("input[type='radio'][name='filter'][value='original']").first
    if radio.count() == 0:
        print("Original file type radio not found; continuing with default file option.")
        return

    try:
        if radio.is_visible(timeout=500):
            radio.check(timeout=timeout_ms, force=True)
            return
    except Exception:
        pass

    try:
        page.evaluate(
            """
            () => {
                const el = document.querySelector("input[type='radio'][name='filter'][value='original']");
                if (!el) return false;
                el.checked = true;
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
            }
            """
        )
    except Exception:
        print("Could not explicitly select original file type; continuing with default file option.")


def _click_download(page, timeout_ms: int) -> str | None:
    button = page.locator("#btn-download-file").first
    button.wait_for(state="attached", timeout=timeout_ms)

    # Try to get the download URL or form data before clicking
    download_url = page.evaluate(
        """
        () => {
            const btn = document.querySelector("#btn-download-file");
            if (!btn) return null;
            
            // Check if it's a form submission
            const form = btn.closest("form");
            if (form && form.action) {
                return form.action;
            }
            
            // Check for href or onclick handler
            if (btn.href) return btn.href;
            
            return null;
        }
        """
    )
    
    if download_url:
        print(f"Detected download URL: {download_url}")

    try:
        button.click(timeout=timeout_ms)
        return download_url
    except Exception as e:
        print(f"Button click failed: {e}")

    try:
        result = page.evaluate(
            """
            () => {
                const el = document.querySelector("#btn-download-file");
                if (!el) throw new Error("Download button not found");
                const form = el.closest("form");
                if (form && form.action) {
                    return form.action;
                }
                el.click();
                return null;
            }
            """
        )
        if result:
            print(f"Detected download URL from form: {result}")
        return result
    except Exception as e:
        print(f"JavaScript evaluation failed: {e}")
        return None


def export_wikiloc_activity(
    url: str,
    out_dir: Path,
    profile_dir: Path,
    storage_state_file: Path | None,
    index_csv: Path | None,
    menu_timeout_ms: int,
    download_timeout_ms: int,
    slow_mo_ms: int,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

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

        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        _open_file_tab(page, menu_timeout_ms)
        _select_original_file_type(page, menu_timeout_ms)
        page_date = _extract_activity_date_from_page(page)

        print(f"Initiating download for {url}")
        download = None
        try:
            with page.expect_download(timeout=download_timeout_ms) as download_info:
                download_url = _click_download(page, menu_timeout_ms)
            download = download_info.value
            print(f"Download captured successfully")
        except TimeoutError as e:
            print(f"Download timeout: {e}")
            print(f"Page URL after click: {page.url}")
            # If the page redirected to downloadToFile.do, the download might not have been captured
            # Try waiting a bit longer or checking the page state
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            raise

        if not download:
            raise RuntimeError("Download was not captured after clicking the button")
        activity_id = _extract_activity_id(page.url or url)
        target = _build_download_target(out_dir, activity_id, download.suggested_filename)
        download.save_as(str(target))

        file_date = _extract_activity_date_from_file(target)
        activity_date = file_date or page_date

        if index_csv:
            _update_index_row(
                index_csv=index_csv,
                activity_id=activity_id,
                activity_url=url,
                file_name=target.name,
                activity_date=activity_date,
            )

        if activity_date:
            print(f"Detected activity date: {activity_date}")
        else:
            print("Activity date not detected for this download.")

        if storage_state_file:
            storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_file))
            print(f"Saved session state: {storage_state_file.resolve()}")

        context.close()
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Download an individual WikiLoc activity file")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--index-csv", default="")
    parser.add_argument("--menu-timeout-ms", type=int, default=5000)
    parser.add_argument("--download-timeout-ms", type=int, default=20000)
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    args = parser.parse_args()

    storage_state_file = Path(args.storage_state) if args.storage_state else None
    index_csv = Path(args.index_csv) if args.index_csv else None

    try:
        saved = export_wikiloc_activity(
            url=args.url,
            out_dir=Path(args.out_dir),
            profile_dir=Path(args.profile_dir),
            storage_state_file=storage_state_file,
            index_csv=index_csv,
            menu_timeout_ms=max(1000, args.menu_timeout_ms),
            download_timeout_ms=max(3000, args.download_timeout_ms),
            slow_mo_ms=max(0, args.slow_mo_ms),
        )
        print(f"Saved: {saved.resolve()}")
    except TimeoutError as ex:
        raise RuntimeError(f"Timeout while downloading WikiLoc activity: {ex}") from ex


if __name__ == "__main__":
    main()
