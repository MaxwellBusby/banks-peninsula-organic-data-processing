from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError, sync_playwright


def _extract_region_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return tail or "region"


def _find_show_more(page):
    candidates = [
        page.locator("button:has-text('Show more')"),
        page.get_by_role("button", name=re.compile(r"show\s*more", re.IGNORECASE)),
        page.locator("button.styles_button__KagQX:has-text('Show more')"),
        page.locator("li.TopResults_topResultListItem__XhYgV button:has-text('Show more')"),
    ]

    for locator in candidates:
        try:
            first = locator.first
            if first.is_visible(timeout=700):
                return first
        except Exception:
            continue
    return None


def _collect_trail_rows(page) -> list[dict[str, str]]:
    cards = page.locator("li.TopResults_topResultListItem__XhYgV")
    try:
        card_count = cards.count()
    except Exception:
        card_count = 0

    if card_count == 0:
        raise RuntimeError("No trail cards found on the region page.")

    rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for idx in range(card_count):
        card = cards.nth(idx)

        url_value = ""
        for selector in [
            "a.TopResults_resultName__ar1k7",
            "a.TopResults_resultStatsLink__9jayb",
            "a[href*='/trail/']",
        ]:
            try:
                href = (card.locator(selector).first.get_attribute("href") or "").strip()
            except Exception:
                continue
            if href:
                url_value = urljoin("https://www.alltrails.com", href)
                break

        name_value = ""
        for selector in [
            "a.TopResults_resultName__ar1k7",
            "h3.TopResults_trailCardHeading__hlxWV",
            "div[role='group'][aria-label='trail']",
        ]:
            try:
                text = (card.locator(selector).first.inner_text(timeout=1000) or "").strip()
            except Exception:
                continue
            if text:
                name_value = re.sub(r"^#\d+\s*-\s*", "", text).strip()
                break

        if not url_value:
            raise RuntimeError(f"Trail card {idx + 1} is missing a trail URL.")
        if not name_value:
            raise RuntimeError(f"Trail card {idx + 1} is missing a trail name.")

        if url_value in seen_urls:
            continue
        seen_urls.add(url_value)

        rows.append({"track_name": name_value, "track_url": url_value})

    if not rows:
        raise RuntimeError("No trail rows could be extracted from region page cards.")

    return rows


def _click_show_more_until_exhausted(page) -> list[dict[str, str]]:
    stagnant_rounds = 0
    click_round = 0
    trail_rows = _collect_trail_rows(page)

    while True:
        button = _find_show_more(page)
        if button is None:
            break

        try:
            button_text = button.inner_text(timeout=1000).strip()
        except Exception:
            button_text = ""
        if button_text:
            print(f"Clicking show more: {button_text}")

        try:
            button.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        before_count = len(trail_rows)
        clicked = False
        for force in (False, True):
            try:
                button.click(timeout=6000, force=force)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            break

        click_round += 1

        try:
            page.wait_for_function(
                """prevCount => document.querySelectorAll("li.TopResults_topResultListItem__XhYgV").length > prevCount""",
                arg=before_count,
                timeout=12000,
            )
        except TimeoutError:
            page.wait_for_timeout(1500)

        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(500)

        new_rows = _collect_trail_rows(page)
        if len(new_rows) <= before_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        trail_rows = new_rows
        print(f"Show more click {click_round}: {len(trail_rows)} trail rows found")

        if stagnant_rounds >= 3:
            break

    return trail_rows


def _write_trail_urls_csv(target: Path, rows: list[dict[str, str]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["track_name", "track_url"])
        writer.writeheader()
        writer.writerows(rows)


def _build_output_target(out_dir: Path, region_slug: str) -> Path:
    base_name = f"{region_slug}_track_urls.csv"
    target = out_dir / base_name

    counter = 2
    while target.exists():
        target = out_dir / f"{Path(base_name).stem}_{counter}{Path(base_name).suffix}"
        counter += 1
    return target


def export_region_trails(
    url: str,
    out_dir: Path,
    profile_dir: Path,
    storage_state_file: Path | None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    region_slug = _extract_region_slug(url)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            slow_mo=100,
        )

        page = context.pages[0] if context.pages else context.new_page()

        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page.goto(url, wait_until="domcontentloaded")
        print("If login or slider challenge appears, complete it manually in this browser.")
        print("Waiting for the trail list to become available...")

        ready = False
        for i in range(180):
            current = page.url
            if i % 10 == 0:
                print(f"Still waiting... current URL: {current}")

            try:
                page.locator("li.TopResults_topResultListItem__XhYgV").first.wait_for(
                    state="visible", timeout=1000
                )
                ready = True
                break
            except Exception:
                pass

            try:
                show_more = _find_show_more(page)
                if show_more is not None:
                    ready = True
                    break
            except Exception:
                pass

            time.sleep(1)

        if not ready:
            context.close()
            raise RuntimeError(
                "Login/challenge did not reach a usable region page in time. "
                "Complete login in the opened browser and rerun."
            )

        if storage_state_file:
            storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_file))
            print(f"Saved session state: {storage_state_file.resolve()}")

        trail_rows = _click_show_more_until_exhausted(page)

        target = _build_output_target(out_dir, region_slug)
        _write_trail_urls_csv(target, trail_rows)
        print(f"Saved {len(trail_rows)} trail URLs to {target.resolve()}")

        context.close()
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Export trail names and URLs from an AllTrails region page")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--storage-state", default="")
    args = parser.parse_args()

    storage_state_file = Path(args.storage_state) if args.storage_state else None
    saved = export_region_trails(
        url=args.url,
        out_dir=Path(args.out_dir),
        profile_dir=Path(args.profile_dir),
        storage_state_file=storage_state_file,
    )
    print(f"Saved: {saved.resolve()}")


if __name__ == "__main__":
    main()
