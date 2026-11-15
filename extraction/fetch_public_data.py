"""Download the public time series used as features.

Usage:
    uv run extraction/fetch_public_data.py

Writes, with one row per period:
    data/raw/eurostat/une_rt_m_RO.csv     monthly unemployment rate, Romania, seasonally adjusted
    data/raw/eurostat/jvs_q_nace2_RO_J.csv quarterly job vacancy rate, Romania, section J (information & communication)
    data/raw/ins/som103b.csv               monthly registered unemployment rate per county (INS TEMPO SOM103B)

layoffs.fyi has no download API, so its CSV is added by hand (see data/README.md).
"""

import io
import sys

import pandas as pd
import requests

from common import RAW_DIR

START_YEAR = 2018
EUROSTAT_SDMX = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"
TEMPO = "http://statistici.insse.ro:8077/tempo-ins"
RO_MONTHS = {
    "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6,
    "iulie": 7, "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12,
}  # fmt: skip


def eurostat(dataset: str, key: str, start: str) -> pd.DataFrame:
    url = f"{EUROSTAT_SDMX}/{dataset}/{key}"
    response = requests.get(url, params={"format": "SDMX-CSV", "startPeriod": start}, timeout=60)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.text))


def fetch_unemployment() -> pd.DataFrame:
    df = eurostat("une_rt_m", "M.SA.TOTAL.PC_ACT.T.RO", f"{START_YEAR}-01")
    return df.rename(columns={"TIME_PERIOD": "period", "OBS_VALUE": "value"})[["period", "value"]]


def fetch_job_vacancies() -> pd.DataFrame:
    df = eurostat("jvs_q_nace2", "Q.NSA.J.TOTAL.JVR.RO", f"{START_YEAR}-Q1")
    return df.rename(columns={"TIME_PERIOD": "period", "OBS_VALUE": "value"})[["period", "value"]]


def fetch_county_unemployment() -> pd.DataFrame:
    """INS TEMPO has no documented API; these are the calls its web interface makes."""
    matrix = requests.get(f"{TEMPO}/matrix/SOM103B", timeout=60).json()
    sexes, regions, months, units = (dim["options"] for dim in matrix["dimensionsMap"])

    def month_of(label: str) -> tuple[int, int] | None:
        parts = label.split()  # "Luna ianuarie 2024"
        return (int(parts[2]), RO_MONTHS[parts[1].lower()]) if len(parts) == 3 else None

    selected_months = [m for m in months if (month_of(m["label"].strip()) or (0, 0))[0] >= START_YEAR]
    total_sex = next(s for s in sexes if s["label"].strip().lower() == "total")
    selection = [[total_sex], regions, selected_months, units]
    query = ":".join(",".join(str(option["nomItemId"]) for option in dim) for dim in selection)
    body = {
        "encQuery": query,
        "language": "ro",
        "matCode": "SOM103B",
        "matMaxDim": matrix["details"]["matMaxDim"],
        "matUMSpec": matrix["details"]["matUMSpec"],
        "matRegJ": matrix["details"]["matRegJ"],
    }
    response = requests.post(f"{TEMPO}/pivot", json=body, timeout=120)
    response.raise_for_status()

    df = pd.read_csv(io.StringIO(response.text), skipinitialspace=True)
    df.columns = ["sex", "region", "month", "unit", "value"]
    year_month = df["month"].str.strip().map(month_of)
    return pd.DataFrame(
        {
            "region": df["region"].str.strip(),
            "period": [f"{y:04d}-{m:02d}" for y, m in year_month],
            "value": pd.to_numeric(df["value"], errors="coerce"),
        }
    )


def save(df: pd.DataFrame, relative_path: str) -> None:
    path = RAW_DIR / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"{relative_path}: {len(df)} rows, {df['period'].min()} to {df['period'].max()}")


def main() -> int:
    save(fetch_unemployment(), "eurostat/une_rt_m_RO.csv")
    save(fetch_job_vacancies(), "eurostat/jvs_q_nace2_RO_J.csv")
    save(fetch_county_unemployment(), "ins/som103b.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
