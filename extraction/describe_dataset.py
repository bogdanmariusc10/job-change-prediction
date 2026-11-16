"""Descriptive figures of the dataset for the phase 1 presentation.

Usage:
    uv run extraction/describe_dataset.py

Reads data/processed/dataset.csv and writes one PNG per figure to docs/figures/. The data collection funnel (01) also
needs the local files in data/interim/ and is skipped when they are missing, so the script also runs on a fresh
clone that only has the published dataset. Every figure shows aggregates only.
"""

import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.transforms import offset_copy  # noqa: E402

from build_dataset import OUTPUT_CSV, load_layoffs  # noqa: E402
from common import INTERIM_DIR, PROFILE_URLS_CSV, RAW_DIR, ROOT_DIR, SCRAPE_STATUS_JSON  # noqa: E402

FIGURES_DIR = ROOT_DIR / "docs" / "figures"

# Colors: blue is the data, gray is context ("stayed", small groups).
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
BLUE, GRAY = "#2a78d6", "#c3c2b7"
DIVERGING = LinearSegmentedColormap.from_list("red_gray_blue", ["#b8312f", "#efa29e", "#f0efec", "#86b6ef", "#1c5cab"])

MIN_GROUP_ROWS = 50  # groups with fewer rows are drawn in gray (or folded into "Other")

REGION_LABELS = {
    "bucuresti_ilfov": "Bucharest–Ilfov", "cluj": "Cluj", "timis": "Timiș", "iasi": "Iași", "brasov": "Brașov",
    "other_ro": "Other county", "ro_unspecified": "Romania, no city", "abroad": "Abroad", "unknown": "Unknown",
}  # fmt: skip
EDU_LABELS = {0: "No degree yet", 1: "Bachelor", 2: "Master", 3: "PhD", -1: "No education listed"}
SENIORITY_LABELS = {
    0: "Intern", 1: "Junior", 2: "Mid level", 3: "Senior", 4: "Lead / staff / principal",
    5: "People manager", 6: "Director / head", 7: "VP / C-level",
}  # fmt: skip
JOB_FAMILY_LABELS = {
    "engineering": "Engineering", "data": "Data", "product": "Product", "design": "Design", "sales": "Sales",
    "marketing": "Marketing", "hr": "HR", "finance_legal": "Finance & legal", "support_ops": "Support & operations",
    "research": "Research", "consulting": "Consulting", "other": "Other",
}  # fmt: skip
MISSING_REASONS = {
    "county_unemployment_rate": "job abroad or county unknown",
    "avg_prev_tenure_m": "first job, no previous employer",
    "layoff_rate_12m": "layoff events reported without a count or %",
    "headcount_growth_yoy": "UiPath published no headcount before 2021",
    "edu_level": "no education on the profile",
    "edu_it": "no education on the profile",
}
CORRELATION_COLUMNS = [
    "tenure_company_m", "tenure_role_m", "months_since_promotion", "n_promotions_company", "n_prev_employers",
    "avg_prev_tenure_m", "n_employer_changes_3y", "total_experience_m", "seniority_level", "edu_level", "edu_it",
    "attrition_12m", "layoff_events_12m", "layoff_rate_12m", "headcount_growth_yoy", "tech_layoffs_12m_k",
    "tech_layoff_events_12m", "ro_unemployment_rate", "ict_job_vacancy_rate", "county_unemployment_rate", "left_12m",
]  # fmt: skip

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.family": "sans-serif", "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 11, "text.color": INK, "axes.labelcolor": INK_2, "axes.edgecolor": AXIS, "axes.linewidth": 1,
        "xtick.color": AXIS, "ytick.color": AXIS, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.titlepad": 10,
        "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-", "axes.axisbelow": True,
        "legend.frameon": False, "savefig.dpi": 200, "savefig.bbox": "tight", "savefig.pad_inches": 0.3,
    }
)  # fmt: skip


# ---------------------------------------------------------------- helpers


def heading(fig, title: str, subtitle: str | None = None, note: str | None = None) -> None:
    """Title and subtitle above the plots, note below them (in points, so it works for any figure size)."""
    lines = subtitle.count("\n") + 1 if subtitle else 0
    if subtitle:
        fig.text(0, 1, subtitle, transform=offset_copy(fig.transFigure, fig=fig, y=8, units="points"), ha="left", va="bottom", fontsize=10.5, color=INK_2)
    fig.text(0, 1, title, transform=offset_copy(fig.transFigure, fig=fig, y=14 + 15 * lines, units="points"), ha="left", va="bottom", fontsize=15, fontweight="bold")
    if note:
        fig.text(0, 0, note, transform=offset_copy(fig.transFigure, fig=fig, y=-6, units="points"), ha="left", va="top", fontsize=9.5, color=MUTED)


def hbar(ax, labels: list[str], values: list[float], texts: list[str], colors: str | list[str] = BLUE, slots: int | None = None) -> None:
    """Horizontal bars, first label on top, every bar labeled at its tip (so no value axis).

    slots fixes the number of bar positions, so panels with fewer categories keep the same bar thickness.
    """
    y = np.arange(len(labels))
    ax.barh(y, values, height=0.62, color=colors)
    ax.set_yticks(y, labels)
    ax.set_ylim((slots or len(labels)) - 0.5, -0.5)
    ax.tick_params(axis="y", length=0)
    ax.spines[["bottom"]].set_visible(False)
    ax.xaxis.set_visible(False)
    top = max(values) or 1
    for yi, value, text in zip(y, values, texts):
        ax.text(value + top * 0.015, yi, text, va="center", ha="left", fontsize=10, color=INK_2)
    ax.set_xlim(0, top * 1.3)


def segmented_bar(ax, parts: list[tuple[str, float, str, str]], total_label: str) -> None:
    """One whole split into parts: (name, share, color, text color). Shares too small for a label go in the legend."""
    left, handles = 0.0, []
    for name, share, color, text_color in parts:
        ax.barh(0, share, left=left, height=0.6, color=color, edgecolor=SURFACE, linewidth=2)
        inside = share >= 0.12
        if inside:
            ax.text(left + 0.012, 0, f"{share:.1%}", va="center", ha="left", fontsize=12, fontweight="bold", color=text_color)
        handles.append(Patch(color=color, label=name if inside else f"{name}: {share:.1%}"))
        left += share
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.5, 0.5)
    ax.axis("off")
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(0, -0.05), ncol=len(parts), fontsize=10.5, handlelength=1, handleheight=1)
    ax.text(1, 0.42, total_label, transform=ax.transData, ha="right", va="bottom", fontsize=10, color=MUTED)


def boxes(ax, data: pd.DataFrame, column: str, title: str, percent: bool = False) -> None:
    """Stayed (gray) vs left (blue): box = middle 50%, whiskers = 5th-95th percentile, line = median."""
    scale = 100 if percent else 1
    groups = [data.loc[data["left_12m"] == label, column].dropna() * scale for label in (0, 1)]
    plot = ax.boxplot(
        groups, positions=[0, 1], widths=0.42, whis=(5, 95), showfliers=False, patch_artist=True,
        medianprops={"color": INK, "linewidth": 2}, whiskerprops={"color": MUTED}, capprops={"color": MUTED},
    )  # fmt: skip
    for patch, color in zip(plot["boxes"], (GRAY, BLUE)):
        patch.set(facecolor=color, edgecolor=color)
    for x, values in enumerate(groups):
        median = values.median()
        text = f"{median:.1f}%" if percent else f"{median:.0f}"
        ax.text(x + 0.25, median, text, va="center", ha="left", fontsize=10, color=INK_2)
    ax.set_xticks([0, 1], [f"Stayed\n(n={len(groups[0]):,})", f"Left\n(n={len(groups[1]):,})"])
    ax.set_xlim(-0.5, 1.9)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y")
    ax.set_title(title, fontsize=11)


def save(fig, name: str) -> None:
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES_DIR / name)
    plt.close(fig)
    print(f"Wrote {(FIGURES_DIR / name).relative_to(ROOT_DIR)}")


def pct(value: float) -> str:
    return f"{value:.1%}"


def company_labels(d: pd.DataFrame) -> pd.Series:
    """Companies with few rows are folded into one "Other" group named after them."""
    counts = d["company_group"].value_counts()
    small = sorted(counts[counts < MIN_GROUP_ROWS].index)
    return d["company_group"].where(~d["company_group"].isin(small), "Other (" + ", ".join(small) + ")")






# ---------------------------------------------------------------- figures


def fig_funnel(d: pd.DataFrame, local: dict) -> None:
    # URLs whose download failed (HTTP 403) are left out, so the funnel starts from the profiles that could be fetched.
    urls, downloaded, people = len(local["urls"]) - local["failed"], len(local["profiles"]), d["person_id"].nunique()
    stages = [
        ("Usable profile URLs\nfound through Google search", urls),
        ("Profiles downloaded\nfrom LinkedIn", downloaded),
        ("People at a target company\non a reference date", people),
    ]
    texts = [f"{urls:,}"] + [f"{value:,}  ({value / stages[i][1]:.0%} of the step above)" for i, (_, value) in enumerate(stages[1:])]
    fig, ax = plt.subplots(figsize=(10, 3.4), layout="constrained")
    hbar(ax, [s[0] for s in stages], [s[1] for s in stages], texts)
    heading(
        fig,
        "Data collection funnel",
        f"Each of the {people} people adds one row per reference date (every 6 months, 2021-01 to 2025-07)\n"
        "at which they worked at one of the target companies.",
    )
    save(fig, "01_data_funnel.png")




def fig_class_balance(d: pd.DataFrame) -> None:
    n, left = len(d), int(d["left_12m"].sum())
    people_left = d.loc[d["left_12m"] == 1, "person_id"].nunique()
    fig, ax = plt.subplots(figsize=(10, 1.5), layout="constrained")
    segmented_bar(
        ax,
        [
            (f"Stayed for the next 12 months ({n - left:,} rows)", (n - left) / n, GRAY, INK),
            (f"Left within 12 months ({left} rows, {people_left} people)", left / n, BLUE, "#ffffff"),
        ],
        f"{n:,} rows",
    )
    heading(
        fig,
        "Class distribution of left_12m",
        "left_12m = 1 when the person leaves the company within 12 months of the reference date.",
    )
    save(fig, "02_class_balance.png")


def fig_rows_per_person(d: pd.DataFrame) -> None:
    per_person = d.groupby("person_id").size()
    counts = per_person.value_counts().reindex(range(1, per_person.max() + 1), fill_value=0)
    fig, ax = plt.subplots(figsize=(10, 4.2), layout="constrained")
    ax.bar(counts.index, counts.values, width=0.6, color=BLUE)
    peak = counts.idxmax()
    ax.text(peak, counts[peak], f"{counts[peak]} people", ha="center", va="bottom", fontsize=10, color=INK_2)
    ax.set_xticks(counts.index)
    ax.set_xlabel("Rows per person (reference dates at which the person worked at a target company)")
    ax.set_ylabel("People")
    ax.grid(axis="y")
    ax.tick_params(axis="x", length=0)
    heading(
        fig,
        "Number of rows per person",
        f"{len(d):,} rows from {len(per_person)} people; median {per_person.median():.0f} rows per person.",
    )
    save(fig, "03_rows_per_person.png")


def fig_departures_over_time(d: pd.DataFrame) -> None:
    by_t = d.groupby("t").agg(n=("left_12m", "size"), k=("left_12m", "sum"), layoffs=("tech_layoffs_12m_k", "first"))
    rate = by_t["k"] / by_t["n"] * 100
    x = np.arange(len(by_t))
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(8.4, 5.8), sharex=True, layout="constrained")

    top.plot(x, rate, color=BLUE, lw=2, marker="o", ms=8, mec=SURFACE, mew=2, solid_capstyle="round")
    for i in {int(np.argmax(rate)), int(np.argmin(rate)), len(x) - 1}:
        top.annotate(f"{rate.iloc[i]:.1f}%", (x[i], rate.iloc[i]), xytext=(0, 9), textcoords="offset points", ha="center", fontsize=10, color=INK_2)
    top.set_ylim(0, rate.max() * 1.3)
    top.grid(axis="y")
    top.set_title("Rows whose person left within the next 12 months (%)")

    bottom.bar(x, by_t["layoffs"], width=0.5, color=GRAY)
    peak = int(np.argmax(by_t["layoffs"]))
    bottom.annotate(f"{by_t['layoffs'].iloc[peak]:.0f}K", (x[peak], by_t["layoffs"].iloc[peak]), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=10, color=INK_2)
    bottom.grid(axis="y")
    bottom.set_title("Tech layoffs in the 12 months before t (thousands)")
    bottom.set_xticks(x, by_t.index)
    bottom.tick_params(axis="x", length=0)
    bottom.set_xlabel("Reference date t")

    heading(
        fig,
        "Departure rate and tech layoffs by reference date",
        "The departure rate at t looks forward (t to t+11 months); the layoffs look back (t-12 to t-1).",
        f"Between {by_t['n'].min()} and {by_t['n'].max()} rows per reference date.",
    )
    save(fig, "04_departures_over_time.png")


def shares(series: pd.Series, order: list | None = None) -> pd.Series:
    share = series.value_counts(normalize=True, dropna=False)
    return share.reindex([o for o in order if o in share.index]) if order else share.sort_values(ascending=False)


def education_labels(d: pd.DataFrame) -> pd.Series:
    return d["edu_level"].fillna(-1).astype(int).map(EDU_LABELS)


def fig_categorical(d: pd.DataFrame) -> None:
    it = d["edu_it"].map({1: "Technical field", 0: "Other field"}).fillna("No education listed")
    panels = [
        ("Company", shares(company_labels(d))),
        ("Region of the job", shares(d["region"].map(REGION_LABELS))),
        ("Highest finished degree", shares(education_labels(d), [EDU_LABELS[k] for k in (0, 1, 2, 3, -1)])),
        ("Seniority (from the job title)", shares(d["seniority_level"].map(SENIORITY_LABELS), list(SENIORITY_LABELS.values()))),
        ("Job family (from the job title)", shares(d["job_family"].map(JOB_FAMILY_LABELS))),
        ("Field of the finished degree", shares(it, ["Technical field", "Other field", "No education listed"])),
    ]
    slots = max(len(share) for _, share in panels)
    fig, axes = plt.subplots(2, 3, figsize=(11, 7.2), layout="constrained")
    for ax, (title, share) in zip(axes.flat, panels):
        hbar(ax, list(share.index), list(share.values), [pct(v) for v in share.values], slots=slots)
        ax.set_title(title)
    heading(fig, "Distribution of the categorical features", f"Share of the {len(d):,} rows (a person counts once per reference date).")
    save(fig, "05_categorical_distributions.png")




def fig_leave_rate_by_group(d: pd.DataFrame) -> None:
    overall = d["left_12m"].mean() * 100
    seniority = d["seniority_level"].map(lambda s: "Intern / junior" if s <= 1 else SENIORITY_LABELS[s])
    seniority_order = ["Intern / junior"] + [SENIORITY_LABELS[k] for k in range(2, 8)]
    panels = []
    for title, groups, order in (
        ("Seniority", seniority, seniority_order),
        ("Company", company_labels(d), None),
        ("Job family", d["job_family"].map(JOB_FAMILY_LABELS), None),
        ("Region of the job", d["region"].map(REGION_LABELS), None),
    ):
        stats = d.groupby(groups)["left_12m"].agg(["mean", "size"])
        stats = stats[stats["size"] >= MIN_GROUP_ROWS].assign(rate=lambda s: s["mean"] * 100)
        stats = stats.reindex([o for o in order if o in stats.index]) if order else stats.sort_values("rate", ascending=False)
        panels.append((title, stats))
    top = max(stats["rate"].max() for _, stats in panels)

    fig, axes = plt.subplots(2, 2, figsize=(10.5, 7.2), layout="constrained")
    for ax, (title, stats) in zip(axes.flat, panels):
        y = np.arange(len(stats))
        # Compared as printed, so a group shown as "5.0%" is never colored above an average shown as "5.0%".
        above = [round(rate, 1) > round(overall, 1) for rate in stats["rate"]]
        ax.barh(y, stats["rate"], height=0.62, color=[BLUE if a else GRAY for a in above])
        for yi, rate in zip(y, stats["rate"]):
            # A surface-colored box keeps the average line from running through the label.
            ax.text(rate + top * 0.012, yi, f"{rate:.1f}%", va="center", ha="left", fontsize=10, color=INK_2, zorder=4,
                    bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5})  # fmt: skip
        ax.axvline(overall, color=INK_2, lw=1, zorder=3)
        ax.text(overall + top * 0.012, len(stats) - 0.15, f"average {overall:.1f}%", va="center", ha="left", fontsize=9.5, color=INK_2)
        ax.set_yticks(y, [f"{name}  (n={int(n):,})" for name, n in zip(stats.index, stats["size"])])
        ax.set_ylim(len(stats) + 0.2, -0.6)
        ax.set_xlim(0, top * 1.18)  # the same scale in every panel
        ax.tick_params(axis="y", length=0)
        ax.xaxis.set_visible(False)
        ax.spines[["bottom"]].set_visible(False)
        ax.set_title(title)
    heading(
        fig,
        "Departure rate by seniority, company, job family and region",
        "Share of rows in which the person left the company within the next 12 months. "
        f"Blue: above the average of {overall:.1f}%.\nn = number of rows. Groups with fewer than {MIN_GROUP_ROWS} rows are left out.",
    )
    save(fig, "06_leave_rate_by_group.png")


def fig_numeric_by_class(d: pd.DataFrame) -> None:
    panels = [
        ("tenure_company_m", "Tenure at company (months)", False),
        ("tenure_role_m", "Time in current role (months)", False),
        ("months_since_promotion", "Months since last title change", False),
        ("total_experience_m", "Total experience (months)", False),
        ("n_prev_employers", "Previous employers", False),
        ("attrition_12m", "Colleague attrition, 12 months (%)", True),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(10.5, 6.8), layout="constrained")
    for ax, (column, title, percent) in zip(axes.flat, panels):
        boxes(ax, d, column, title, percent)
    heading(fig, "Numeric features by class (stayed vs left)")
    save(fig, "07_numeric_by_class.png")




def fig_missing(d: pd.DataFrame) -> None:
    missing = d.isna().mean()
    missing = missing[missing > 0].sort_values(ascending=False)
    labels = [f"{column}\n{MISSING_REASONS.get(column, '')}" for column in missing.index]
    fig, ax = plt.subplots(figsize=(6.6, 0.6 * len(missing) + 0.8), layout="constrained")
    hbar(ax, labels, list(missing.values), [f"{v:.1%}  ({int(v * len(d))} rows)" for v in missing.values])
    heading(fig, "Missing values by column")
    save(fig, "08_missing_values.png")


def fig_correlations(d: pd.DataFrame) -> None:
    corr = d[CORRELATION_COLUMNS].corr(method="spearman")
    n = len(corr)
    masked = np.where(np.triu(np.ones((n, n), dtype=bool)), np.nan, corr.values)
    fig, ax = plt.subplots(figsize=(8.2, 7.4), layout="constrained")
    image = ax.imshow(masked, cmap=DIVERGING, vmin=-1, vmax=1)
    for i in range(n):
        for j in range(i):
            value = corr.values[i, j]
            if abs(value) >= 0.5 or corr.index[i] == "left_12m":
                sign = "−" if round(value, 2) < 0 else ""
                ax.text(j, i, sign + f"{abs(value):.2f}".lstrip("0"), ha="center", va="center", fontsize=8, color="#ffffff" if abs(value) >= 0.6 else INK)
    ax.set_xticks(range(n), corr.columns, rotation=45, ha="right", rotation_mode="anchor", fontsize=10)
    ax.set_yticks(range(n), corr.index, fontsize=10)
    ax.tick_params(length=0)
    ax.spines[["left", "bottom"]].set_visible(False)
    ax.set_xlim(-0.5, n - 1.5)
    ax.set_ylim(n - 0.5, 0.5)
    colorbar = fig.colorbar(image, ax=ax, shrink=0.6, ticks=[-1, -0.5, 0, 0.5, 1])
    colorbar.outline.set_visible(False)
    colorbar.ax.tick_params(length=0)
    heading(fig, "Spearman correlation between the features")
    save(fig, "09_correlations.png")


def month_start(period: str) -> pd.Timestamp:
    """'2023-05' -> May 2023; '2023-Q2' -> the middle month of the quarter."""
    if "Q" in period:
        year, quarter = period.split("-Q")
        return pd.Timestamp(int(year), int(quarter) * 3 - 1, 1)
    return pd.Timestamp(period + "-01")


def fig_public_series() -> None:
    unemployment = pd.read_csv(RAW_DIR / "eurostat" / "une_rt_m_RO.csv")
    vacancies = pd.read_csv(RAW_DIR / "eurostat" / "jvs_q_nace2_RO_J.csv")
    layoffs = load_layoffs()
    start, end = pd.Timestamp("2019-01-01"), pd.Timestamp("2025-12-31")

    fig, axes = plt.subplots(3, 1, figsize=(6.8, 6.8), sharex=True, layout="constrained")
    for ax, frame, title in (
        (axes[0], unemployment, "Unemployment rate, Romania (%, Eurostat)"),
        (axes[1], vacancies, "ICT job vacancy rate, Romania (%, Eurostat)"),
    ):
        dates = frame["period"].map(month_start)
        keep = (dates >= start) & (dates <= end)
        ax.plot(dates[keep], frame.loc[keep, "value"], color=BLUE, lw=2, marker="o" if ax is axes[1] else None, ms=5, mec=SURFACE, mew=1.5)
        ax.grid(axis="y")
        ax.set_title(title, fontsize=11)
    if layoffs is not None:
        monthly = layoffs.dropna(subset=["month"]).groupby("month")["laid_off"].sum() / 1000
        dates = [pd.Timestamp(int(m) // 12, int(m) % 12 + 1, 1) for m in monthly.index]
        series = pd.Series(monthly.values, index=dates).loc[start:end]
        axes[2].bar(series.index, series.values, width=20, color=BLUE)
        axes[2].grid(axis="y")
        axes[2].set_title("Tech layoffs worldwide (thousands/month, layoffs.fyi)", fontsize=11)
    axes[2].set_xlim(start, end)
    heading(fig, "Public labor market and layoff series")
    save(fig, "10_public_series.png")




# ---------------------------------------------------------------- main


def load_local() -> dict | None:
    """Aggregates from the local (not published) scraping files, or None on a fresh clone."""
    profiles_csv = INTERIM_DIR / "profiles.csv"
    if not (profiles_csv.exists() and PROFILE_URLS_CSV.exists() and SCRAPE_STATUS_JSON.exists()):
        return None
    with SCRAPE_STATUS_JSON.open(encoding="utf-8") as f:
        failed = sum(1 for entry in json.load(f) if entry.get("error"))
    return {
        "urls": pd.read_csv(PROFILE_URLS_CSV, dtype=str, keep_default_na=False),
        "profiles": pd.read_csv(profiles_csv, dtype=str, keep_default_na=False),
        "failed": failed,
    }


def main() -> int:
    if not OUTPUT_CSV.exists():
        print(f"Missing {OUTPUT_CSV.relative_to(ROOT_DIR)}: run build_dataset.py first", file=sys.stderr)
        return 1
    d = pd.read_csv(OUTPUT_CSV)

    local = load_local()
    if local:
        fig_funnel(d, local)
    else:
        print("No local scraping files in data/interim/: skipping figure 01")
    fig_class_balance(d)
    fig_rows_per_person(d)
    fig_departures_over_time(d)
    fig_categorical(d)
    fig_leave_rate_by_group(d)
    fig_numeric_by_class(d)
    fig_missing(d)
    fig_correlations(d)
    fig_public_series()
    return 0


if __name__ == "__main__":
    sys.exit(main())
