# Job Change Prediction Experiments

Predicts whether a person working at a big tech company in Romania leaves their employer within the next 12 months.
The dataset is built from public LinkedIn profiles collected with our own scraper, joined with layoff events
(layoffs.fyi), company headcount (annual reports) and labour market statistics (Eurostat, INS).

Each row is a (person, reference date) pair: one profile yields a row every 6 months while the person worked at a
target company, with features computed only from what was known at that date. Column meanings and sources are in
[data/README.md](data/README.md).

## Repository

```
extraction/   scripts that collect, parse and join the data (pipeline below)
data/         reference tables, public raw data and the final dataset (see data/README.md)
docs/         presentations
```

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
uv run pytest            # checks the dataset logic on hand-computed examples
```

## Pipeline

| Step | Command | Output |
|---|---|---|
| 1. Find profiles | `uv run extraction/collect_urls.py --target 800` | `data/interim/profile_urls.csv` |
| 2. Scrape profiles | `uv run extraction/linkedin_scraper.py --limit 40` (daily) | `data/raw/linkedin/<id>/*.json` |
| 3. Parse profiles | `uv run extraction/parse_profiles.py` | `data/interim/{profiles,positions,educations}.csv` |
| 4. Public data | `uv run extraction/fetch_public_data.py`, plus the layoffs.fyi CSV by hand | `data/raw/{eurostat,ins,layoffs}/` |
| 5. Build dataset | `uv run extraction/build_dataset.py` | `data/processed/dataset.csv` |
| 6. Descriptive figures | `uv run extraction/describe_dataset.py` | `docs/figures/*.png` |

Steps 3 and 5 can be rerun at any time; they only read local files.

### 1. Finding profiles

`collect_urls.py` runs Google "X-ray" searches such as `site:ro.linkedin.com/in "Oracle" "engineer" ("Bucharest" OR "București")`
through the [Serper](https://serper.dev) API (set `SERPER_API_KEY` in `.env`), for every target company in
`data/reference/companies.csv` x role keyword x city. No request goes to LinkedIn. Every query is logged in
`data/interim/search_log.csv`, so a rerun continues where the last one stopped. `--dry-run` prints the queries
without calling the API; `--add-urls FILE` adds profile URLs found by hand.

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
`data/interim/scrape_status.json`. It stops at the first response that is not HTTP 200; rerun it later to continue.
`--no-education` skips the education request, `--skip ID` excludes a profile, `--verbose` logs every request.

Automated access is against LinkedIn's terms and can get the account restricted: keep batches small (tens of
profiles a day) and keep the delays.

### 5. Building the dataset

`build_dataset.py` merges consecutive positions at the same employer into stints (a title change there is a
promotion, not a departure), creates a row for every reference date at which a target-company stint was active,
labels it 1 if that stint ends within 12 months, and adds the features. It also prints the number of rows and the
share of leavers per date and per company, the share of missing values per column, and company names that look
like a target company but did not match its pattern in `companies.csv`.
