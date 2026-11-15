# Data

```
data/
  reference/companies.csv     target companies: search term, name pattern, LinkedIn IDs, layoffs.fyi names
  raw/eurostat/               une_rt_m_RO.csv, jvs_q_nace2_RO_J.csv      (fetch_public_data.py)
  raw/ins/                    som103b.csv                                (fetch_public_data.py)
  raw/layoffs/                layoffs.fyi snapshot                       (added by hand, see below)
  raw/headcount/              headcount.csv, global employees per fiscal year, with source_url
  raw/linkedin/               raw LinkedIn responses                     NOT PUBLISHED
  interim/                    profile URLs, scrape status, parsed tables NOT PUBLISHED
  processed/dataset.csv       the modeling dataset, pseudonymized
```

## Why the LinkedIn data is not in the repository

Public LinkedIn profiles are still personal data under the GDPR. `raw/linkedin/` and `interim/` hold names,
profile URLs and job titles, so they are git-ignored, kept only on the author's machine and deleted at the end
of the project. The published `processed/dataset.csv` keeps only derived features: people are identified by a
salted hash, titles are reduced to a seniority level and a job family, and locations to a region.

## Public sources

| File | Source | Notes |
|---|---|---|
| `raw/eurostat/une_rt_m_RO.csv` | Eurostat [`une_rt_m`](https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table) | unemployment rate, Romania, seasonally adjusted, % of active population |
| `raw/eurostat/jvs_q_nace2_RO_J.csv` | Eurostat [`jvs_q_nace2`](https://ec.europa.eu/eurostat/databrowser/view/jvs_q_nace2/default/table) | job vacancy rate, Romania, NACE section J (information and communication), not seasonally adjusted |
| `raw/ins/som103b.csv` | INS TEMPO `SOM103B` | registered unemployment rate at the end of each month, per county |
| `raw/layoffs/*.csv` | [layoffs.fyi](https://layoffs.fyi) | one row per layoff event: company, date, people laid off, % of workforce |
| `raw/headcount/headcount.csv` | 10-K filings and annual reports | global employees at fiscal-year end, one `source_url` per value |

### Adding the layoffs.fyi snapshot

layoffs.fyi has no download API and its Airtable view does not allow CSV downloads. The snapshot used here is the
Kaggle mirror [swaptr/layoffs-2022](https://www.kaggle.com/datasets/swaptr/layoffs-2022) (version 488, updated
2026-11-15), saved as `raw/layoffs/layoffs_fyi_<YYYY-MM-DD>.csv`. `build_dataset.py` reads the newest file and accepts
both the Kaggle column names (`company`, `date`, `total_laid_off`, `percentage_laid_off`) and the Airtable ones
(`Company`, `Date`, `# Laid Off`, `%`).

LinkedIn positions are matched to a target company by LinkedIn company ID first and by `name_pattern` otherwise;
names such as "Google via Qualitest Group" are contractors and do not match. Fitbit counts as Google: Alphabet
closed the acquisition on 14 January 2021 and the Bucharest Fitbit office became part of Google, so time at Fitbit
counts toward Google tenure. Wind River is not mapped to Intel, because Intel sold it in 2018.

Layoffs are matched to a target company by the names in `reference/companies.csv` (`layoffs_names`), which include
the consolidated subsidiaries listed in the data (e.g. LinkedIn and GitHub for Microsoft, Waymo and Verily for
Alphabet), because the headcount used as the denominator includes them too. Qualtrics is left out: SAP sold it in 2023.

Limitations: the events are global and only those reported publicly; between 2020-03 and 2025-06, 31 of the 86
events at target companies give neither a count nor a percentage. `layoff_events_12m` counts them, while
`layoff_rate_12m` cannot, so the rate is a lower bound.

## `processed/dataset.csv`

One row per person and reference date `t` (every 6 months from 2021-01 to 2025-07) at which the person worked
at a target company. Every feature only uses information available before `t`.

| Column | Meaning |
|---|---|
| `person_id` | pseudonymous person ID (the same person appears at several dates) |
| `t` | reference date, `YYYY-MM` (the first day of that month) |
| `company_group` | target company the person works at on `t` |
| **`left_12m`** | **label: 1 if the person leaves that company in the 12 months from `t`** |
| `tenure_company_m` | months at the company (consecutive roles there count as one stint) |
| `tenure_role_m` | months in the current role |
| `months_since_promotion` | months since the last title change at the company, or tenure if none |
| `n_promotions_company` | title changes at the company before `t` |
| `n_prev_employers` | employers left before `t` |
| `avg_prev_tenure_m` | average months spent at those employers (empty for a first job) |
| `n_employer_changes_3y` | employers left in the 3 years before `t` |
| `total_experience_m` | months since the first job |
| `seniority_level` | 0 intern, 1 junior, 2 mid, 3 senior, 4 lead/staff/principal, 5 people manager, 6 director/head, 7 VP/C-level (from the title) |
| `job_family` | engineering, data, product, design, sales, marketing, hr, finance_legal, support_ops, research, consulting, other (from the title) |
| `edu_level` | highest degree finished before `t`: 0 none, 1 bachelor, 2 master, 3 PhD (empty when the profile lists no education) |
| `edu_it` | 1 if a finished degree is in a technical field |
| `region` | bucuresti_ilfov, cluj, iasi, timis, brasov, other_ro, ro_unspecified, abroad, unknown |
| `attrition_12m` | share of the other sampled employees of the company on `t-12` who left before `t`, smoothed toward the overall share |
| `attrition_12m_n` | number of sampled colleagues behind `attrition_12m` (diagnostic, not a feature) |
| `layoff_events_12m` | layoffs.fyi events at the company in the 12 months before `t` |
| `layoff_rate_12m` | people laid off in those events / global headcount (uses the % when the count is missing) |
| `headcount_growth_yoy` | change in global headcount between the last two published fiscal years |
| `tech_layoffs_12m_k` | thousands of people laid off across all of layoffs.fyi in the 12 months before `t` |
| `tech_layoff_events_12m` | number of those events |
| `ro_unemployment_rate` | latest Romanian unemployment rate published before `t` |
| `ict_job_vacancy_rate` | latest ICT job vacancy rate published before `t` |
| `county_unemployment_rate` | latest registered unemployment rate of the county of the job published before `t` |
| `dates_imputed` | 1 if a date of the stint had only a year (the month is then taken as July) |

Public series are matched to `t` by publication date, not by period: monthly unemployment is used from 2 months
after the month, county unemployment from 3 months after, quarterly vacancies from 4 months after the quarter
ends, and headcount from 3 months after the fiscal year ends.
