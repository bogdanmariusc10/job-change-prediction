"""Paths and small helpers shared by the extraction scripts."""

import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
LINKEDIN_RAW_DIR = RAW_DIR / "linkedin"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
REFERENCE_DIR = DATA_DIR / "reference"
LOG_DIR = ROOT_DIR / "logs"

PROFILE_URLS_CSV = INTERIM_DIR / "profile_urls.csv"
SCRAPE_STATUS_JSON = INTERIM_DIR / "scrape_status.json"


def extract_public_id(url: str) -> str | None:
    """Return the public profile ID that the LinkedIn API expects, or None for non-profile URLs.

    "https://ro.linkedin.com/in/jane-doe-123/?originalSubdomain=ro" -> "jane-doe-123"
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "in":
        return None
    return unicodedata.normalize("NFC", unquote(parts[1]))


def profile_key(public_id: str) -> str:
    """Key that identifies a profile regardless of case and Unicode form."""
    return unicodedata.normalize("NFC", public_id).lower()


def ascii_fold(text: str | None) -> str:
    """Lowercase and strip diacritics: "Bucureşti, România" -> "bucuresti, romania"."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def month_index(year: int, month: int) -> int:
    """Months since year 0, so differences between dates are plain subtractions."""
    return year * 12 + (month - 1)


def parse_month(value: str) -> int:
    """"2023-07" -> month index."""
    year, month = value.split("-")
    return month_index(int(year), int(month))


def format_month(index: int) -> str:
    """Month index -> "2023-07"."""
    year, month0 = divmod(int(index), 12)
    return f"{year:04d}-{month0 + 1:02d}"
