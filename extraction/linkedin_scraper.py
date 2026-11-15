"""Download the Experience and Education sections of the LinkedIn profiles in data/interim/profile_urls.csv.

Usage:
    uv run extraction/linkedin_scraper.py --dry-run          # only read the CSV, no LinkedIn requests
    uv run extraction/linkedin_scraper.py --limit 1          # pilot: check that every endpoint answers
    uv run extraction/linkedin_scraper.py --limit 40         # one daily batch (resumes previous runs)
    uv run extraction/linkedin_scraper.py --skip jane-doe    # never scrape this profile (e.g. it does not exist)
    uv run extraction/linkedin_scraper.py --no-education     # positions only (one request less per profile)

The script only downloads. Each LinkedIn response is saved untouched in data/raw/linkedin/<public_id>/
(profile.json, positions.json, educations.json); parse_profiles.py turns them into tables, so parsing
can change without scraping again. Progress per profile is kept in data/interim/scrape_status.json.

Requests are authenticated with the cookies of a browser login, set in .env (see README.md).
Every run that talks to LinkedIn also writes a detailed log file to logs/.
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from dotenv import load_dotenv

from common import LINKEDIN_RAW_DIR, LOG_DIR, PROFILE_URLS_CSV, ROOT_DIR, SCRAPE_STATUS_JSON, extract_public_id, profile_key

# Stop the run after this many profiles in a row fail without an HTTP error (e.g. network errors).
# Any HTTP status other than 200 stops the run right away.
MAX_CONSECUTIVE_FAILURES = 5
# Status codes LinkedIn answers with when it throttles or blocks an account.
RATE_LIMIT_STATUS_CODES = {429, 999}
VOYAGER_BASE_URL = "https://www.linkedin.com/voyager/api"
# Random pause before every request, on top of the pause between profiles.
REQUEST_DELAY = (2, 5)
# LinkedIn sometimes redirects back to the same URL to hand out its routing cookie (lidc).
MAX_REDIRECTS = 3
# Redirect targets that mean LinkedIn wants a human to log in or pass a security check.
CHALLENGE_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "/login")
PAGE_SIZE = 100

# Dash endpoints the LinkedIn web app calls for each profile section.
POSITIONS_PATH = "/identity/dash/profilePositions"
# Assumed by analogy with profilePositions and not verified yet: check it with --limit 1 first and,
# if LinkedIn answers anything but HTTP 200, copy the request the Education section makes from
# DevTools -> Network on a profile page.
EDUCATIONS_PATH = "/identity/dash/profileEducations"

logger = logging.getLogger("linkedin_scraper")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, default=PROFILE_URLS_CSV, help="CSV with a url column (collect_urls.py output)")
    parser.add_argument("--status", type=Path, default=SCRAPE_STATUS_JSON, help="JSON file that tracks progress per profile")
    parser.add_argument("--limit", type=int, help="scrape at most this many profiles in this run")
    parser.add_argument("--min-delay", type=float, default=5, help="minimum seconds to wait between profiles")
    parser.add_argument("--max-delay", type=float, default=12, help="maximum seconds to wait between profiles")
    parser.add_argument("--no-education", action="store_true", help="skip the Education request")
    parser.add_argument("--dry-run", action="store_true", help="list the profiles to scrape without logging in")
    parser.add_argument(
        "--skip",
        action="append",
        metavar="PROFILE",
        help="mark a profile (public ID or URL) as skipped so no run scrapes it, then exit; can be repeated",
    )
    parser.add_argument("--verbose", action="store_true", help="also show debug messages (e.g. every HTTP request)")
    return parser.parse_args()


def setup_logging(verbose: bool, log_file: Path | None) -> None:
    """Log INFO (DEBUG with --verbose) to the console and everything to log_file."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(file_handler)

    # urllib3 logs every connection at DEBUG; log_response() already covers each request.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def set_cookie_names(response: requests.Response) -> list[str]:
    """Names of the cookies a response sets, with deleted ones marked. Values are never logged."""
    names = []
    for header in response.raw.headers.getlist("set-cookie"):
        lowered = header.lower()
        deleted = "max-age=0" in lowered or "expires=thu, 01-jan-1970" in lowered
        name = header.split("=", 1)[0].strip()
        names.append(f"{name} (deleted)" if deleted else name)
    return names


def log_response(response: requests.Response, **kwargs) -> None:
    """requests hook: log every LinkedIn API call, the cookies involved and any throttling."""
    request = response.request
    logger.debug(
        "%s %s -> HTTP %d (%.2fs)",
        request.method,
        urlparse(request.url).path,
        response.status_code,
        response.elapsed.total_seconds(),
    )
    sent = [part.split("=", 1)[0].strip() for part in request.headers.get("Cookie", "").split(";") if part.strip()]
    logger.debug("  Cookies sent: %s", ", ".join(sent) or "none")
    received = set_cookie_names(response)
    if received:
        logger.debug("  Cookies set: %s", ", ".join(received))
    if response.is_redirect:
        logger.debug("  Redirect to %s", response.headers.get("location"))

    duplicates = sorted({name for name in sent if sent.count(name) > 1})
    if duplicates:
        logger.warning("Sent duplicate cookies: %s", ", ".join(duplicates))
    if response.status_code in RATE_LIMIT_STATUS_CODES:
        logger.warning("LinkedIn answered with HTTP %d, the account is likely being rate limited", response.status_code)


def format_duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def load_people(csv_path: Path) -> list[dict]:
    """Read the profile list and return one entry per unique LinkedIn profile, in file order."""
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    people = {}
    invalid_urls = duplicates = 0
    for row in rows:
        url = (row.get("url") or "").strip()
        public_id = extract_public_id(url)
        if not public_id:
            logger.warning("Skipping %r: not a LinkedIn profile URL", url)
            invalid_urls += 1
            continue
        key = profile_key(public_id)
        if key in people:
            duplicates += 1
            continue
        people[key] = {"public_id": public_id, "url": url}

    logger.info(
        "Read %d rows from %s: %d unique profiles (%d without a profile URL, %d duplicates)",
        len(rows),
        csv_path.name,
        len(people),
        invalid_urls,
        duplicates,
    )
    return list(people.values())


class LinkedInRequestError(Exception):
    """A problem that only affects the current profile, e.g. a profile that does not exist."""


class StopScrapingError(LinkedInRequestError):
    """LinkedIn answered with a status other than HTTP 200, so the run stops to protect the account."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SessionEndedError(StopScrapingError):
    """LinkedIn logged the session out or wants a security check, so fresh cookies are needed."""


class VoyagerClient:
    """Minimal client for LinkedIn's internal Voyager API, using the cookies of a browser login.

    It sends what the LinkedIn web app sends: the li_at session cookie, JSESSIONID with a
    matching csrf-token header, and liap=true. Without liap, or with two copies of the same
    cookie, LinkedIn logs the session out after a few requests.
    """

    def __init__(self, li_at: str, jsessionid: str, user_agent: str):
        token = jsessionid.strip().strip('"')
        self.auth_cookies = {"li_at": li_at.strip(), "JSESSIONID": f'"{token}"'}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "user-agent": user_agent,
                "accept": "application/vnd.linkedin.normalized+json+2.1",
                "accept-language": "en-US,en;q=0.9",
                "csrf-token": token,
                "x-restli-protocol-version": "2.0.0",
            }
        )
        for name, value in self.auth_cookies.items():
            self.session.cookies.set(name, value, domain=".www.linkedin.com", path="/")
        self.session.cookies.set("liap", "true", domain=".linkedin.com", path="/")
        self.session.hooks["response"].append(log_response)

    def get_json(self, path: str, params: dict | None = None, referer: str | None = None) -> dict:
        """GET a Voyager endpoint and return the normalized JSON body."""
        time.sleep(random.uniform(*REQUEST_DELAY))
        for _ in range(MAX_REDIRECTS + 1):
            self._drop_duplicate_cookies()
            # requests percent-encodes the params (including URNs), which is the form LinkedIn expects.
            response = self.session.get(
                VOYAGER_BASE_URL + path,
                params=params,
                headers={"referer": referer} if referer else None,
                allow_redirects=False,
                timeout=30,
            )
            if not response.is_redirect:
                break
            location = response.headers.get("location", "")
            if any(marker in location for marker in CHALLENGE_MARKERS):
                raise SessionEndedError(f"LinkedIn redirected to a security check ({location})", response.status_code)
            if "li_at (deleted)" in set_cookie_names(response):
                raise SessionEndedError(f"LinkedIn logged the session out on {path}", response.status_code)
            # Any other redirect hands out a cookie, so retry with it.
        else:
            raise StopScrapingError(
                f"HTTP {response.status_code} from {path}, still redirected after {MAX_REDIRECTS} retries",
                response.status_code,
            )

        if response.status_code == 401:
            raise SessionEndedError(f"HTTP 401 from {path}", 401)
        if response.status_code != 200:
            raise StopScrapingError(f"HTTP {response.status_code} from {path}", response.status_code)
        return response.json()

    def _drop_duplicate_cookies(self) -> None:
        """Keep one cookie per name. LinkedIn treats two different li_at or JSESSIONID values as a logout."""
        jar = self.session.cookies
        by_name = defaultdict(list)
        for cookie in jar:
            by_name[cookie.name].append(cookie)

        for name, cookies in by_name.items():
            if len(cookies) < 2:
                continue
            # The browser login cookies win; for cookies LinkedIn set itself, the most specific domain wins.
            keep = next((c for c in cookies if c.value == self.auth_cookies.get(name)), None)
            keep = keep or max(cookies, key=lambda c: len(c.domain or ""))
            for cookie in cookies:
                if cookie is not keep:
                    jar.clear(cookie.domain, cookie.path, cookie.name)
                    logger.debug("Dropped duplicate cookie %s for domain %s", name, cookie.domain)


def save_raw_payload(public_id: str, name: str, payload: dict) -> None:
    """Keep the untouched response so parse_profiles.py can be changed without scraping the profile again."""
    folder = LINKEDIN_RAW_DIR / public_id
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{name}.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def count_entities(payload: dict, type_name: str) -> int:
    """Number of included entities of a type, e.g. "Position" or "Education"."""
    return sum(
        1
        for entity in payload.get("included", [])
        if isinstance(entity, dict) and str(entity.get("$type", "")).endswith("." + type_name)
    )


def fetch_profile(client: VoyagerClient, public_id: str, with_education: bool) -> dict[str, int]:
    """Download and save the profile's sections. Returns the number of entries found per section.

    These are the Dash endpoints the LinkedIn web app uses. The older
    /identity/profiles/{id}/profileView endpoint (linkedin-api's get_profile()) returns HTTP 410.
    """
    referer = f"https://www.linkedin.com/in/{quote(public_id)}/"
    profile = client.get_json("/identity/dash/profiles", {"q": "memberIdentity", "memberIdentity": public_id}, referer)
    urns = (profile.get("data") or {}).get("*elements") or []
    if not urns:
        raise LinkedInRequestError("profile not found")
    logger.debug("Profile URN for %s: %s", public_id, urns[0])
    save_raw_payload(public_id, "profile", profile)

    params = {"q": "viewee", "profileUrn": urns[0], "start": 0, "count": PAGE_SIZE}
    positions = client.get_json(POSITIONS_PATH, params, referer)
    save_raw_payload(public_id, "positions", positions)
    counts = {"positions": count_entities(positions, "Position")}

    if with_education:
        educations = client.get_json(EDUCATIONS_PATH, params, referer)
        save_raw_payload(public_id, "educations", educations)
        counts["educations"] = count_entities(educations, "Education")
    return counts


def load_status(status_path: Path) -> dict[str, dict]:
    if not status_path.exists():
        return {}
    with status_path.open(encoding="utf-8") as f:
        return {profile_key(entry["public_id"]): entry for entry in json.load(f)}


def save_status(status_path: Path, people: list[dict], results: dict[str, dict]) -> None:
    """Write the status in CSV order. Writes to a temp file first so an interrupted run never corrupts it."""
    ordered = [results[key] for key in (profile_key(p["public_id"]) for p in people) if key in results]
    status_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = status_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
    tmp_path.replace(status_path)


def login() -> VoyagerClient | None:
    load_dotenv(ROOT_DIR / ".env")
    settings = {name: os.getenv(name, "").strip() for name in ("LINKEDIN_LI_AT", "LINKEDIN_JSESSIONID", "LINKEDIN_USER_AGENT")}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        logger.error("Set %s in .env (README.md explains how to copy them from the browser)", ", ".join(missing))
        return None

    client = VoyagerClient(settings["LINKEDIN_LI_AT"], settings["LINKEDIN_JSESSIONID"], settings["LINKEDIN_USER_AGENT"])
    logger.info("Checking the LinkedIn session from .env")
    try:
        me = client.get_json("/me")
    except LinkedInRequestError as exc:
        logger.error(
            "The LinkedIn session does not work (%s). Log in to LinkedIn in the browser, "
            "complete any security check, and copy fresh cookies into .env.",
            exc,
        )
        return None

    profile = next((entity for entity in me.get("included", []) if entity.get("firstName")), {})
    logger.info("Logged in as %s %s", profile.get("firstName", "?"), profile.get("lastName", ""))
    return client


def scrape(client: VoyagerClient, people: list[dict], pending: list[dict], results: dict[str, dict], args) -> tuple[list[dict], int]:
    """Scrape the pending profiles, saving after each one. Returns this run's entries and the exit code."""
    scraped = []
    consecutive_failures = 0
    started = time.monotonic()

    def record(entry: dict) -> None:
        results[profile_key(entry["public_id"])] = entry
        save_status(args.status, people, results)
        scraped.append(entry)

    try:
        for index, person in enumerate(pending, start=1):
            eta = ""
            if index > 1:
                remaining = (time.monotonic() - started) / (index - 1) * (len(pending) - index + 1)
                eta = f", ETA {format_duration(remaining)}"
            logger.info("[%d/%d%s] %s", index, len(pending), eta, person["public_id"])

            entry = {"public_id": person["public_id"], "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            profile_started = time.monotonic()
            try:
                entry.update(fetch_profile(client, person["public_id"], not args.no_education))
                consecutive_failures = 0
                if entry["positions"]:
                    logger.info(
                        "  Found %d positions, %s educations in %.1fs",
                        entry["positions"],
                        entry.get("educations", "-"),
                        time.monotonic() - profile_started,
                    )
                else:
                    logger.warning("  No positions found (the profile may be private or have no experience)")
            except SessionEndedError as exc:
                # Not the profile's fault, so it is not recorded and the next run scrapes it as usual.
                logger.error(
                    "Stopping: LinkedIn ended the session (%s). Progress is saved. Log in again in the "
                    "browser, copy fresh cookies into .env, then rerun.",
                    exc,
                )
                return scraped, 1
            except StopScrapingError as exc:
                # Recorded as failed, so the next run retries this profile after the ones not tried yet.
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["http_status"] = exc.status
                record(entry)
                logger.error(
                    "Stopping: LinkedIn answered %s instead of HTTP 200. Progress is saved; wait before rerunning.",
                    exc,
                )
                return scraped, 1
            except Exception as exc:
                consecutive_failures += 1
                entry["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("  Failed: %s", entry["error"])
                logger.debug("Traceback for %s", person["public_id"], exc_info=True)

            record(entry)

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "%d failures in a row, stopping to protect the account. "
                    "Wait a while, then run the script again to continue.",
                    consecutive_failures,
                )
                return scraped, 1
            if index < len(pending):
                delay = random.uniform(args.min_delay, args.max_delay)
                logger.debug("Waiting %.1fs before the next profile", delay)
                time.sleep(delay)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user. Run the script again to continue.")
        return scraped, 130
    finally:
        logger.info("Run took %s", format_duration(time.monotonic() - started))

    return scraped, 0


def log_summary(scraped: list[dict], results: dict[str, dict], status_path: Path) -> None:
    failed = [entry for entry in scraped if entry.get("error")]
    without_positions = [entry for entry in scraped if not entry.get("error") and not entry.get("positions")]
    logger.info(
        "This run: %d profiles processed, %d with positions, %d without positions, %d failed",
        len(scraped),
        len(scraped) - len(failed) - len(without_positions),
        len(without_positions),
        len(failed),
    )
    for entry in failed:
        logger.warning("Failed: %s: %s", entry["public_id"], entry["error"])

    total_failed = sum(1 for entry in results.values() if entry.get("error"))
    total_skipped = sum(1 for entry in results.values() if entry.get("skipped"))
    logger.info(
        "Status file %s now has %d profiles (%d failed and retried on the next run, %d skipped)",
        status_path,
        len(results),
        total_failed,
        total_skipped,
    )


def skip_profiles(values: list[str], people: list[dict], results: dict[str, dict], status_path: Path) -> int:
    """Mark profiles as skipped, so they count as done and no run scrapes them again."""
    by_key = {profile_key(p["public_id"]): p for p in people}
    missing = False
    for value in values:
        public_id = extract_public_id(value) or value.strip()
        person = by_key.get(profile_key(public_id))
        if not person:
            logger.error("No profile %r in the CSV", value)
            missing = True
            continue
        results[profile_key(person["public_id"])] = {"public_id": person["public_id"], "skipped": True}
        logger.info("Marked %s as skipped", person["public_id"])

    save_status(status_path, people, results)
    return 1 if missing else 0


def select_pending(people: list[dict], results: dict[str, dict]) -> tuple[list[dict], int]:
    """Return the profiles to scrape and how many are already done.

    Scraped and skipped profiles count as done. Profiles not tried yet come first and failed ones
    are retried last, so a profile that keeps failing (and stopping the run) cannot block the rest.
    """
    not_tried = [p for p in people if profile_key(p["public_id"]) not in results]
    failed = [p for p in people if results.get(profile_key(p["public_id"]), {}).get("error")]
    return not_tried + failed, len(people) - len(not_tried) - len(failed)


def main() -> int:
    args = parse_args()
    log_file = None if args.dry_run or args.skip else LOG_DIR / f"scraper_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
    setup_logging(args.verbose, log_file)
    logger.debug("Arguments: %s", vars(args))

    people = load_people(args.csv)
    results = load_status(args.status)
    if results:
        logger.info("Loaded the status of %d profiles from previous runs (%s)", len(results), args.status)

    if args.skip:
        return skip_profiles(args.skip, people, results, args.status)

    pending, already_done = select_pending(people, results)
    if args.limit is not None:
        pending = pending[: args.limit]
    logger.info("%d profiles already scraped or skipped, %d to scrape in this run", already_done, len(pending))

    if args.dry_run:
        for person in pending:
            print(person["public_id"])
        return 0
    if not pending:
        logger.info("Nothing to scrape")
        return 0

    logger.info("Writing a detailed log to %s", log_file)
    logger.info(
        "Waiting %g-%gs between profiles, plus %d-%ds before each request",
        args.min_delay,
        args.max_delay,
        *REQUEST_DELAY,
    )

    client = login()
    if not client:
        return 1

    scraped, exit_code = scrape(client, people, pending, results, args)
    log_summary(scraped, results, args.status)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
