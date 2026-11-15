"""Build the modeling dataset: one row per (person, reference date) while the person works at a target company.

Usage:
    uv run extraction/build_dataset.py

Inputs:  data/interim/{profiles,positions,educations}.csv (parse_profiles.py), data/reference/companies.csv,
         data/raw/layoffs/*.csv, data/raw/headcount/headcount.csv, data/raw/eurostat/*.csv, data/raw/ins/som103b.csv
Output:  data/processed/dataset.csv (column meanings in data/README.md)

The one rule everything here follows: a feature at reference date t only uses information available
before t, and the label only uses what happens in the 12 months from t.

Dates are month indexes (year * 12 + month - 1). A reference date t means "the first day of month t":
a position that started in month s and ended in month e covers t when s < t <= e, and the person
leaves within 12 months when e falls in months t .. t+11.
"""

import math
import re
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from common import INTERIM_DIR, PROCESSED_DIR, RAW_DIR, REFERENCE_DIR, ROOT_DIR, ascii_fold, format_month, month_index, parse_month

# Reference dates: every 6 months. Starts in 2021 because layoffs.fyi starts in March 2020, so the
# 12-month layoff window is complete from 2021 on. Ends early enough that the 12-month label window
# is over before the scrape (see REPORTING_BUFFER).
FIRST_REFERENCE = "2021-01"
LAST_REFERENCE = "2025-07"
STEP_MONTHS = 6
HORIZON = 12
# People update LinkedIn late, so a "Present" job only counts as still held this many months before the scrape.
REPORTING_BUFFER = 4
# Positions at the same employer less than this many months apart form one stint (a role change, not a new job).
STINT_GAP = 3
# Month assumed when LinkedIn only gives the year.
UNKNOWN_MONTH = 7
# Weight of the overall attrition rate when smoothing a company's attrition (in "virtual employees").
ATTRITION_PRIOR_WEIGHT = 10

# Months between the end of a period and the first reference date at which its value may be used.
# Eurostat publishes monthly unemployment about a month after the month ends, quarterly job vacancies
# about 75 days after the quarter ends, INS county bulletins about two months after; 10-K filings
# come at most 60 days after the fiscal year ends for large companies.
UNEMPLOYMENT_LAG = 2
COUNTY_UNEMPLOYMENT_LAG = 3
VACANCY_LAG = 4
HEADCOUNT_LAG = 3

COMPANIES_CSV = REFERENCE_DIR / "companies.csv"
OUTPUT_CSV = PROCESSED_DIR / "dataset.csv"

FREELANCE = re.compile(r"\b(freelanc\w*|self[- ]employed|independent contractor|liber profesionist|pfa)\b")
CONTRACTOR = re.compile(r"\b(via|through|prin)\b")
INTERN = re.compile(r"\b(intern|internship|trainee|stagiar|practicant|apprentice|working student|student)\b")
# Manager titles that are individual-contributor roles, not people management.
IC_MANAGER = re.compile(
    r"\b(product|program|programme|project|account|marketing|partner|customer success|key account|sales|community|"
    r"content|brand|campaign|category|release|relationship|channel|territory|portfolio|solutions?|success|social media)"
    r" manager\b"
)
SENIORITY = [  # checked top-down, first match wins; no match means mid level (2)
    (7, re.compile(r"\b(chief|ceo|cto|cfo|coo|cio|ciso|cmo|vp|svp|evp|vice president|president)\b")),
    (6, re.compile(r"\b(director|head|sef)\b")),
    (4, re.compile(r"\b(lead|leader|staff|principal|architect|distinguished|fellow|coordonator|coordinator)\b")),
    (3, re.compile(r"\b(senior|sr|snr)\b")),
    (1, re.compile(r"\b(junior|jr|entry|graduate|associate|asistent|assistant)\b")),
]
JOB_FAMILIES = [  # checked top-down, first match wins
    ("hr", r"\b(hr|human resources|recruit\w*|talent|people partner|people operations|resurse umane|payroll)\b"),
    ("finance_legal", r"\b(financ\w*|accountant|accounting|controller|audit\w*|tax|treasury|legal|lawyer|counsel|jurist|contabil\w*|procurement|buyer|purchasing)\b"),
    ("data", r"\b(data|machine learning|ml|ai|artificial intelligence|analytics|analyst|analist|scientist|bi|business intelligence|statistic\w*)\b"),
    ("product", r"\b(product manager|product owner|product management|program manager|programme manager|project manager|scrum master|agile coach|delivery manager|pm)\b"),
    ("design", r"\b(design\w*|ux|ui|user experience)\b"),
    ("marketing", r"\b(marketing|brand|communications?|content|seo|social media|pr|public relations|growth)\b"),
    ("sales", r"\b(sales|account|business development|bdr|sdr|partner|customer success|presales|pre-sales|vanzari|commercial|key account)\b"),
    ("support_ops", r"\b(support|helpdesk|help desk|service desk|customer service|operations|technician|administrator|admin|logistics|supply chain|facilities|office)\b"),
    ("engineering", r"\b(engineer\w*|developer|software|programmer|programator|inginer|devops|sre|qa|tester|test|architect|firmware|hardware|embedded|security|cloud|network|sysadmin|web|frontend|backend|full ?stack|mobile|ios|android)\b"),
    ("research", r"\b(research\w*|cercetator)\b"),
    ("consulting", r"\b(consultant|consulting|advisor)\b"),
]
JOB_FAMILIES = [(name, re.compile(pattern)) for name, pattern in JOB_FAMILIES]
PHD = re.compile(r"\b(phd|ph\.d|doctor\w*|doctorat\w*|dphil)\b")
MASTER = re.compile(r"\b(master\w*|msc|m\.sc|mba|ma|meng|m\.eng|ms|mres|masterat|postgraduate)\b")
BACHELOR = re.compile(r"\b(bachelor\w*|bsc|b\.sc|ba|beng|b\.eng|bs|licen\w*|engineer\w*|inginer\w*|undergraduate)\b")
UNIVERSITY = re.compile(r"\b(universit\w*|politehnic\w*|polytechnic|academia|academy|faculty|facultatea|institut\w*|ase)\b")
IT_EDUCATION = re.compile(
    r"\b(computer|computing|informatic\w*|calculatoare|software|automatic\w*|automation|electronic\w*|telecom\w*|"
    r"cibernetic\w*|cybernetic\w*|mathemat\w*|matematic\w*|information|data|artificial intelligence|ai|it|ict|"
    r"systems|sisteme|electrical|electric\w*|mecatronic\w*|robotic\w*)\b"
)
# INS county names (as in SOM103B) and the words that point to them in a LinkedIn location.
COUNTIES = {
    "Municipiul Bucuresti": ["bucuresti", "bucharest", "bucarest", "bukarest"],
    "Ilfov": ["ilfov", "otopeni", "voluntari", "pipera", "chiajna", "bragadiru", "popesti"],
    "Cluj": ["cluj"], "Iasi": ["iasi"], "Timis": ["timis", "timisoara"], "Brasov": ["brasov"],
    "Sibiu": ["sibiu"], "Constanta": ["constanta"], "Dolj": ["dolj", "craiova"], "Bihor": ["bihor", "oradea"],
    "Galati": ["galati"], "Prahova": ["prahova", "ploiesti"], "Arges": ["arges", "pitesti"], "Arad": ["arad"],
    "Bacau": ["bacau"], "Suceava": ["suceava"], "Mures": ["mures", "targu mures", "tirgu mures"],
    "Maramures": ["maramures", "baia mare"], "Braila": ["braila"], "Buzau": ["buzau"], "Satu Mare": ["satu mare"],
    "Valcea": ["valcea"], "Mehedinti": ["mehedinti", "drobeta"], "Neamt": ["neamt"],
    "Hunedoara": ["hunedoara", "deva", "petrosani"], "Alba": ["alba iulia", "alba"], "Covasna": ["covasna", "sfantu gheorghe"],
    "Harghita": ["harghita", "miercurea ciuc"], "Salaj": ["salaj", "zalau"], "Bistrita-Nasaud": ["bistrita"],
    "Vrancea": ["vrancea", "focsani"], "Dambovita": ["dambovita", "targoviste"], "Olt": ["olt", "slatina"],
    "Teleorman": ["teleorman", "alexandria"], "Giurgiu": ["giurgiu"], "Calarasi": ["calarasi"],
    "Ialomita": ["ialomita", "slobozia"], "Tulcea": ["tulcea"], "Vaslui": ["vaslui"], "Botosani": ["botosani"],
    "Caras-Severin": ["caras", "resita"], "Gorj": ["gorj", "targu jiu"],
}  # fmt: skip
COUNTY_PATTERNS = [(county, re.compile(r"\b(" + "|".join(words) + r")\b")) for county, words in COUNTIES.items()]
REGION_OF_COUNTY = {"Municipiul Bucuresti": "bucuresti_ilfov", "Ilfov": "bucuresti_ilfov", "Cluj": "cluj", "Iasi": "iasi", "Timis": "timis", "Brasov": "brasov"}

OUTPUT_COLUMNS = [
    "person_id", "t", "company_group", "left_12m",
    "tenure_company_m", "tenure_role_m", "months_since_promotion", "n_promotions_company",
    "n_prev_employers", "avg_prev_tenure_m", "n_employer_changes_3y", "total_experience_m",
    "seniority_level", "job_family", "edu_level", "edu_it", "region",
    "attrition_12m", "attrition_12m_n", "layoff_events_12m", "layoff_rate_12m", "headcount_growth_yoy",
    "tech_layoffs_12m_k", "tech_layoff_events_12m",
    "ro_unemployment_rate", "ict_job_vacancy_rate", "county_unemployment_rate",
    "dates_imputed",
]  # fmt: skip


# ---------------------------------------------------------------- reference data


@dataclass
class Company:
    group: str
    pattern: re.Pattern
    li_ids: set[str]
    layoffs_names: set[str]


def split_list(value: str) -> set[str]:
    return {part.strip() for part in (value or "").split(";") if part.strip()}


def load_companies(path: Path = COMPANIES_CSV) -> list[Company]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [
        Company(row.company_group, re.compile(row.name_pattern), split_list(row.linkedin_urns), {n.lower() for n in split_list(row.layoffs_names)})
        for row in df.itertuples()
    ]


def match_company(name: str | None, li_id: str | None, companies: list[Company]) -> str | None:
    """Target company group of a LinkedIn company, by LinkedIn ID first and by name otherwise.

    Names like "Google via Qualitest Group" are contractors employed by the agency, so they do not match.
    """
    if li_id:
        for company in companies:
            if li_id in company.li_ids:
                return company.group
    folded = ascii_fold(name)
    if folded and not CONTRACTOR.search(folded):
        for company in companies:
            if company.pattern.search(folded):
                return company.group
    return None


# ---------------------------------------------------------------- title, education and location parsing


def seniority(title: str) -> int:
    """0 intern, 1 junior, 2 mid (default), 3 senior, 4 lead/staff/principal, 5 people manager, 6 director/head, 7 VP/C-level."""
    folded = ascii_fold(title)
    if INTERN.search(folded):
        return 0
    for level, pattern in SENIORITY[:2]:
        if pattern.search(folded):
            return level
    if re.search(r"\bmanager\b", folded) and not IC_MANAGER.search(folded):
        return 5
    for level, pattern in SENIORITY[2:]:
        if pattern.search(folded):
            return level
    return 2


def job_family(title: str) -> str:
    folded = ascii_fold(title)
    return next((name for name, pattern in JOB_FAMILIES if pattern.search(folded)), "other")


def education_level(degree: str, school: str) -> int:
    """0 no university degree, 1 bachelor, 2 master, 3 PhD."""
    folded = ascii_fold(degree)
    for level, pattern in ((3, PHD), (2, MASTER), (1, BACHELOR)):
        if pattern.search(folded):
            return level
    # A university with no degree name given is most likely a bachelor's.
    return 1 if not folded and UNIVERSITY.search(ascii_fold(school)) else 0


def locate(location: str) -> tuple[str | None, str]:
    """LinkedIn location -> (INS county name or None, region category)."""
    folded = ascii_fold(location)
    if not folded:
        return None, "unknown"
    for county, pattern in COUNTY_PATTERNS:
        if pattern.search(folded):
            return county, REGION_OF_COUNTY.get(county, "other_ro")
    if "romania" in folded:
        return None, "ro_unspecified"
    return None, "abroad"


# ---------------------------------------------------------------- positions and stints


def num(value) -> int | None:
    if value is None or value == "" or (isinstance(value, float) and math.isnan(value)):
        return None
    return int(float(value))


def prepare_positions(positions: pd.DataFrame, companies: list[Company]) -> dict[str, list[dict]]:
    """Positions per person with month indexes, employer key and target company group."""
    by_person = defaultdict(list)
    for p in positions.to_dict("records"):
        start_year, start_month = num(p.get("start_year")), num(p.get("start_month"))
        end_year, end_month = num(p.get("end_year")), num(p.get("end_month"))
        name = p.get("company_name") or None
        li_id = str(num(p.get("company_li_id"))) if num(p.get("company_li_id")) is not None else None
        if start_year is None or not (name or li_id):
            continue

        start = month_index(start_year, start_month or UNKNOWN_MONTH)
        end = month_index(end_year, end_month or UNKNOWN_MONTH) if end_year else None
        if end is not None and end < start:
            end = start
        title = p.get("title") or ""
        group = match_company(name, li_id, companies)
        text = ascii_fold(" ".join([title, name or "", p.get("employment_type") or ""]))
        by_person[p["person_id"]].append(
            {
                "order": num(p.get("position_order")) or 0,
                "title": title,
                "title_key": ascii_fold(title),
                "company_name": name,
                # Positions at the same employer form stints: the target group (so Amazon and AWS are one
                # employer), otherwise the LinkedIn company ID, otherwise the company name.
                "employer": group or (f"li:{li_id}" if li_id else f"name:{ascii_fold(name)}"),
                "company_group": group,
                "location": p.get("location") or "",
                "start": start,
                "end": end,
                "imputed": start_month is None or (end_year is not None and end_month is None),
                "freelance": group is None and bool(FREELANCE.search(text)),
            }
        )
    return by_person


def build_stints(positions: list[dict]) -> list[dict]:
    """Merge one person's consecutive positions at the same employer into stints (freelance work excluded)."""
    by_employer = defaultdict(list)
    for position in positions:
        if not position["freelance"]:
            by_employer[position["employer"]].append(position)

    stints = []
    for employer, items in by_employer.items():
        current = None
        for position in sorted(items, key=lambda p: p["start"]):
            if current and (current["end"] is None or position["start"] <= current["end"] + STINT_GAP):
                current["positions"].append(position)
                current["end"] = None if current["end"] is None or position["end"] is None else max(current["end"], position["end"])
                current["imputed"] = current["imputed"] or position["imputed"]
            else:
                current = {
                    "employer": employer,
                    "company_group": position["company_group"],
                    "start": position["start"],
                    "end": position["end"],
                    "positions": [position],
                    "imputed": position["imputed"],
                }
                stints.append(current)
    return stints


def is_active(stint: dict, t: int) -> bool:
    return stint["start"] < t and (stint["end"] is None or stint["end"] >= t)


# ---------------------------------------------------------------- features known at t


def history_features(stint: dict, stints: list[dict], positions: list[dict], t: int) -> dict:
    # Within a month, LinkedIn lists the most recent position first (lower order).
    in_stint = sorted((p for p in stint["positions"] if p["start"] < t), key=lambda p: (p["start"], -p["order"]))
    current = in_stint[-1]
    title_changes = [nxt["start"] for prev, nxt in zip(in_stint, in_stint[1:]) if nxt["title_key"] != prev["title_key"]]
    tenure = t - stint["start"]
    previous = [s for s in stints if s is not stint and s["end"] is not None and s["end"] < t]
    durations = [s["end"] - s["start"] + 1 for s in previous]
    return {
        "tenure_company_m": tenure,
        "tenure_role_m": t - current["start"],
        "months_since_promotion": t - title_changes[-1] if title_changes else tenure,
        "n_promotions_company": len(title_changes),
        "n_prev_employers": len(previous),
        "avg_prev_tenure_m": round(sum(durations) / len(durations), 1) if durations else None,
        "n_employer_changes_3y": sum(1 for s in previous if s["end"] >= t - 36),
        "total_experience_m": t - min(p["start"] for p in positions if not p["freelance"] and p["start"] < t),
        "seniority_level": seniority(current["title"]),
        "job_family": job_family(current["title"]),
    }


def education_features(educations: list[dict], t: int) -> dict:
    if not educations:  # no Education section: unknown, not "no degree"
        return {"edu_level": None, "edu_it": None}
    completed = [e for e in educations if e["end"] is not None and e["end"] < t]
    return {
        "edu_level": max((e["level"] for e in completed), default=0),
        "edu_it": int(any(e["it"] for e in completed if e["level"] >= 1)),
    }


def location_features(stint: dict, positions: list[dict], t: int) -> dict:
    """Location of the current role, else of the most recent earlier position that has one."""
    newest_first = lambda items: sorted((p for p in items if p["start"] < t), key=lambda p: (p["start"], -p["order"]), reverse=True)  # noqa: E731
    candidates = newest_first(stint["positions"]) + newest_first(positions)
    location = next((p["location"] for p in candidates if p["location"]), "")
    county, region = locate(location)
    return {"region": region, "_county": county}


def prepare_educations(educations: pd.DataFrame) -> dict[str, list[dict]]:
    by_person = defaultdict(list)
    for e in educations.to_dict("records"):
        end_year = num(e.get("end_year"))
        degree, school, field = e.get("degree") or "", e.get("school_name") or "", e.get("field_of_study") or ""
        level = education_level(degree, school)
        by_person[e["person_id"]].append(
            {
                "end": month_index(end_year, num(e.get("end_month")) or UNKNOWN_MONTH) if end_year else None,
                "level": level,
                "it": bool(IT_EDUCATION.search(ascii_fold(" ".join([field, degree, school])))),
            }
        )
    return by_person


def person_rows(person_id: str, positions: list[dict], stints: list[dict], educations: list[dict], scrape_month: int, ref_months: list[int]) -> list[dict]:
    rows = []
    last_observed = scrape_month - REPORTING_BUFFER
    for t in ref_months:
        # Same rule for every row, whatever the outcome, so dropping rows cannot bias the label.
        if t + HORIZON - 1 > last_observed:
            continue
        active = [s for s in stints if s["company_group"] and is_active(s, t)]
        if not active:
            continue
        stint = min(active, key=lambda s: s["start"])  # main job: the longest-held target job
        rows.append(
            {
                "person_id": person_id,
                "t": t,
                "company_group": stint["company_group"],
                "left_12m": int(stint["end"] is not None and stint["end"] <= t + HORIZON - 1),
                **history_features(stint, stints, positions, t),
                **education_features(educations, t),
                **location_features(stint, positions, t),
                "dates_imputed": int(stint["imputed"]),
            }
        )
    return rows


# ---------------------------------------------------------------- company and market features


def add_attrition(rows: list[dict], stints_by_person: dict[str, list[dict]]) -> None:
    """Share of the other sampled employees of the company at t-12 who left before t, smoothed toward the overall share."""
    target = [(pid, s) for pid, stints in stints_by_person.items() for s in stints if s["company_group"]]
    for row in rows:
        t, start = row["t"], row["t"] - HORIZON
        n = leavers = all_n = all_leavers = 0
        for pid, stint in target:
            if pid == row["person_id"] or not is_active(stint, start):
                continue
            left = stint["end"] is not None and stint["end"] < t
            all_n += 1
            all_leavers += left
            if stint["company_group"] == row["company_group"]:
                n += 1
                leavers += left
        overall = all_leavers / all_n if all_n else None
        row["attrition_12m"] = (
            round((leavers + ATTRITION_PRIOR_WEIGHT * overall) / (n + ATTRITION_PRIOR_WEIGHT), 4) if overall is not None else None
        )
        row["attrition_12m_n"] = n


class AsOf:
    """Values that become usable at a given month; returns the latest value usable at t."""

    def __init__(self, points: list[tuple[int, float]]):
        points = sorted((m, v) for m, v in points if v is not None and not (isinstance(v, float) and math.isnan(v)))
        self.months = [m for m, _ in points]
        self.values = [v for _, v in points]

    def at(self, t: int, back: int = 0) -> float | None:
        """Latest value usable at t (back=1: the one before it)."""
        i = bisect_right(self.months, t) - 1 - back
        return self.values[i] if i >= 0 else None


def headcount_series(headcount: pd.DataFrame | None) -> dict[str, AsOf]:
    if headcount is None:
        return {}
    points = defaultdict(list)
    for row in headcount.to_dict("records"):
        if num(row.get("employees")) is None:
            continue
        year, month = (int(x) for x in str(row["fiscal_year_end"]).split("-")[:2])
        points[row["company_group"]].append((month_index(year, month) + HEADCOUNT_LAG, float(row["employees"])))
    return {group: AsOf(p) for group, p in points.items()}


def add_layoffs(rows: list[dict], layoffs: pd.DataFrame | None, headcount: pd.DataFrame | None, companies: list[Company]) -> None:
    columns = ["layoff_events_12m", "layoff_rate_12m", "headcount_growth_yoy", "tech_layoffs_12m_k", "tech_layoff_events_12m"]
    staff = headcount_series(headcount)
    for row in rows:
        series = staff.get(row["company_group"])
        current, previous = (series.at(row["t"]), series.at(row["t"], back=1)) if series else (None, None)
        row["headcount_growth_yoy"] = round(current / previous - 1, 4) if current and previous else None
    if layoffs is None:
        for row in rows:
            row.update({c: None for c in columns if c != "headcount_growth_yoy"})
        return

    group_of = {name: c.group for c in companies for name in c.layoffs_names}
    events = layoffs.assign(group=layoffs["company"].str.strip().str.lower().map(group_of))
    for row in rows:
        t = row["t"]
        window = events[(events["month"] >= t - HORIZON) & (events["month"] < t)]
        own = window[window["group"] == row["company_group"]]
        staff_now = staff[row["company_group"]].at(t) if row["company_group"] in staff else None
        if own.empty:
            rate = 0.0
        elif staff_now:
            estimated = own["laid_off"].fillna(own["percentage"] * staff_now)
            rate = round(estimated.sum() / staff_now, 5) if estimated.notna().any() else None
        else:
            rate = None
        row.update(
            {
                "layoff_events_12m": len(own),
                "layoff_rate_12m": rate,
                "tech_layoffs_12m_k": round(window["laid_off"].sum() / 1000, 1),
                "tech_layoff_events_12m": len(window),
            }
        )


def add_macro(rows: list[dict], unemployment: pd.DataFrame | None, vacancies: pd.DataFrame | None, county: pd.DataFrame | None) -> None:
    def monthly(df: pd.DataFrame, lag: int) -> AsOf:
        return AsOf([(parse_month(p) + lag, v) for p, v in zip(df["period"], df["value"])])

    def quarterly(df: pd.DataFrame, lag: int) -> AsOf:
        points = []
        for period, value in zip(df["period"], df["value"]):
            year, quarter = period.split("-Q")
            points.append((month_index(int(year), int(quarter) * 3) + lag, value))
        return AsOf(points)

    national = monthly(unemployment, UNEMPLOYMENT_LAG) if unemployment is not None else None
    ict = quarterly(vacancies, VACANCY_LAG) if vacancies is not None else None
    by_county = {name: monthly(group, COUNTY_UNEMPLOYMENT_LAG) for name, group in county.groupby("region")} if county is not None else {}
    for row in rows:
        series = by_county.get(row.pop("_county", None))
        row["ro_unemployment_rate"] = national.at(row["t"]) if national else None
        row["ict_job_vacancy_rate"] = ict.at(row["t"]) if ict else None
        row["county_unemployment_rate"] = series.at(row["t"]) if series else None


# ---------------------------------------------------------------- assembly


def reference_months(first: str = FIRST_REFERENCE, last: str = LAST_REFERENCE, step: int = STEP_MONTHS) -> list[int]:
    return list(range(parse_month(first), parse_month(last) + 1, step))


def build_dataset(
    profiles: pd.DataFrame,
    positions: pd.DataFrame,
    educations: pd.DataFrame,
    companies: list[Company],
    layoffs: pd.DataFrame | None = None,
    headcount: pd.DataFrame | None = None,
    unemployment: pd.DataFrame | None = None,
    vacancies: pd.DataFrame | None = None,
    county: pd.DataFrame | None = None,
    ref_months: list[int] | None = None,
) -> pd.DataFrame:
    ref_months = ref_months or reference_months()
    positions_by_person = prepare_positions(positions, companies)
    educations_by_person = prepare_educations(educations)
    stints_by_person = {pid: build_stints(items) for pid, items in positions_by_person.items()}

    rows = []
    for profile in profiles.to_dict("records"):
        pid = profile["person_id"]
        scraped = pd.Timestamp(profile["scraped_at"])
        rows += person_rows(
            pid,
            positions_by_person.get(pid, []),
            stints_by_person.get(pid, []),
            educations_by_person.get(pid, []),
            month_index(scraped.year, scraped.month),
            ref_months,
        )

    add_attrition(rows, stints_by_person)
    add_layoffs(rows, layoffs, headcount, companies)
    add_macro(rows, unemployment, vacancies, county)
    for row in rows:
        row["t"] = format_month(row["t"])
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


# ---------------------------------------------------------------- loading files


def read_optional(path: Path, **kwargs) -> pd.DataFrame | None:
    if not path.exists():
        print(f"Missing {path.relative_to(ROOT_DIR)}: the features that use it are left empty")
        return None
    return pd.read_csv(path, **kwargs)


def load_layoffs(folder: Path = RAW_DIR / "layoffs") -> pd.DataFrame | None:
    """Latest layoffs.fyi CSV, from the Airtable export or a Kaggle mirror (column names differ between them)."""
    files = sorted(folder.glob("*.csv"))
    if not files:
        print("Missing data/raw/layoffs/*.csv: the layoff features are left empty (see data/README.md)")
        return None
    df = pd.read_csv(files[-1], dtype=str, keep_default_na=False)
    keys = {re.sub(r"[^a-z]", "", c.lower()) or c: c for c in df.columns}

    def column(*names: str) -> pd.Series:
        for name in names:
            if name in keys:
                return df[keys[name]]
        raise ValueError(f"{files[-1].name} has none of the columns {names}; found {list(df.columns)}")

    def percentage(value: str) -> float | None:
        """"10%", "10" or "0.1" -> 0.1"""
        value = value.strip()
        try:
            number = float(value.rstrip("%"))
        except ValueError:
            return None
        return number / 100 if value.endswith("%") or number > 1 else number

    dates = pd.to_datetime(column("date"), errors="coerce", format="mixed")
    laid_off = pd.to_numeric(column("laidoff", "totallaidoff", "laidoffcount").str.replace(",", ""), errors="coerce")
    out = pd.DataFrame(
        {
            "company": column("company"),
            "month": [month_index(d.year, d.month) if pd.notna(d) else None for d in dates],
            "laid_off": laid_off,
            "percentage": column("%", "percentage", "percentagelaidoff").map(percentage),
        }
    ).dropna(subset=["month"])
    out["month"] = out["month"].astype(int)
    print(f"Layoffs: {len(out)} events from {files[-1].name}")
    return out


def report(dataset: pd.DataFrame, profiles: pd.DataFrame, positions: pd.DataFrame, companies: list[Company]) -> None:
    people_with_rows = dataset["person_id"].nunique()
    print(f"\n{len(profiles)} profiles parsed -> {people_with_rows} with at least one row -> {len(dataset)} rows")
    if dataset.empty:
        return
    by_t = dataset.groupby("t")["left_12m"].agg(rows="size", left_rate="mean").round(3)
    print("\nRows and share who leave within 12 months, per reference date:\n" + by_t.to_string())
    by_company = dataset.groupby("company_group")["left_12m"].agg(rows="size", left_rate="mean").round(3)
    print("\nPer company:\n" + by_company.sort_values("rows", ascending=False).to_string())
    missing = dataset.isna().mean().round(3)
    print("\nShare missing per column (only columns with gaps):\n" + missing[missing > 0].to_string())

    # Company names that mention a target company but did not match its pattern: candidates to fix in companies.csv.
    words = [c.group.lower() for c in companies]
    names = Counter(ascii_fold(n) for n in positions["company_name"].fillna("") if n)
    near = [(n, k) for n, k in names.most_common() if any(w in n for w in words) and not match_company(n, None, companies)]
    if near:
        print("\nCompany names that mention a target company but are not matched (check companies.csv):")
        for name, count in near[:15]:
            print(f"  {count:4d}  {name}")


def main() -> int:
    if not (INTERIM_DIR / "positions.csv").exists():
        print("Missing data/interim/positions.csv: run parse_profiles.py first", file=sys.stderr)
        return 1

    def interim(name: str) -> pd.DataFrame:
        return pd.read_csv(INTERIM_DIR / name, dtype=str, keep_default_na=False)

    companies = load_companies()
    profiles, positions, educations = interim("profiles.csv"), interim("positions.csv"), interim("educations.csv")
    dataset = build_dataset(
        profiles,
        positions,
        educations,
        companies,
        layoffs=load_layoffs(),
        headcount=read_optional(RAW_DIR / "headcount" / "headcount.csv", dtype=str, keep_default_na=False),
        unemployment=read_optional(RAW_DIR / "eurostat" / "une_rt_m_RO.csv"),
        vacancies=read_optional(RAW_DIR / "eurostat" / "jvs_q_nace2_RO_J.csv"),
        county=read_optional(RAW_DIR / "ins" / "som103b.csv"),
    )
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(OUTPUT_CSV, index=False)
    print(f"Wrote {OUTPUT_CSV.relative_to(ROOT_DIR)}")
    report(dataset, profiles, positions, companies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
