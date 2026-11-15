"""Turn the raw LinkedIn responses into tables: profiles, positions and educations.

Usage:
    uv run extraction/parse_profiles.py

Reads data/raw/linkedin/<public_id>/*.json for every profile marked as scraped in
data/interim/scrape_status.json and writes data/interim/{profiles,positions,educations}.csv.

People are identified by person_id, a salted hash of the public profile ID. The salt is created on the
first run in data/interim/pseudonym_salt.txt and never leaves the machine, so the IDs in the published
dataset cannot be linked back to a profile by hashing guessed profile IDs.
"""

import csv
import hashlib
import json
import logging
import re
import secrets
import sys
from collections import Counter
from pathlib import Path

from common import INTERIM_DIR, LINKEDIN_RAW_DIR, PROFILE_URLS_CSV, SCRAPE_STATUS_JSON, profile_key

SALT_FILE = INTERIM_DIR / "pseudonym_salt.txt"
PROFILE_COLUMNS = ["person_id", "public_id", "source", "query", "scraped_at", "location", "n_positions", "n_educations"]
POSITION_COLUMNS = [
    "person_id", "position_order", "title", "company_name", "company_li_id", "location", "employment_type",
    "start_year", "start_month", "end_year", "end_month",
]  # fmt: skip
EDUCATION_COLUMNS = [
    "person_id", "school_name", "school_li_id", "degree", "field_of_study",
    "start_year", "start_month", "end_year", "end_month",
]  # fmt: skip

logger = logging.getLogger("parse_profiles")


def pseudonymizer() -> callable:
    SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SALT_FILE.exists():
        SALT_FILE.write_text(secrets.token_hex(16), encoding="utf-8")
    salt = SALT_FILE.read_text(encoding="utf-8").strip()
    return lambda public_id: hashlib.sha256(f"{salt}:{profile_key(public_id)}".encode()).hexdigest()[:12]


def entity_type(entity: dict) -> str:
    """"com.linkedin.voyager.dash.identity.profile.Position" -> "Position"."""
    return str(entity.get("$type", "")).rsplit(".", 1)[-1]


def text_of(value) -> str | None:
    """Return the text of a plain string or of LinkedIn's wrappers ({"text": ...}, locale maps)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "localized", "en_US", "name", "defaultLocalizedName"):
            if key in value:
                return text_of(value[key])
    return None


def li_id(urn: str | None) -> str | None:
    """"urn:li:fsd_company:1035" -> "1035". The same company has the same number in every URN namespace."""
    if not isinstance(urn, str):
        return None
    match = re.search(r":(\d+)\)?$", urn)
    return match.group(1) if match else None


def date_parts(date: dict | None) -> tuple[int | None, int | None]:
    if not isinstance(date, dict) or "year" not in date:
        return None, None
    return date["year"], date.get("month")


def ordered_entities(payload: dict, type_name: str) -> tuple[list[dict], dict[str, dict]]:
    """Entities of one type in the order LinkedIn lists them, plus every included entity by URN."""
    included = [entity for entity in payload.get("included", []) if isinstance(entity, dict)]
    by_urn = {entity["entityUrn"]: entity for entity in included if "entityUrn" in entity}

    data = payload.get("data") or {}
    ordered = [by_urn[urn] for urn in data.get("*elements") or [] if urn in by_urn]
    found = [entity for entity in ordered if entity_type(entity) == type_name]
    if not found:
        found = [entity for entity in included if entity_type(entity) == type_name]

    total = (data.get("paging") or {}).get("total")
    if isinstance(total, int) and total > len(found):
        logger.warning("LinkedIn reports %d %s entries but only %d were returned", total, type_name, len(found))
    return found, by_urn


def linked(entity: dict, by_urn: dict[str, dict], *keys: str) -> tuple[str | None, dict]:
    """Follow the first reference field present ("*company", "companyUrn", ...) to its URN and entity."""
    for key in keys:
        urn = entity.get(key)
        if isinstance(urn, str):
            return urn, by_urn.get(urn, {})
    return None, {}


def parse_positions(payload: dict, person_id: str) -> list[dict]:
    positions, by_urn = ordered_entities(payload, "Position")
    rows = []
    for order, position in enumerate(positions):
        company_urn, company = linked(position, by_urn, "*company", "companyUrn")
        type_urn, type_entity = linked(position, by_urn, "*employmentType", "employmentTypeUrn")
        employment_type = text_of(type_entity.get("name")) or text_of(position.get("employmentType"))
        if not employment_type and type_urn:
            employment_type = type_urn.rsplit(":", 1)[-1]

        date_range = position.get("dateRange") or position.get("timePeriod") or {}
        start_year, start_month = date_parts(date_range.get("start") or date_range.get("startDate"))
        end_year, end_month = date_parts(date_range.get("end") or date_range.get("endDate"))
        rows.append(
            {
                "person_id": person_id,
                "position_order": order,
                "title": text_of(position.get("title")),
                "company_name": text_of(position.get("companyName")) or text_of(company.get("name")),
                "company_li_id": li_id(company_urn),
                "location": text_of(position.get("locationName")) or text_of(position.get("geoLocationName")),
                "employment_type": employment_type,
                "start_year": start_year,
                "start_month": start_month,
                "end_year": end_year,
                "end_month": end_month,
            }
        )
    return rows


def parse_educations(payload: dict, person_id: str) -> list[dict]:
    educations, by_urn = ordered_entities(payload, "Education")
    rows = []
    for education in educations:
        school_urn, school = linked(education, by_urn, "*school", "schoolUrn")
        date_range = education.get("dateRange") or education.get("timePeriod") or {}
        start_year, start_month = date_parts(date_range.get("start") or date_range.get("startDate"))
        end_year, end_month = date_parts(date_range.get("end") or date_range.get("endDate"))
        rows.append(
            {
                "person_id": person_id,
                "school_name": text_of(education.get("schoolName")) or text_of(school.get("name")),
                "school_li_id": li_id(school_urn),
                "degree": text_of(education.get("degreeName")),
                "field_of_study": text_of(education.get("fieldOfStudy")),
                "start_year": start_year,
                "start_month": start_month,
                "end_year": end_year,
                "end_month": end_month,
            }
        )
    return rows


def profile_location(payload: dict) -> str | None:
    """Current location shown on the profile (only used for descriptives, never as a feature)."""
    included = [entity for entity in payload.get("included", []) if isinstance(entity, dict)]
    by_urn = {entity["entityUrn"]: entity for entity in included if "entityUrn" in entity}
    for entity in included:
        if entity_type(entity) != "Profile":
            continue
        name = text_of(entity.get("locationName")) or text_of(entity.get("geoLocationName"))
        geo = entity.get("geoLocation") or {}
        geo_entity = by_urn.get(geo.get("*geo") or geo.get("geoUrn"), {})
        # The memberIdentity response usually has no Geo entity, only the country code.
        country = (entity.get("location") or {}).get("countryCode")
        return name or text_of(geo_entity.get("defaultLocalizedName")) or (country.upper() if country else None)
    return None


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    if not SCRAPE_STATUS_JSON.exists():
        logger.error("No %s yet: run linkedin_scraper.py first", SCRAPE_STATUS_JSON)
        return 1

    with SCRAPE_STATUS_JSON.open(encoding="utf-8") as f:
        status = [entry for entry in json.load(f) if not entry.get("error") and not entry.get("skipped")]
    with PROFILE_URLS_CSV.open(newline="", encoding="utf-8") as f:
        origin = {profile_key(row["public_id"]): row for row in csv.DictReader(f)}

    to_person_id = pseudonymizer()
    profiles, positions, educations = [], [], []
    for entry in status:
        public_id = entry["public_id"]
        folder = LINKEDIN_RAW_DIR / public_id
        person_id = to_person_id(public_id)

        positions_payload = read_json(folder / "positions.json")
        if positions_payload is None:
            logger.warning("%s is marked as scraped but has no positions.json, skipping", public_id)
            continue
        person_positions = parse_positions(positions_payload, person_id)
        educations_payload = read_json(folder / "educations.json")
        person_educations = parse_educations(educations_payload, person_id) if educations_payload else []

        source = origin.get(profile_key(public_id), {})
        profiles.append(
            {
                "person_id": person_id,
                "public_id": public_id,
                "source": source.get("source"),
                "query": source.get("query"),
                "scraped_at": entry["scraped_at"],
                "location": profile_location(read_json(folder / "profile.json") or {}),
                "n_positions": len(person_positions),
                # Empty (not 0) when the Education section was not downloaded, so missing is not read as "none".
                "n_educations": len(person_educations) if educations_payload else None,
            }
        )
        positions.extend(person_positions)
        educations.extend(person_educations)

    write_csv(INTERIM_DIR / "profiles.csv", PROFILE_COLUMNS, profiles)
    write_csv(INTERIM_DIR / "positions.csv", POSITION_COLUMNS, positions)
    write_csv(INTERIM_DIR / "educations.csv", EDUCATION_COLUMNS, educations)

    year_only = sum(1 for p in positions if p["start_year"] and not p["start_month"])
    logger.info(
        "Parsed %d profiles: %d positions (%d with a start year but no month), %d educations",
        len(profiles),
        len(positions),
        year_only,
        len(educations),
    )
    top = Counter((p["company_name"], p["company_li_id"]) for p in positions).most_common(25)
    logger.info("Most frequent companies (name, LinkedIn ID): %s", top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
