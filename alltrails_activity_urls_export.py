from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError, sync_playwright


INDEX_FIELDNAMES = [
    "track_name",
    "activity_url",
    "activity_id",
    "activity_date",
    "activity_type",
    "user_anon_id",
    "file_name",
]


def _extract_trail_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    return tail or "alltrails"


def _extract_activity_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    match = re.search(r"-([a-f0-9-]{6,})$", tail, flags=re.IGNORECASE)
    if match:
        hex_id = re.sub(r"[^a-f0-9]", "", match.group(1).lower())
        if len(hex_id) >= 6:
            return hex_id
    return ""


def _normalize_iso_date(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    try:
        if cleaned.endswith("Z"):
            dt = datetime.fromisoformat(cleaned[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(cleaned)
        return dt.date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(cleaned, "%B %d, %Y").date().isoformat()
        except ValueError:
            pass
        match = re.search(r"(\d{4}-\d{2}-\d{2})", cleaned)
        return match.group(1) if match else ""


def _extract_date_from_card_text(text: str) -> str:
    candidates = [segment.strip() for segment in re.split(r"[\r\n]+", text or "") if segment.strip()]
    for candidate in candidates:
        normalized = _normalize_iso_date(candidate)
        if normalized:
            return normalized

    match = re.search(
        r"\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b",
        text or "",
    )
    if match:
        return _normalize_iso_date(match.group(1))

    return ""


def _make_anon_id(user_key: str, salt: str) -> str:
    cleaned = user_key.strip().lower()
    if not cleaned:
        return ""
    payload = f"{salt}|{cleaned}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _clean_card_text(value: str) -> str:
    return (value or "").strip()


def _extract_user_key_from_card(card) -> str:
    selectors = [
        "a[href^='/members/']",
        "div.EntityLabel_labelRow__gK39v:not(.EntityLabel_subtitle__XSpP9) span",
        "div.EntityLabel_labelRow__gK39v:not(.EntityLabel_subtitle__XSpP9)",
    ]
    for selector in selectors:
        try:
            text = _clean_card_text(card.locator(selector).first.inner_text(timeout=1000))
        except Exception:
            continue
        if text:
            return text
    return ""


def _extract_activity_date_from_card(card) -> str:
    selectors = [
        "div.EntityLabel_subtitle__XSpP9 > span div",
        "div.TrackCard_userSubheader__aIfz7 > div:first-child",
        "div.EntityLabel_subtitle__XSpP9",
    ]
    for selector in selectors:
        try:
            text = _clean_card_text(card.locator(selector).first.inner_text(timeout=1000))
        except Exception:
            continue
        normalized = _normalize_iso_date(text)
        if normalized:
            return normalized

    try:
        subtitle_text = _clean_card_text(card.locator("div.EntityLabel_subtitle__XSpP9").first.inner_text(timeout=1000))
    except Exception:
        subtitle_text = ""
    if subtitle_text:
        match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b", subtitle_text)
        if match:
            return _normalize_iso_date(match.group(1))

    return ""


def _extract_activity_type_from_card(card) -> str:
    selectors = [
        "div.TrackCard_activityLabel___kkuy",
        "div.EntityLabel_subtitle__XSpP9 .TrackCard_activityLabel___kkuy",
    ]
    for selector in selectors:
        try:
            text = _clean_card_text(card.locator(selector).first.inner_text(timeout=1000))
        except Exception:
            continue
        if text:
            return text
    return ""


def _extract_activity_url_from_card(card) -> str:
    selectors = [
        "a.TrackCard_activityCardContainer__zPN49",
        "a[href*='/explore/recording/']",
    ]
    for selector in selectors:
        try:
            href = (card.locator(selector).first.get_attribute("href") or "").strip()
        except Exception:
            continue
        if href:
            return urljoin("https://www.alltrails.com", href)
    return ""


def _extract_expected_activity_count(page) -> int:
    selectors = [
        "div[class*='ActivitiesList_heading']",
        "[data-testid='activities-list-heading']",
    ]
    for selector in selectors:
        try:
            text = page.locator(selector).first.inner_text(timeout=3000).strip()
        except Exception:
            continue

        match = re.search(r"([\d,]+)\s+activities?", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
    return 0


def _extract_last_activity_year(page) -> int | None:
    cards = page.locator("[data-testid='track-card']")
    try:
        card_count = cards.count()
    except Exception:
        return None

    if card_count == 0:
        return None

    # The last visible card should be the oldest currently loaded activity.
    last_card = cards.nth(card_count - 1)
    date_str = _extract_activity_date_from_card(last_card)
    if not date_str:
        return None

    try:
        return datetime.strptime(date_str, "%Y-%m-%d").year
    except ValueError:
        return None


def _collect_activity_urls(page) -> list[str]:
    try:
        hrefs = page.locator("a[href*='/explore/recording/']").evaluate_all(
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
    except Exception:
        hrefs = []

    urls = []
    seen = set()
    for href in hrefs:
        absolute = urljoin("https://www.alltrails.com", href)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _find_show_more(page):
    candidates = [
        page.locator("#trail-reviews div.Reviews_ugcContainer__Tv_Fa > div > button"),
        page.locator("#trail-reviews div.Reviews_ugcContainer__Tv_Fa button:has-text('Show more')"),
        page.locator("div.Reviews_ugcContainer__Tv_Fa > div > button"),
        page.locator("div.Reviews_ugcContainer__Tv_Fa button:has-text('Show more')"),
        page.get_by_role("button", name=re.compile(r"show\s*more", re.IGNORECASE)),
        page.locator("button:has-text('Show more')"),
        page.locator("a:has-text('Show more')"),
        page.locator("[class*='styles_button']:has-text('Show more')"),
    ]

    for locator in candidates:
        try:
            first = locator.first
            if first.is_visible(timeout=700):
                return first
        except Exception:
            continue
    return None


def _find_activities_tab(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"\bactivities\b", re.IGNORECASE)),
        page.locator("#trail-reviews button:nth-of-type(2)"),
        page.locator("button:has-text('activities')"),
        page.locator("[aria-label*='activities' i]"),
    ]

    for locator in candidates:
        try:
            first = locator.first
            if first.is_visible(timeout=700):
                return first
        except Exception:
            continue
    return None


def _click_activities_tab(page) -> bool:
    button = _find_activities_tab(page)
    if button is None:
        return False

    try:
        button.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    for force in (False, True):
        try:
            button.click(timeout=8000, force=force)
            return True
        except Exception:
            continue

    return False


def _click_show_more_until_exhausted(page, expected_count: int, stop_before_year: int | None = 2022) -> list[str]:
    stagnant_rounds = 0
    activity_urls = _collect_activity_urls(page)
    click_round = 0

    while True:
        if expected_count and len(activity_urls) >= expected_count:
            break

        button = _find_show_more(page)
        if button is None:
            break

        try:
            label = button.inner_text(timeout=1000).strip()
        except Exception:
            label = ""
        if label:
            print(f"Clicking show more: {label}")

        try:
            button.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        before_count = len(activity_urls)
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
                """prevCount => Array.from(document.querySelectorAll("a[href*='/explore/recording/']")).length > prevCount""",
                arg=before_count,
                timeout=12000,
            )
        except TimeoutError:
            # Some pages update slowly without immediately increasing anchor count.
            page.wait_for_timeout(1500)

        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(500)

        new_activity_urls = _collect_activity_urls(page)
        if len(new_activity_urls) <= before_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        activity_urls = new_activity_urls
        if expected_count:
            print(f"Show more click {click_round}: {len(activity_urls)}/{expected_count} URLs found")
        else:
            print(f"Show more click {click_round}: {len(activity_urls)} URLs found")

        if stop_before_year is not None:
            last_activity_year = _extract_last_activity_year(page)
            if last_activity_year is not None:
                print(f"Last loaded activity year after click {click_round}: {last_activity_year}")
                if last_activity_year < stop_before_year:
                    print(
                        f"Stopping Show more: last loaded activity is older than {stop_before_year}."
                    )
                    break
            else:
                print("Could not read the last activity date after this click; continuing.")

        if stagnant_rounds >= 3:
            break

    return activity_urls


def _upsert_index_row(index_csv: Path, row: dict[str, str], key_field: str = "activity_id") -> None:
    rows: list[dict[str, str]] = []
    if index_csv.exists() and index_csv.stat().st_size > 0:
        with index_csv.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    key_value = (row.get(key_field) or "").strip()
    replaced = False
    for existing in rows:
        existing_value = (existing.get(key_field) or "").strip()
        if key_value and existing_value == key_value:
            for fieldname in INDEX_FIELDNAMES:
                new_value = (row.get(fieldname) or "").strip()
                if new_value:
                    existing[fieldname] = new_value
                else:
                    existing.setdefault(fieldname, "")
            replaced = True
            break

    if not replaced:
        normalized_row = {fieldname: (row.get(fieldname) or "").strip() for fieldname in INDEX_FIELDNAMES}
        rows.append(normalized_row)

    index_csv.parent.mkdir(parents=True, exist_ok=True)
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _collect_activity_rows_from_cards(page, trail_slug: str, id_salt_env: str) -> list[dict[str, str]]:
    cards = page.locator("[data-testid='track-card']")
    try:
        card_count = cards.count()
    except Exception:
        card_count = 0

    if card_count == 0:
        raise RuntimeError("No activity cards were found on the activities list page.")

    salt_value = os.getenv(id_salt_env, "")
    warned_unsalted = False
    rows: list[dict[str, str]] = []
    skipped_cards: list[str] = []
    seen_ids: set[str] = set()

    for idx in range(card_count):
        card = cards.nth(idx)
        activity_url = _extract_activity_url_from_card(card)
        activity_id = _extract_activity_id(activity_url)
        activity_type = _extract_activity_type_from_card(card)
        activity_date = _extract_activity_date_from_card(card)
        user_key = _extract_user_key_from_card(card)

        if not activity_url:
            skipped_cards.append(f"Card {idx}: missing activity URL")
            continue
        if not activity_id:
            skipped_cards.append(f"Card {idx}: could not parse activity_id from URL '{activity_url}'")
            continue
        if not activity_date:
            skipped_cards.append(f"Card {idx}: missing/invalid date")
            continue
        if not activity_type:
            skipped_cards.append(f"Card {idx}: missing activity type")
            continue
        if not user_key:
            skipped_cards.append(f"Card {idx}: missing user key")
            continue

        if activity_id in seen_ids:
            continue
        seen_ids.add(activity_id)

        user_anon_id = _make_anon_id(user_key, salt_value)
        if user_key and not salt_value and not warned_unsalted:
            print(
                f"Warning: env var {id_salt_env} is not set; using unsalted stable hash for user_anon_id."
            )
            warned_unsalted = True

        rows.append(
            {
                "track_name": trail_slug,
                "activity_url": activity_url,
                "activity_id": activity_id,
                "activity_date": activity_date,
                "activity_type": activity_type,
                "user_anon_id": user_anon_id,
                "file_name": "",
            }
        )

    if skipped_cards:
        print(
            "Warning: skipped cards with incomplete metadata; continuing with remaining cards."
        )
        for detail in skipped_cards[:10]:
            print(detail)
        if len(skipped_cards) > 10:
            print(f"... and {len(skipped_cards) - 10} more skipped cards")

    return rows


def _write_activity_rows(target: Path, rows: list[dict[str, str]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    for row in rows:
        _upsert_index_row(target, row, key_field="activity_id")


def export_activity_urls(
    url: str,
    out_dir: Path,
    profile_dir: Path,
    storage_state_file: Path | None,
    index_csv: Path,
    id_salt_env: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    trail_slug = _extract_trail_slug(url)

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
            slow_mo=100,
        )

        page = context.pages[0] if context.pages else context.new_page()

        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page.goto(url, wait_until="domcontentloaded")
        print("If login or slider challenge appears, complete it manually in this browser.")
        print("Waiting for the activities list to become available...")

        ready = False
        for i in range(180):
            current = page.url
            if i % 10 == 0:
                print(f"Still waiting... current URL: {current}")

            try:
                heading = page.locator("div[class*='ActivitiesList_heading']").first
                heading.wait_for(state="visible", timeout=1000)
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
                "Login/challenge did not reach a usable trail page in time. "
                "Complete login in the opened browser and rerun."
            )

        if storage_state_file:
            storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_file))
            print(f"Saved session state: {storage_state_file.resolve()}")

        print("Selecting the Activities tab...")
        tab_clicked = _click_activities_tab(page)
        if not tab_clicked:
            print("Could not find the Activities tab with the known selectors; continuing anyway.")
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            try:
                page.locator("div.Reviews_ugcContainer__Tv_Fa > div > button").first.wait_for(
                    state="visible",
                    timeout=10000,
                )
            except Exception:
                pass

        expected_count = _extract_expected_activity_count(page)

        print("Starting Show more exhaustion...")
        _click_show_more_until_exhausted(page, expected_count)

        target = index_csv
        activity_rows = _collect_activity_rows_from_cards(page, trail_slug, id_salt_env)
        _write_activity_rows(target, activity_rows)

        print(
            f"Saved {len(activity_rows)} activity rows to {target.resolve()}"
            + (f" (header said {expected_count})." if expected_count else ".")
        )

        if expected_count and len(activity_rows) != expected_count:
            print(
                "Warning: the extracted count did not match the activities count."
            )

        context.close()
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Export activity URLs from an AllTrails trail page")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--index-csv", default="")
    parser.add_argument("--id-salt-env", default="ALLTRAILS_ID_SALT")
    args = parser.parse_args()

    storage_state_file = Path(args.storage_state) if args.storage_state else None
    index_csv = Path(args.index_csv) if args.index_csv else Path(args.out_dir) / "activity_index.csv"
    saved = export_activity_urls(
        url=args.url,
        out_dir=Path(args.out_dir),
        profile_dir=Path(args.profile_dir),
        storage_state_file=storage_state_file,
        index_csv=index_csv,
        id_salt_env=args.id_salt_env,
    )
    print(f"Saved: {saved.resolve()}")


if __name__ == "__main__":
    main()
