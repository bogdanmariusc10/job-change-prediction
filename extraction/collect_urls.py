"""Collect LinkedIn profile URLs of people who work or worked at the target companies.

Runs "X-ray" Google searches (site:ro.linkedin.com/in "Oracle" "engineer" "Bucharest") through the
Serper API, so no request goes to LinkedIn. New profiles are appended to data/interim/profile_urls.csv
and every query is logged, so a rerun continues with the queries not run yet.

Usage:
    uv run extraction/collect_urls.py --dry-run            # print the query grid, no API calls
    uv run extraction/collect_urls.py --target 800         # search until 800 unique profiles
    uv run extraction/collect_urls.py --add-urls links.txt # add URLs found by hand (source=manual)
"""

import argparse
import csv
import itertools
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from common import INTERIM_DIR, PROFILE_URLS_CSV, REFERENCE_DIR, ROOT_DIR, extract_public_id, profile_key

SEARCH_LOG_CSV = INTERIM_DIR / "search_log.csv"
COMPANIES_CSV = REFERENCE_DIR / "companies.csv"
SERPER_URL = "https://google.serper.dev/search"

# Kept broad so the sample is not only engineers. Each word only has to appear somewhere on the profile.
ROLE_KEYWORDS = ["engineer", "developer", "manager", "analyst", "sales", "recruiter", "consultant", "specialist"]
# Public profile pages are in English or Romanian, so each city is searched under both spellings.
CITIES = [
    '("Bucharest" OR "București")',
    '("Cluj-Napoca" OR "Cluj")',
    '("Iasi" OR "Iași")',
    '("Timisoara" OR "Timișoara")',
    '("Brasov" OR "Brașov")',
]
URL_COLUMNS = ["public_id", "url", "source", "query", "rank", "found_at"]
LOG_COLUMNS = ["query", "page", "results", "new_profiles", "searched_at"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=int, default=800, help="stop once this many unique profiles are collected")
    parser.add_argument("--max-queries", type=int, help="run at most this many API calls in this run")
    parser.add_argument("--pages", type=int, default=1, help="result pages per query (10 results each)")
    parser.add_argument("--dry-run", action="store_true", help="print the queries that would run, without API calls")
    parser.add_argument("--add-urls", type=Path, help="text or CSV file with profile URLs to add as source=manual")
    return parser.parse_args()


def load_search_terms() -> list[str]:
    with COMPANIES_CSV.open(newline="", encoding="utf-8") as f:
        return [row["search_term"] for row in csv.DictReader(f) if row["search_term"].strip()]


def build_queries(search_terms: list[str]) -> list[str]:
    """Every company x role x city combination, ordered so each company gets a query before any gets a second."""
    combos = list(itertools.product(CITIES, ROLE_KEYWORDS))
    queries = []
    for city, role in combos:
        for term in search_terms:
            queries.append(f'site:ro.linkedin.com/in {term} "{role}" {city}')
    return queries


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_rows(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def search(api_key: str, query: str, page: int) -> list[dict]:
    response = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "gl": "ro", "num": 10, "page": page},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("organic", [])


def add_manual_urls(path: Path, known: set[str]) -> int:
    text = path.read_text(encoding="utf-8")
    candidates = [token.strip().strip('",') for token in text.replace(",", "\n").split()]
    rows = []
    for url in candidates:
        public_id = extract_public_id(url)
        if public_id and profile_key(public_id) not in known:
            known.add(profile_key(public_id))
            rows.append({"public_id": public_id, "url": url, "source": "manual", "query": path.name, "rank": "", "found_at": now()})
    append_rows(PROFILE_URLS_CSV, URL_COLUMNS, rows)
    print(f"Added {len(rows)} new profiles from {path} ({len(known)} in total)")
    return 0


def main() -> int:
    args = parse_args()
    known = {profile_key(row["public_id"]) for row in read_csv(PROFILE_URLS_CSV)}
    if args.add_urls:
        return add_manual_urls(args.add_urls, known)

    done = {(row["query"], int(row["page"])) for row in read_csv(SEARCH_LOG_CSV)}
    pending = [
        (query, page)
        for query in build_queries(load_search_terms())
        for page in range(1, args.pages + 1)
        if (query, page) not in done
    ]
    print(f"{len(known)} profiles collected so far, {len(done)} searches done, {len(pending)} left in the grid")

    if args.dry_run:
        for query, page in pending[: args.max_queries]:
            print(f"page {page}: {query}")
        return 0

    load_dotenv(ROOT_DIR / ".env")
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        print("Set SERPER_API_KEY in .env (see .env.example)", file=sys.stderr)
        return 1

    calls = 0
    for query, page in pending:
        if len(known) >= args.target:
            print(f"Reached the target of {args.target} profiles")
            break
        if args.max_queries is not None and calls >= args.max_queries:
            print(f"Stopped after {calls} searches (--max-queries)")
            break

        try:
            results = search(api_key, query, page)
        except requests.RequestException as exc:
            print(f"Stopping: search failed ({exc}). Rerun to continue.", file=sys.stderr)
            return 1
        calls += 1

        new_rows = []
        for result in results:
            public_id = extract_public_id(result.get("link", ""))
            if not public_id or profile_key(public_id) in known:
                continue
            known.add(profile_key(public_id))
            new_rows.append(
                {
                    "public_id": public_id,
                    "url": result["link"],
                    "source": "xray_serper",
                    "query": query,
                    "rank": result.get("position", ""),
                    "found_at": now(),
                }
            )
        append_rows(PROFILE_URLS_CSV, URL_COLUMNS, new_rows)
        append_rows(
            SEARCH_LOG_CSV,
            LOG_COLUMNS,
            [{"query": query, "page": page, "results": len(results), "new_profiles": len(new_rows), "searched_at": now()}],
        )
        print(f"[{calls}] {len(results):2d} results, {len(new_rows):2d} new, {len(known)} total | {query}")
        time.sleep(1)

    return 0


if __name__ == "__main__":
    sys.exit(main())
