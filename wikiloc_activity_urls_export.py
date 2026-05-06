from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import sync_playwright

INDEX_FIELDNAMES = [
    "track_name",
    "activity_url",
    "activity_id",
    "activity_type",
    "user_name",
    "activity_date",
    "file_name",
]

EMPTY_LIKE = {"", "nan", "none", "null", "<na>", "na", "n/a"}
BASE_URL = "https://www.wikiloc.com"


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in EMPTY_LIKE else text


def _sanitize_url(url: str) -> str:
    return (url or "").strip().rstrip(")")


def _set_page(url: str, page_num: int) -> str:
    parsed = urlparse(_sanitize_url(url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page_num)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def _extract_id_from_href(href: str) -> str:
    match = re.search(r"-(\d+)(?:$|[/?#])", href or "")
    return match.group(1) if match else ""


def _pick_card_container_from_link(link: Tag) -> Tag | None:
    """Find the card container that includes all metadata (title, type, author)."""
    parent = link
    
    # First pass: look specifically for trail-card or trail-list containers
    for _ in range(10):
        parent = parent.parent if isinstance(parent, Tag) else None
        if not isinstance(parent, Tag):
            break

        class_tokens = {str(c).strip() for c in parent.get("class", []) if str(c).strip()}
        if {"trail-card", "trail-list"}.intersection(class_tokens):
            return parent
    
    # Second pass: climb higher to find any trail-like container
    parent = link
    for _ in range(14):
        parent = parent.parent if isinstance(parent, Tag) else None
        if not isinstance(parent, Tag):
            break

        class_tokens = {str(c).strip() for c in parent.get("class", []) if str(c).strip()}
        if {"trail", "trail-card", "trail-list"}.intersection(class_tokens):
            return parent
    
    return None


def _extract_card_rows(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    by_id: dict[str, dict[str, str]] = {}
    links = soup.select("a[href*='-trails/']")
    for link in links:
        if link is None:
            continue

        card = _pick_card_container_from_link(link) or link

        href = _clean_text(link.get("href", ""))
        if not href:
            continue

        absolute_href = urljoin(BASE_URL, href)
        activity_id = _extract_id_from_href(absolute_href)
        if not activity_id:
            continue

        title_node = card.select_one("h3")
        type_node = card.select_one(
            "small.trail-card__pictogram-text, small.trail-list__pictogram-text, small[class*='pictogram-text'], small[class*='pictogram'], [class*='pictogram'] small"
        )
        author_node = card.select_one(
            "p.trail-list__author-txt, "
            ".trail-list__author-txt, "
            ".trail-card__footer-right p, "
            "a.trail-card__footer-right p, "
            "[class*='author-txt'] p, "
            "[class*='author-txt'], "
            "[class*='author'] a"
        )

        user_name = _clean_text(author_node.get_text(" ", strip=True) if author_node else "")
        if user_name.lower().startswith("by "):
            user_name = user_name[3:].strip()

        row = {
            "track_name": _clean_text(title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True)),
            "activity_url": f"https://www.wikiloc.com/wikiloc/download.do?id={activity_id}",
            "activity_id": activity_id,
            "activity_type": _clean_text(type_node.get_text(" ", strip=True) if type_node else ""),
            "user_name": user_name,
            "activity_date": "",
            "file_name": "",
        }

        if activity_id in by_id:
            for field in INDEX_FIELDNAMES:
                incoming = _clean_text(row.get(field))
                current = _clean_text(by_id[activity_id].get(field))
                if incoming and not current:
                    by_id[activity_id][field] = incoming
        else:
            by_id[activity_id] = row

    return list(by_id.values())


def _upsert_index_rows(index_csv: Path, rows: list[dict[str, str]]) -> None:
    existing: list[dict[str, str]] = []
    if index_csv.exists() and index_csv.stat().st_size > 0:
        with index_csv.open("r", newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    by_id: dict[str, dict[str, str]] = {}
    for row in existing:
        row_norm = {field: _clean_text(row.get(field)) for field in INDEX_FIELDNAMES}
        if row_norm["activity_id"]:
            by_id[row_norm["activity_id"]] = row_norm

    for row in rows:
        activity_id = _clean_text(row.get("activity_id"))
        if not activity_id:
            continue

        if activity_id in by_id:
            for field in INDEX_FIELDNAMES:
                incoming = _clean_text(row.get(field))
                current = _clean_text(by_id[activity_id].get(field))

                if field in {"activity_date", "file_name"}:
                    if not current and incoming:
                        by_id[activity_id][field] = incoming
                    continue

                if incoming and incoming != current:
                    by_id[activity_id][field] = incoming
        else:
            by_id[activity_id] = {field: _clean_text(row.get(field)) for field in INDEX_FIELDNAMES}

    merged = sorted(by_id.values(), key=lambda r: int(r["activity_id"]))
    index_csv.parent.mkdir(parents=True, exist_ok=True)
    with index_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDNAMES)
        writer.writeheader()
        writer.writerows(merged)


def export_activity_urls(
    url: str,
    out_dir: Path,
    profile_dir: Path,
    storage_state_file: Path | None,
    index_csv: Path,
    start_page: int,
    max_page: int,
    slow_mo_ms: int,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    all_rows_by_id: dict[str, dict[str, str]] = {}

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
            slow_mo=slow_mo_ms,
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        for page_num in range(start_page, max_page + 1):
            target_url = _set_page(url, page_num)
            print(f"Loading page {page_num}: {target_url}")

            page.goto(target_url, wait_until="networkidle", timeout=45000)
            html = page.content()
            page_rows = _extract_card_rows(html)
            added = 0

            for row in page_rows:
                activity_id = row["activity_id"]
                if activity_id in all_rows_by_id:
                    for field in INDEX_FIELDNAMES:
                        incoming = _clean_text(row.get(field))
                        current = _clean_text(all_rows_by_id[activity_id].get(field))
                        if incoming and not current:
                            all_rows_by_id[activity_id][field] = incoming
                    continue

                all_rows_by_id[activity_id] = row
                added += 1

            typed_count = sum(1 for r in page_rows if _clean_text(r.get("activity_type")))
            user_count = sum(1 for r in page_rows if _clean_text(r.get("user_name")))
            print(
                f"Page {page_num}: found {len(page_rows)} cards, added {added} new activities "
                f"(activity_type: {typed_count}, user_name: {user_count})."
            )

        if storage_state_file:
            storage_state_file.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state_file))
            print(f"Saved session state: {storage_state_file.resolve()}")

        context.close()

    all_rows = list(all_rows_by_id.values())
    _upsert_index_rows(index_csv, all_rows)
    print(f"Saved {len(all_rows)} extracted activities to {index_csv.resolve()}")
    return index_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Export WikiLoc activity URLs from map result cards")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--index-csv", default="")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-page", type=int, default=30)
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    index_csv = Path(args.index_csv) if args.index_csv else out_dir / "wikiloc_activity_index.csv"
    storage_state_file = Path(args.storage_state) if args.storage_state else None

    export_activity_urls(
        url=args.url,
        out_dir=out_dir,
        profile_dir=Path(args.profile_dir),
        storage_state_file=storage_state_file,
        index_csv=index_csv,
        start_page=max(1, args.start_page),
        max_page=max(1, args.max_page),
        slow_mo_ms=max(0, args.slow_mo_ms),
    )


if __name__ == "__main__":
    main()
