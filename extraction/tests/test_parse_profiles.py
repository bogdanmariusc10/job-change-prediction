"""Parsing of LinkedIn's normalized JSON. The payloads mimic the Dash format; check them against a real
response in data/raw/linkedin/ after the first scrape and update them if LinkedIn's fields differ."""

from parse_profiles import li_id, parse_educations, parse_positions

POSITIONS = {
    "data": {"*elements": ["urn:li:fsd_profilePosition:(ACoAA,2)", "urn:li:fsd_profilePosition:(ACoAA,1)"], "paging": {"total": 2}},
    "included": [
        {
            "$type": "com.linkedin.voyager.dash.identity.profile.Position",
            "entityUrn": "urn:li:fsd_profilePosition:(ACoAA,1)",
            "title": "Software Engineer",
            "companyName": "Microsoft",
            "companyUrn": "urn:li:fsd_company:1035",
            "locationName": "Bucharest, Romania",
            "dateRange": {"start": {"month": 1, "year": 2019}, "end": {"month": 3, "year": 2024}},
            "*employmentType": "urn:li:fsd_employmentType:1",
            "description": "free text that must not be kept",
        },
        {
            "$type": "com.linkedin.voyager.dash.identity.profile.Position",
            "entityUrn": "urn:li:fsd_profilePosition:(ACoAA,2)",
            "title": {"text": "Engineer"},
            "*company": "urn:li:fsd_company:9999",
            "dateRange": {"start": {"year": 2024}},
        },
        {"$type": "com.linkedin.voyager.dash.organization.Company", "entityUrn": "urn:li:fsd_company:9999", "name": "UiPath"},
        {"$type": "com.linkedin.voyager.dash.identity.profile.EmploymentType", "entityUrn": "urn:li:fsd_employmentType:1", "name": "Full-time"},
    ],
}

EDUCATIONS = {
    "data": {"*elements": ["urn:li:fsd_profileEducation:(ACoAA,1)"]},
    "included": [
        {
            "$type": "com.linkedin.voyager.dash.identity.profile.Education",
            "entityUrn": "urn:li:fsd_profileEducation:(ACoAA,1)",
            "schoolName": "Universitatea Politehnica din București",
            "schoolUrn": "urn:li:fsd_school:12345",
            "degreeName": "Bachelor's degree",
            "fieldOfStudy": "Computer Science",
            "dateRange": {"start": {"year": 2015}, "end": {"year": 2019}},
        }
    ],
}


def test_positions_keep_order_company_and_dates_but_not_descriptions():
    rows = parse_positions(POSITIONS, "p1")
    assert [r["company_name"] for r in rows] == ["UiPath", "Microsoft"]
    newest, oldest = rows
    assert newest["title"] == "Engineer"
    assert newest["company_li_id"] == "9999"
    assert (newest["start_year"], newest["start_month"], newest["end_year"]) == (2024, None, None)
    assert oldest["company_li_id"] == "1035"
    assert oldest["employment_type"] == "Full-time"
    assert (oldest["start_year"], oldest["start_month"], oldest["end_year"], oldest["end_month"]) == (2019, 1, 2024, 3)
    assert all("description" not in r for r in rows)


def test_educations():
    [row] = parse_educations(EDUCATIONS, "p1")
    assert row["degree"] == "Bachelor's degree"
    assert row["school_li_id"] == "12345"
    assert (row["start_year"], row["end_year"], row["end_month"]) == (2015, 2019, None)


def test_li_id():
    assert li_id("urn:li:fsd_company:1035") == "1035"
    assert li_id("urn:li:company:1035") == "1035"
    assert li_id(None) is None
