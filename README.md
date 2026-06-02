# Skinny 8872 Contribution Explorer

A campaign-finance search tool for **IRS Form 8872** contribution data, rebuilt
to run reliably on **Streamlit Community Cloud's free tier** without crashing.

This is the **"Skinny"** build: it is restricted to the **12 original recipient
organizations** and ships a single pre-filtered parquet (~24 MB, ~1.14M rows).
It is the stop-gap that keeps a working, non-crashing tool online while the
larger full-dataset hosting question is resolved.

It draws directly from the **IRS POFD** dataset (`8872_contributions.csv.gz`,
the IRS-direct build), which is more accurate and more easily updated than the
old PDF-parsed `contributions.csv`.

---

## What's in this repo

| File | Purpose |
|---|---|
| `app.py` | The Streamlit app (Tab 1: Contribution Search, Tab 2: Comparative Donor Analysis). |
| `explorer_core.py` | All shared, non-Streamlit logic — normalization, classification, 3-tier donor grouping. Imported by **both** the app and the QC harness so they can't drift. |
| `qc_runner.py` | The QC harness. Re-runs the 2022/2023/2024 comparative-analysis QC on the new data and compares to the old app's published results. |
| `Prepared Data/prepare_data.py` | The one-time build script that produces the parquet from the IRS source. |
| `Prepared Data/skinny_8872_contributions.parquet` | The app's data — 12 orgs, deduped, classified, affiliates rolled up. |
| `Prepared Data/donor_groups_flat.json` | Tier-1.5 human-validated donor groupings (from 3 years of QC). |
| `Prepared Data/recipient_summary.parquet` | Per-recipient/year totals. |
| `Prepared Data/build_report.txt` | Diagnostics from the last data build. |

---

## The three problems this build solves

### Problem 1 — Crashing (Option B: pre-filtered parquet)
The old app loaded its entire contributions CSV into RAM, which on the new
8.4M-row dataset would blow past the 1 GB free-tier ceiling. This build
**pre-filters to the 12 target organizations** at build time, producing a
24 MB / ~1.14M-row parquet that loads once (`@st.cache_data`) and is shared
across all sessions. Donor clustering runs only on the **filtered subset** of a
given search, so memory stays low even with a dozen concurrent users.

### Problem 2 — Individual vs. Non-Individual classification
The old app used a brittle "blank employer ⇒ company" rule that misclassified
retirees as companies and corporate in-kind gifts (the CVS case) as
individuals. The IRS POFD rows carry no DIME `contributor.type`, so this build
applies a **layered heuristic** (`explorer_core.classify`) at build time:

1. Organization/legal keyword in the name → **Non-Individual** (catches in-kind
   corporate gifts that carry an employer).
2. Real `occupation` present → **Individual** (orgs file `occupation = NA`).
3. Real `employer` present (not `NA`/`SELF`/retired markers) → **Individual**.
4. Personal name structure (comma `LAST, FIRST`, or a suffix like `Jr`/`MD`) → **Individual**.
5. No signal → **Non-Individual**.

`CVS Health` → Non-Individual; `Aaron E. Jabbour / Retired` → Individual;
`Aetna` → Non-Individual. ✔

### Problem 3 — Merging Bonica CIDs with human intelligence
A **three-tier donor-grouping hierarchy** (`explorer_core.build_donor_clusters`),
human intelligence first:

* **Tier 1 — `NAME_ALIAS_GROUPS`** (243 curated alias groups). Highest trust.
* **Tier 1.5 — `donor_groups.json`** (85k QC-validated groupings from 2022–2024).
* **Tier 3 — fuzzy / prefix / address matching** (the old 5-strategy algorithm).

> **Tier 2 (Bonica CID grouping) is not active.** The IRS POFD build that
> produced `8872_contributions.csv.gz` carries **no `bonica.cid` column**, so
> there is nothing to group on. The recommendations doc assumed a DIME-merged
> source; this Skinny build uses the IRS-direct source, so Tier 2 is a no-op and
> Tiers 1, 1.5 and 3 do the work. If a future build joins DIME CIDs in, Tier 2
> can be added without touching Tiers 1/1.5/3.

### Bonus — Affiliated-527 rollup
Per `Party Key.xlsx` ("Affiliated 527 Group"), contributions to affiliated
groups are **counted under the parent org**. Ten affiliated groups whose parent
is one of the 12 are rolled up at build time, e.g.:

* *DAGA People's Lawyer Project* & *Committee for Justice and Fairness* → **DAGA**
* *Put Michigan First*, *Alliance for Common Sense*, *A Stronger Michigan/Wisconsin* → **DGA**
* *Good Government Coalition* → **GOPAC**; *American Leadership Committee* → **DLCC**; etc.

(*Emerge California* is excluded — its parent *Emerge America* is not one of the 12.)

### Data-quality fix — overlapping-filing de-duplication
The IRS source reports the same contribution in multiple filings whose periods
overlap/nest (e.g. a Q3 *Jul–Sep* report **and** an H2 *Jul–Dec* report that both
list the same gift). The upstream builder de-dupes only by exact period, so
nested periods double-count. `prepare_data.py` fixes this by dropping filings
whose period is strictly contained in a larger filing for the same org, plus a
cross-filing safety net — matching the database's documented rule ("same donor,
same amount, same day"). Verified: Centene 2023 → \$1.21M, Molina 2023 → \$775K,
both exactly matching the human answer key.

---

## QC results — new vs. old (share of answer-key companies whose DGA/RGA \$ match)

| Year | Metric | OLD app | NEW Skinny | Δ |
|------|--------|--------:|-----------:|---:|
| 2022 | Both Match | 52.4% | **62.5%** | **+10.2** |
| 2022 | DGA Match  | 75.9% | **86.4%** | **+10.5** |
| 2022 | RGA Match  | 74.2% | 74.3% | +0.0 |
| 2023 | Both Match | 77.2% | 76.8% | −0.4 |
| 2023 | DGA Match  | 85.2% | 84.9% | −0.3 |
| 2023 | RGA Match  | 90.2% | 90.0% | −0.2 |
| 2024 | Both Match | 76.9% | 76.8% | −0.1 |
| 2024 | DGA Match  | 84.2% | 84.4% | +0.1 |
| 2024 | RGA Match  | 90.9% | 90.7% | −0.2 |

**2022 improves materially; 2023 and 2024 match within ≈0.5 pts** — comfortably
inside the ±3% bar, using the new (more accurate) IRS-direct data.

Reproduce with `python qc_runner.py` (writes per-year sheets to `QC Results/`).

---

## Running it

### Locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
Default password: `$h0w-me-the-m0n3y` (override via `.streamlit/secrets.toml` →
`APP_PASSWORD`, or the Streamlit Cloud Secrets box).

### On Streamlit Community Cloud
Point a new app at this repo, branch `main`, main file `app.py`. The parquet is
committed, so no build step runs on deploy. Optionally set `APP_PASSWORD` in the
app's Secrets.

### Rebuilding the data
`prepare_data.py` expects the IRS source alongside the original project layout
(`../../Sanitized Database/8872_contributions.csv.gz`) and is **not** run on
deploy — only locally when the IRS data refreshes:
```bash
python "Prepared Data/prepare_data.py"
```
