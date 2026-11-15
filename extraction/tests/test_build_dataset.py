"""Checks that the dataset rows match hand-computed values and that no feature looks past the reference date."""

import pandas as pd
import pytest

from build_dataset import (
    build_dataset,
    education_level,
    job_family,
    load_companies,
    locate,
    reference_months,
    seniority,
)

COMPANIES = load_companies()
SCRAPED_AT = "2026-11-15T10:00:00+00:00"


def position(person, order, title, company, start, end=None, location="Bucharest, Romania"):
    start_year, start_month = start
    end_year, end_month = end or ("", "")
    return {
        "person_id": person, "position_order": order, "title": title, "company_name": company,
        "company_li_id": "", "location": location, "employment_type": "",
        "start_year": start_year, "start_month": start_month, "end_year": end_year, "end_month": end_month,
    }  # fmt: skip


# Ana: an internship, Microsoft from Jan 2019 to Mar 2024 (senior from Jun 2022), then UiPath. LinkedIn lists newest first.
ANA = [
    position("ana", 0, "Software Engineer", "UiPath", (2024, 4), location="Bucureşti, România"),
    position("ana", 1, "Senior Software Engineer", "Microsoft", (2022, 6), (2024, 3)),
    position("ana", 2, "Software Engineer", "Microsoft", (2019, 1), (2022, 5)),
    position("ana", 3, "Software Engineer Intern", "Endava", (2018, 7), (2018, 12)),
]
ANA_EDUCATION = {
    "person_id": "ana", "school_name": "Universitatea Politehnica din București", "school_li_id": "",
    "degree": "Bachelor's degree", "field_of_study": "Computer Science",
    "start_year": 2015, "start_month": "", "end_year": 2019, "end_month": "",
}  # fmt: skip


def frame(rows):
    return pd.DataFrame(rows).astype(str).replace("None", "")


def build(positions, profiles=None, educations=(), **kwargs):
    people = profiles or [{"person_id": pid, "scraped_at": SCRAPED_AT} for pid in dict.fromkeys(p["person_id"] for p in positions)]
    education_columns = list(ANA_EDUCATION)
    return build_dataset(
        pd.DataFrame(people),
        frame(positions),
        frame(educations) if educations else pd.DataFrame(columns=education_columns),
        COMPANIES,
        **kwargs,
    ).set_index(["person_id", "t"])


def test_ana_rows_and_labels():
    data = build(ANA)
    ana = data.loc["ana"]
    assert list(ana.index) == ["2021-01", "2021-07", "2022-01", "2022-07", "2023-01", "2023-07", "2024-01", "2024-07", "2025-01", "2025-07"]
    assert ana["company_group"].tolist() == ["Microsoft"] * 7 + ["UiPath"] * 3
    # She leaves Microsoft in Mar 2024: inside the window of Jul 2023 and Jan 2024, not of Jan 2023.
    assert ana["left_12m"].tolist() == [0, 0, 0, 0, 0, 1, 1, 0, 0, 0]


def test_ana_example_from_the_slides():
    ana = build(ANA).loc["ana"]
    assert ana.loc["2022-07", ["tenure_company_m", "months_since_promotion", "left_12m"]].tolist() == [42, 1, 0]
    assert ana.loc["2023-07", ["tenure_company_m", "months_since_promotion", "left_12m"]].tolist() == [54, 13, 1]
    assert ana.loc["2024-07", ["tenure_company_m", "months_since_promotion", "left_12m"]].tolist() == [3, 3, 0]


def test_history_only_uses_the_past():
    ana = build(ANA).loc["ana"]
    # In 2022-07 only the Endava internship is a previous employer; UiPath is still in the future.
    row = ana.loc["2022-07"]
    assert row["n_prev_employers"] == 1
    assert row["avg_prev_tenure_m"] == 6
    assert row["total_experience_m"] == 48  # since Jul 2018
    assert row["n_promotions_company"] == 1
    assert row["seniority_level"] == 3
    # Before the promotion she was mid level and had no title change yet.
    assert ana.loc["2022-01", ["seniority_level", "n_promotions_company", "months_since_promotion"]].tolist() == [2, 0, 36]
    # At UiPath, Microsoft counts as a previous employer and as a change in the last 3 years.
    assert ana.loc["2024-07", ["n_prev_employers", "n_employer_changes_3y"]].tolist() == [2, 1]


def test_education_counts_only_after_graduation():
    ana = build(ANA, educations=[ANA_EDUCATION]).loc["ana"]
    # Graduation year only (2019) is taken as July 2019: not done in Jan 2019 (no row then), done by Jan 2021.
    assert ana.loc["2021-01", ["edu_level", "edu_it"]].tolist() == [1, 1]
    no_education = build(ANA).loc["ana"]
    assert no_education["edu_level"].isna().all()


def test_rows_whose_label_window_is_not_observed_are_dropped():
    # Scraped in Mar 2026: windows must end by Nov 2025 (4 months of reporting delay), so 2025-01 and 2025-07 go.
    data = build(ANA, profiles=[{"person_id": "ana", "scraped_at": "2026-03-01"}])
    assert list(data.loc["ana"].index)[-1] == "2024-07"


def test_role_changes_are_not_departures_and_amazon_aws_is_one_employer():
    bob = [
        position("bob", 0, "Solutions Architect", "Amazon Web Services (AWS)", (2022, 3)),
        position("bob", 1, "Support Engineer", "Amazon", (2020, 1), (2022, 2)),
    ]
    data = build(bob).loc["bob"]
    assert (data["left_12m"] == 0).all()
    assert data.loc["2023-01", "tenure_company_m"] == 36


def test_attrition_uses_other_people_and_only_the_past():
    others = [
        position("b", 0, "Engineer", "Microsoft", (2020, 1), (2022, 3)),  # left before 2022-07
        position("c", 0, "Engineer", "Microsoft", (2020, 1), (2023, 6)),  # leaves after 2022-07: not counted
        position("d", 0, "Engineer", "UiPath", (2020, 1)),  # another target company, stays
    ]
    row = build(ANA + others).loc[("ana", "2022-07")]
    # Microsoft: 2 colleagues employed in 2021-07, 1 left. Overall: 3 employed, 1 left.
    assert row["attrition_12m_n"] == 2
    assert row["attrition_12m"] == pytest.approx((1 + 10 * (1 / 3)) / (2 + 10), abs=1e-4)


def test_public_series_use_the_last_value_published_before_t():
    unemployment = pd.DataFrame({"period": ["2022-04", "2022-05", "2022-06"], "value": [1.0, 2.0, 3.0]})
    vacancies = pd.DataFrame({"period": ["2022-Q1", "2022-Q2"], "value": [0.5, 0.9]})
    county = pd.DataFrame({"region": ["Municipiul Bucuresti"] * 2, "period": ["2022-03", "2022-04"], "value": [1.1, 1.2]})
    layoffs = pd.DataFrame({"company": ["Microsoft", "Some Startup"], "month": [2023 * 12, 2023 * 12 + 1], "laid_off": [10000.0, 50.0], "percentage": [None, None]})
    headcount = pd.DataFrame(
        {"company_group": ["Microsoft", "Microsoft", "Microsoft"], "fiscal_year_end": ["2021-06-30", "2022-06-30", "2023-06-30"], "employees": ["181000", "221000", "221000"]}
    )
    ana = build(ANA, unemployment=unemployment, vacancies=vacancies, county=county, layoffs=layoffs, headcount=headcount).loc["ana"]

    row = ana.loc["2022-07"]
    assert row["ro_unemployment_rate"] == 2.0  # May (published by July); June is not out yet
    assert row["ict_job_vacancy_rate"] == 0.5  # Q1 is out by July, Q2 only in October
    assert row["county_unemployment_rate"] == 1.2  # April, with a 3-month lag
    assert row["layoff_events_12m"] == 0

    row = ana.loc["2023-07"]
    # The Jan 2023 Microsoft layoff is in the window; FY2023 headcount (Jun 2023) is not published yet.
    assert row["layoff_events_12m"] == 1
    assert row["layoff_rate_12m"] == pytest.approx(10000 / 221000, abs=1e-5)
    assert row["headcount_growth_yoy"] == pytest.approx(221000 / 181000 - 1, abs=1e-4)
    assert row["tech_layoff_events_12m"] == 2
    assert row["tech_layoffs_12m_k"] == 10.0


def test_reference_dates():
    assert len(reference_months()) == 10


@pytest.mark.parametrize(
    "title, level",
    [
        ("Software Engineer Intern", 0),
        ("Junior Developer", 1),
        ("Software Engineer", 2),
        ("Senior Product Manager", 3),
        ("Tech Lead", 4),
        ("Engineering Manager", 5),
        ("Senior Manager", 5),
        ("Product Manager", 2),
        ("Head of Data", 6),
        ("VP Engineering", 7),
    ],
)
def test_seniority(title, level):
    assert seniority(title) == level


@pytest.mark.parametrize(
    "title, family",
    [
        ("Senior Software Engineer", "engineering"),
        ("Data Engineer", "data"),
        ("Technical Support Engineer", "support_ops"),
        ("Technical Recruiter", "hr"),
        ("Account Executive", "sales"),
        ("Product Owner", "product"),
        ("UX Designer", "design"),
        ("Financial Analyst", "finance_legal"),
    ],
)
def test_job_family(title, family):
    assert job_family(title) == family


@pytest.mark.parametrize(
    "location, expected",
    [
        ("Bucureşti, România", ("Municipiul Bucuresti", "bucuresti_ilfov")),
        ("Cluj County, Romania", ("Cluj", "cluj")),
        ("Piatra Neamţ, Neamţ, Romania", ("Neamt", "other_ro")),
        ("Baia Mare, Maramureș, Romania", ("Maramures", "other_ro")),
        ("Romania", (None, "ro_unspecified")),
        ("London, United Kingdom", (None, "abroad")),
        ("", (None, "unknown")),
    ],
)
def test_locate(location, expected):
    assert locate(location) == expected


@pytest.mark.parametrize(
    "degree, school, level",
    [
        ("Master's degree", "ASE", 2),
        ("Licență", "Universitatea din București", 1),
        ("", "Universitatea Babeș-Bolyai", 1),
        ("PhD", "", 3),
        ("Diplomă de bacalaureat", "Colegiul Național Sfântul Sava", 0),
    ],
)
def test_education_level(degree, school, level):
    assert education_level(degree, school) == level


def test_company_matching():
    from build_dataset import match_company

    assert match_company("Amazon Development Center Romania", None, COMPANIES) == "Amazon"
    assert match_company("Metaminds", None, COMPANIES) is None
    assert match_company("Intelligent Systems SRL", None, COMPANIES) is None
    assert match_company("SAP", None, COMPANIES) == "SAP"
    assert match_company("Google", None, COMPANIES) == "Google"
    assert match_company("Google via Qualitest Group", "86585878", COMPANIES) is None
    assert match_company("Some Local Page Name", "1035", COMPANIES) == "Microsoft"
    # Fitbit became part of Google in January 2021; Intel's Bucharest center has its own page name.
    assert match_company("Fitbit (now part of Google)", "1132117", COMPANIES) == "Google"
    assert match_company("Fitbit", None, COMPANIES) == "Google"
    assert match_company("Intel Software and Development Center", None, COMPANIES) == "Intel"
    assert match_company("INTEL / GENPACT.COM", None, COMPANIES) is None
    assert match_company("Intellimind", None, COMPANIES) is None
