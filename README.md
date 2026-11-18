# Job Change Prediction Experiments

Predicts whether a person working at a big tech company in Romania leaves their employer within the next 12 months.
The dataset is built from public LinkedIn profiles collected with our own scraper, joined with layoff events
(layoffs.fyi), company headcount (annual reports) and labour market statistics (Eurostat, INS).

Each row is a (person, reference date) pair: one profile yields a row every 6 months (2021-01 to 2025-07) while the
person worked at a target company, with features computed only from what was known at that date. Column meanings and
sources are in [data/README.md](data/README.md).

The current [data/processed/dataset.csv](data/processed/dataset.csv) has 2,764 rows from 355 people, 23 features and
5.0% positive rows (`left_12m = 1`).

## Repository

```
extraction/         scripts that collect, parse and join the data (pipeline below)
extraction/tests/   tests of the parsing and of the dataset rules on hand-computed examples
data/               reference tables, public raw data and the final dataset (see data/README.md)
docs/figures/       descriptive figures made by describe_dataset.py
docs/               presentations
```

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
uv run pytest            # checks the parsing and the dataset logic on hand-computed examples
```

## Pipeline

| Step | Command | Output |
|---|---|---|
| 1. Find profiles | `uv run extraction/collect_urls.py --target 1000` | `data/interim/profile_urls.csv` |
| 2. Scrape profiles | `uv run extraction/linkedin_scraper.py --limit 20 --min-delay 20 --max-delay 45` (one batch) | `data/raw/linkedin/<id>/*.json` |
| 3. Parse profiles | `uv run extraction/parse_profiles.py` | `data/interim/{profiles,positions,educations}.csv` |
| 4. Public data | `uv run extraction/fetch_public_data.py`, plus the layoffs.fyi CSV by hand | `data/raw/{eurostat,ins,layoffs}/` |
| 5. Build dataset | `uv run extraction/build_dataset.py` | `data/processed/dataset.csv` |
| 6. Descriptive figures | `uv run extraction/describe_dataset.py` | `docs/figures/*.png` |

Steps 3, 5 and 6 can be rerun at any time; they only read local files. `data/raw/linkedin/` and `data/interim/` hold
personal data and are git-ignored, so a fresh clone can run step 6 (without its first figure) but not steps 3 and 5.

### 1. Finding profiles

`collect_urls.py` runs Google "X-ray" searches such as `site:ro.linkedin.com/in "Oracle" "engineer" ("Bucharest" OR "București")`
through the [Serper](https://serper.dev) API (set `SERPER_API_KEY` in `.env`), for every target company in
`data/reference/companies.csv` x role keyword x city (Bucharest, Cluj, Iași, Timișoara, Brașov). No request goes to
LinkedIn. Every query is logged in `data/interim/search_log.csv`, so a rerun continues where the last one stopped.
`--target` stops at that many unique profiles (default 800), `--max-queries` caps the API calls of one run, `--pages`
sets the result pages per query, `--dry-run` prints the queries without calling the API and `--add-urls FILE` adds
profile URLs found by hand.

The scraper takes the profiles in the order of `profile_urls.csv`, so reordering that file sets the priority. In the
current collection, searches for SAP, Meta, Apple and Salesforce mostly returned people who never worked there
(for example Salesforce consultants and iOS developers), so those URLs were moved to the end of the queue.

The population is everyone with a current or past job at a target company: past employees are needed, otherwise
everyone in the sample stayed by construction. Search engines favour prominent profiles, which biases the sample
toward senior people.

### 2. Scraping

`linkedin_scraper.py` calls the same internal (Voyager) endpoints as the LinkedIn web app, authenticated with the
cookies of a normal browser login. Fill in `.env` from a browser logged in to the account used for scraping:

1. Open https://www.linkedin.com and log in (complete any security check).
2. In DevTools (`Cmd+Option+I`), **Application → Cookies → https://www.linkedin.com**, copy `li_at` into
   `LINKEDIN_LI_AT` and `JSESSIONID` into `LINKEDIN_JSESSIONID`.
3. In the DevTools **Console**, run `navigator.userAgent` and copy the result into `LINKEDIN_USER_AGENT`.

Do not log out in that browser while scraping. Per profile, the script makes three requests (profile, positions,
education) with random pauses, saves each response untouched and records progress in
`data/interim/scrape_status.json`. Profiles not tried yet come first and failed ones are retried last. It stops at
the first response that is not HTTP 200; rerun it later to continue.

| Option | Effect |
|---|---|
| `--limit N` | scrape at most N profiles in this run |
| `--min-delay`, `--max-delay` | seconds to wait between profiles (default 5 to 12) |
| `--no-education` | skip the education request |
| `--dry-run` | list the profiles that would be scraped, without logging in |
| `--skip ID` | mark a profile as skipped so no run tries it again |
| `--verbose` | log every request |

Automated access is against LinkedIn's terms and can get the account restricted. Long runs at the default delays
ended with HTTP 403 or a logged-out session, so keep batches small (`--limit 20`), keep the longer delays above and
pause between batches.

### 3. Parsing

`parse_profiles.py` turns the saved responses of every profile marked as scraped into three tables: profiles,
positions (title, company name and LinkedIn company ID, location, start and end month) and educations (school,
degree, field, dates). People are identified by `person_id`, a salted SHA-256 hash of the public profile ID. The salt
is created on the first run in `data/interim/pseudonym_salt.txt` and never leaves the machine, so the published IDs
cannot be linked back to a profile by hashing guessed profile IDs.

### 4. Public data

`fetch_public_data.py` downloads the Romanian unemployment rate and the ICT job vacancy rate from Eurostat and the
county unemployment rate (SOM103B) from INS TEMPO. The layoffs.fyi snapshot is added by hand and
`data/raw/headcount/headcount.csv` is kept by hand, with a source URL for every value; both are described in
[data/README.md](data/README.md).

### 5. Building the dataset

`build_dataset.py` matches every position to a target company, by LinkedIn company ID first and by the name pattern
in `companies.csv` otherwise. Names such as "Google via Qualitest Group" are contractors and do not match, and Fitbit
counts as Google (acquired in January 2021). It then merges consecutive positions at the same employer into stints (a
title change there is a promotion, not a departure), creates a row for every reference date at which a
target-company stint was active, labels it 1 if that stint ends within 12 months, and adds the features.

Features only use information available before the reference date: public series are joined by publication date,
not by period, and a row is kept only when its 12-month window ended at least 4 months before the profile was
scraped. The script also prints the number of rows and the share of leavers per date and per company, the share of
missing values per column, and company names that look like a target company but did not match its pattern in
`companies.csv`.

### 6. Descriptive figures

`describe_dataset.py` reads `data/processed/dataset.csv` and writes 10 figures to `docs/figures/`: the data
collection funnel, the class distribution, rows per person, the departure rate and tech layoffs by reference date,
the categorical distributions, the departure rate by group, the numeric features by class, missing values, Spearman
correlations and the public labour market series. The funnel also needs the local files in `data/interim/` and is
skipped without them. The figures show aggregates only.
