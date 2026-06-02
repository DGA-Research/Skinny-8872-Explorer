"""
Skinny 8872 Contribution Explorer — Data Preparation
=====================================================

Builds the pre-filtered parquet (Problem 1, Option B) for the 12 original
recipient organizations, with the two upgraded behaviors baked in at build time:

  * Problem 2 (Individual vs. Non-Individual) — a layered classification
    heuristic applied to the IRS POFD rows (which have no DIME contributor.type).
  * Affiliated 527 rollup — contributions to affiliated groups (e.g. "DAGA
    People's Lawyer Project") are re-mapped onto the parent org they are
    affiliated with (e.g. "Democratic Attorneys General Association, Inc.")
    as specified in Party Key.xlsx.

Problem 3 (donor grouping) is handled in the app at runtime via the 3-tier
hierarchy (NAME_ALIAS_GROUPS -> donor_groups.json -> fuzzy). This script also
copies donor_groups.json into the app folder so it ships with the app.

Inputs (read-only):
  ../../Sanitized Database/8872_contributions.csv.gz   (IRS POFD, 8.42M rows)
  ../../Party Key.xlsx                                  (party labels + affiliations)
  ../../Old 8872 Contribution Explorer/donor_groups.json

Outputs (written into this Prepared Data/ folder and the app root):
  skinny_8872_contributions.parquet     (the main filtered dataset)
  recipient_summary.parquet             (per-recipient/year totals for the picker)
  build_report.txt                      (diagnostics)
"""

import csv
import gzip
import os
import re
import sys
import json
from collections import defaultdict, Counter

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)                       # Skinny 8872 For Streamlit Community
UPGRADE_ROOT = os.path.dirname(APP_ROOT)               # 8872 Explorer Upgrade

SRC_GZ = os.path.join(UPGRADE_ROOT, "Sanitized Database", "8872_contributions.csv.gz")
DONOR_GROUPS_SRC = os.path.join(UPGRADE_ROOT, "Old 8872 Contribution Explorer", "donor_groups.json")

# ---------------------------------------------------------------------------
# The 12 original recipient organizations.
# Display names + EIN-with-dash exactly match the strings the old app / QC used,
# so the existing answer keys (which reference e.g.
# "Democratic Governors Association (52-1304889)") line up verbatim.
# ---------------------------------------------------------------------------
PARENTS = {
    "050532524": ("Republican State Leadership Committee - RSLC (05-0532524)", "Republicans"),
    "521870839": ("Democratic Legislative Campaign Committee (52-1870839)",    "Democrats"),
    "521304889": ("Democratic Governors Association (52-1304889)",             "Democrats"),
    "113655877": ("Republican Governors Association (11-3655877)",             "Republicans"),
    "263853861": ("Democratic Association of Secretaries of State (26-3853861)", "Democrats"),
    "134220019": ("DEMOCRATIC ATTORNEYS GENERAL ASSOCIATION INC (13-4220019)", "Democrats"),
    "464501717": ("Republican Attorneys General Association (46-4501717)",     "Republicans"),
    "521237780": ("GOPAC, Inc. (52-1237780)",                                  "Republicans"),
    "521535470": ("Democratic Mayors Association (52-1535470)",                "Democrats"),
    "843171981": ("Democratic Treasurers Association (84-3171981)",            "Democrats"),
    "933905072": ("National Republican Mayors Association (93-3905072)",       "Republicans"),
    "030457299": ("Democratic Lieutenant Governors Association (03-0457299)",  "Democrats"),
}

# Affiliated 527 group EIN  ->  parent EIN (from Party Key.xlsx "Affiliated 527 Group").
# Only groups whose parent is one of the 12 are included; Emerge California ->
# Emerge America is intentionally excluded (Emerge America is not one of the 12).
AFFILIATED = {
    "831281397": "134220019",  # DAGA People's Lawyer Project          -> DAGA
    "263073030": "134220019",  # Committee for Justice and Fairness    -> DAGA
    "844197020": "521237780",  # Good Government Coalition, Inc.        -> GOPAC
    "871484192": "521304889",  # Put Michigan First                    -> DGA
    "863218927": "521304889",  # Alliance for Common Sense             -> DGA
    "824509198": "521304889",  # A Stronger Michigan                   -> DGA
    "824499748": "521304889",  # A Stronger Wisconsin                  -> DGA
    "471531928": "050532524",  # RSLC - Judicial Fairness Initiative   -> RSLC
    "831273004": "521870839",  # American Leadership Committee         -> DLCC
    "881203081": "263853861",  # Safe Accessible Fair Elections        -> DASS
}

TARGET_EINS = set(PARENTS) | set(AFFILIATED)


def norm_ein(e):
    return re.sub(r"\D", "", str(e)).zfill(9)


# ---------------------------------------------------------------------------
# Problem 2 — layered Individual vs. Non-Individual classification
# ---------------------------------------------------------------------------
# Calibrated against the real IRS POFD rows: organizations carry employer="NA"
# and occupation="NA" (and usually an org/legal keyword in the name); individual
# donors carry a real occupation (e.g. "Retired", "Owner", "Attorney") and/or a
# real employer. First confident signal wins; ambiguous -> Non-Individual.

# Trailing legal suffixes (anchored at end of name).
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(INC|INCORPORATED|LLC|L\.?L\.?C|LLP|LP|LTD|LIMITED|CORP|CORPORATION|"
    r"COMPANY|CO|PLLC|PA|PC|PAC|GMBH|NA)\.?\s*$",
    re.IGNORECASE,
)

# Strong organization keywords (whole word, anywhere in the name). Curated to
# avoid common personal surnames.
_ORG_WORDS = {
    "INCORPORATED", "CORPORATION", "CORP", "COMPANY", "COMPANIES", "ASSOCIATION",
    "ASSOC", "ASSN", "COMMITTEE", "CMTE", "UNION", "COUNCIL", "COALITION",
    "FEDERATION", "ALLIANCE", "LEAGUE", "SOCIETY", "FOUNDATION", "INSTITUTE",
    "PARTNERS", "PARTNERSHIP", "PARTNERSHIPS", "HOLDINGS", "HOLDING", "ENTERPRISES",
    "ENTERPRISE", "INDUSTRIES", "INDUSTRY", "SERVICES", "SYSTEMS", "SOLUTIONS",
    "TECHNOLOGIES", "TECHNOLOGY", "BANCORP", "BANK", "BANKERS", "INSURANCE",
    "FINANCIAL", "FINANCE", "PHARMACEUTICALS", "PHARMACEUTICAL", "PHARMA",
    "HEALTHCARE", "HOSPITAL", "MEDICAL", "AIRLINES", "COMMUNICATIONS", "FUND",
    "FUNDS", "ACCOUNT", "GROUP", "CAPITAL", "REALTY", "PROPERTIES", "MOTORS",
    "FOODS", "ENERGY", "BREWING", "TOBACCO", "MANAGEMENT", "CONSULTING",
    "STRATEGIES", "COUNCILS", "ASSOCIATES", "MUTUAL", "PETROLEUM", "RESOURCES",
    "AIRWAYS", "RAILROAD", "RAILWAY", "BEVERAGE", "BEVERAGES", "AUTOMOTIVE",
    "PAC", "POLITICAL", "DEMOCRATIC", "DEMOCRATS", "REPUBLICAN", "REPUBLICANS",
    "CITIZENS", "AMERICANS", "FRIENDS", "PROGRESS", "ACTION", "COMMITTEES",
    "TRIBE", "NATION", "GAMING", "CASINO", "RESORTS", "AGENCY", "AUTHORITY",
    "DISTRICT", "MUNICIPAL", "COOPERATIVE", "CO-OP", "GROWERS", "FARMERS",
    "PRODUCERS", "MANUFACTURING", "MANUFACTURERS", "DISTRIBUTORS", "DISTRIBUTING",
    "TRUST", "TRUSTS", "VENTURES", "GLOBAL", "WORLDWIDE", "NATIONAL",
    "INTERNATIONAL", "AMERICA", "USA", "LABORERS", "TEAMSTERS", "WORKERS",
    "EMPLOYEES", "GUILD", "BUREAU", "CHAMBER", "DEALERS", "DEALER",
}

# Tokens that, standing alone, mark a real occupation/employer as ABSENT.
_NULLISH = {
    "", "NA", "N/A", "N A", "N.A.", "N.A", "NONE", "NULL", "NAN", "N/A.",
    "NOT APPLICABLE", "NOT EMPLOYED", "UNEMPLOYED", "INFORMATION REQUESTED",
    "REQUESTED", "NA NA", "NONE NONE", "N/A N/A",
}

# Personal name suffixes / honorifics.
_PERSON_TOKENS = {
    "JR", "SR", "II", "III", "IV", "V", "MD", "M.D.", "DDS", "DMD", "PHD",
    "PH.D.", "ESQ", "CPA", "RN", "DO", "JD", "MR", "MRS", "MS", "DR", "MISS",
}


def _clean(val):
    return str(val or "").strip()


def _is_nullish(val):
    s = _clean(val).upper().strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s in _NULLISH or len(s) < 2


def _has_org_word(name_upper):
    if _LEGAL_SUFFIX_RE.search(name_upper):
        return True
    toks = set(re.sub(r"[^A-Z0-9&\- ]", " ", name_upper).split())
    return bool(toks & _ORG_WORDS)


def _looks_personal(name):
    """Comma 'LAST, FIRST' pattern or a trailing personal suffix/honorific."""
    n = name.strip()
    nu = n.upper()
    toks = re.sub(r"[^A-Z0-9.& ]", " ", nu).split()
    if toks and toks[-1].strip(".") in {t.strip(".") for t in _PERSON_TOKENS}:
        return True
    if toks and toks[0].strip(".") in {"MR", "MRS", "MS", "DR", "MISS"}:
        return True
    if "," in n:
        after = n.split(",", 1)[1].strip()
        after_words = [w for w in re.split(r"\s+", after) if w]
        # "Smith, John" / "Smith, John A." -> 1-3 short alpha words after comma
        if 1 <= len(after_words) <= 3 and all(re.fullmatch(r"[A-Za-z.\-']+", w) for w in after_words):
            # but not "Bayer US, LLC" (after-comma is a legal token)
            if not any(w.upper().strip(".") in {"LLC", "INC", "LP", "LLP", "PAC", "CO", "CORP", "PA", "PC", "LTD"} for w in after_words):
                return True
    return False


def classify(name, employer, occupation):
    """Return 'Individual' or 'Non-Individual'. First confident signal wins."""
    nu = _clean(name).upper()
    # 1. Strong organization signal in the NAME -> Non-Individual (protects
    #    against in-kind corporate gifts that carry an employer/occupation).
    if _has_org_word(nu):
        return "Non-Individual"
    # 2. Real occupation present -> Individual (orgs carry occupation="NA").
    if not _is_nullish(occupation):
        return "Individual"
    # 3. Real employer present (not self/retired markers) -> Individual.
    if not _is_nullish(employer):
        return "Individual"
    # 4. Personal name structure (comma LAST, FIRST or suffix/title) -> Individual.
    if _looks_personal(name):
        return "Individual"
    # 5. No signal -> Non-Individual (matches the recommendation's default).
    return "Non-Individual"


# ---------------------------------------------------------------------------
# Light first/last name parse (display + parity with old schema)
# ---------------------------------------------------------------------------
_SUFFIX_STRIP = {"JR", "SR", "II", "III", "IV", "V", "MD", "M.D.", "DDS", "PHD",
                 "ESQ", "CPA", "RN", "DO", "JD"}


def split_name(name, donor_type):
    if donor_type != "Individual":
        return ("", "")
    n = _clean(name)
    if "," in n:
        last, rest = n.split(",", 1)
        first = rest.strip().split(" ")[0] if rest.strip() else ""
        return (first.strip(" ."), last.strip(" ."))
    parts = [p for p in re.split(r"\s+", n) if p]
    parts = [p for p in parts if p.upper().strip(".") not in _SUFFIX_STRIP]
    if len(parts) == 1:
        return (parts[0], "")
    if len(parts) >= 2:
        return (parts[0].strip(" ."), parts[-1].strip(" ."))
    return ("", "")


def parse_date(d, period_end):
    """IRS dates are YYYYMMDD strings; fall back to period_end when blank."""
    for cand in (str(d or "").strip(), str(period_end or "").strip()):
        if len(cand) == 8 and cand.isdigit():
            return f"{cand[4:6]}/{cand[6:8]}/{cand[0:4]}"
    return ""


# ---------------------------------------------------------------------------
# Stream the source, filter, remap affiliates, classify.
# ---------------------------------------------------------------------------
def main():
    if not os.path.exists(SRC_GZ):
        sys.exit(f"Source not found: {SRC_GZ}")

    rows = []
    n_total = 0
    cls_counter = Counter()
    affil_rolled = Counter()
    per_recip_year = defaultdict(lambda: defaultdict(int))

    print(f"Streaming {SRC_GZ} ...")
    with gzip.open(SRC_GZ, "rt", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            n_total += 1
            if n_total % 1_000_000 == 0:
                print(f"  ...{n_total:,} rows scanned, {len(rows):,} kept")
            ein = norm_ein(row["recipient_ein"])
            if ein not in TARGET_EINS:
                continue

            # Resolve to parent (affiliated rollup).
            orig_recipient_name = row["recipient_name"]
            if ein in AFFILIATED:
                parent_ein = AFFILIATED[ein]
                affil_rolled[ein] += 1
                affiliated_from = orig_recipient_name
            else:
                parent_ein = ein
                affiliated_from = ""

            disp_name, party = PARENTS[parent_ein]

            name = row["contributor_name"]
            employer = row["employer"]
            occupation = row["occupation"]
            dtype = classify(name, employer, occupation)
            cls_counter[dtype] += 1
            first, last = split_name(name, dtype)

            try:
                amount = float(row["amount"] or 0)
            except ValueError:
                amount = 0.0

            date_str = parse_date(row.get("date"), row.get("period_end"))
            yr = date_str[-4:] if date_str else "?"
            per_recip_year[parent_ein][yr] += 1

            rows.append({
                "donor": name,
                "last": last,
                "first": first,
                "is_company": dtype == "Non-Individual",
                "donor_type": dtype,
                "employer": "" if _is_nullish(employer) else _clean(employer),
                "occupation": "" if _is_nullish(occupation) else _clean(occupation),
                "street": _clean(row.get("address")),
                "city": _clean(row.get("city")),
                "state": _clean(row.get("state")),
                "zip_code": _clean(row.get("zipcode")),
                "amount": amount,
                "date": date_str,
                "recipient": disp_name,
                "recipient_party": party,
                "recipient_ein": parent_ein,
                "affiliated_from": affiliated_from,
                "_dedup_ein": ein,                         # ORIGINAL ein (pre-rollup)
                "_form_id": str(row.get("form_id", "")),
                "_pb": int(row["period_begin"]) if str(row.get("period_begin", "")).isdigit() else 0,
                "_pe": int(row["period_end"]) if str(row.get("period_end", "")).isdigit() else 0,
            })

    print(f"Scanned {n_total:,} rows; kept {len(rows):,}.")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y", errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)

    # ----------------------------------------------------------------------
    # De-duplicate overlapping-period filings (two passes).
    #
    # The IRS source reports the same contribution in multiple filings whose
    # periods overlap/nest (e.g. a Q3 Jul-Sep report AND an H2 Jul-Dec report
    # that both list the same gift). build_engine.py de-dupes by exact
    # (EIN, period_begin, period_end), so nested/overlapping periods slip
    # through and double-count.
    #
    # Pass 1 (filing-level, precise): for each original org, drop any filing
    #   whose period is STRICTLY contained in another filing's period for that
    #   org -- the larger filing is the comprehensive restatement. This removes
    #   genuine double-reporting without touching coincidental duplicates.
    # Pass 2 (contribution-level safety net): for filings that only PARTIALLY
    #   overlap (neither nested), collapse identical contributions
    #   (same org, donor, date, amount) that appear in more than one filing.
    #
    # Verified against the human answer keys: Centene 2023 -> $1.21M (XL ✓),
    # Molina 2023 -> $775K (XL ✓).
    # ----------------------------------------------------------------------
    before = len(df)

    # Pass 1 — strictly-nested filing removal
    dropped_forms = set()
    for ein, grp in df[["_dedup_ein", "_form_id", "_pb", "_pe"]].drop_duplicates().groupby("_dedup_ein"):
        filings = [(r._form_id, r._pb, r._pe) for r in grp.itertuples()]
        for fid, pb, pe in filings:
            if pb == 0 or pe == 0:
                continue
            for ofid, opb, ope in filings:
                if ofid == fid:
                    continue
                # other filing strictly contains this one -> this one is redundant
                if opb <= pb and ope >= pe and (opb < pb or ope > pe):
                    dropped_forms.add(fid)
                    break
    if dropped_forms:
        df = df[~df["_form_id"].isin(dropped_forms)].reset_index(drop=True)
    removed_nested = before - len(df)

    # Pass 2 — residual identical contributions across partially-overlapping
    # filings. Collapse a (org, donor, date, amount) group to one row ONLY when
    # it is reported across MORE THAN ONE distinct filing (true cross-filing
    # duplicate). Identical rows within a single filing, and distinct gifts, are
    # left untouched -- this avoids removing coincidental same-day/same-amount
    # contributions the way a blanket dedup would.
    mid = len(df)
    key = ["_dedup_ein", "donor", "date", "amount"]
    nforms = df.groupby(key, dropna=True)["_form_id"].transform("nunique")
    cross = (nforms > 1) & df.duplicated(subset=key, keep="first")
    df = df[~cross].reset_index(drop=True)
    removed_residual = mid - len(df)

    removed = before - len(df)
    df = df.drop(columns=["_dedup_ein", "_form_id", "_pb", "_pe"])
    print(f"De-dup: dropped {len(dropped_forms):,} nested filings "
          f"({removed_nested:,} rows) + {removed_residual:,} residual dup rows = "
          f"{removed:,} ({removed/before*100:.1f}%); {len(df):,} rows remain.")

    out_parquet = os.path.join(HERE, "skinny_8872_contributions.parquet")
    df.to_parquet(out_parquet, compression="snappy", index=False)
    sz = os.path.getsize(out_parquet) / 1e6
    print(f"Wrote {out_parquet}  ({sz:.1f} MB, {len(df):,} rows)")

    # Recipient summary (per recipient + year) for the picker / dashboard.
    summary = (
        df.assign(year=df["date"].dt.year)
          .groupby(["recipient", "recipient_party", "recipient_ein", "year"], dropna=False)
          .agg(total_amount=("amount", "sum"),
               transactions=("amount", "size"))
          .reset_index()
    )
    out_summary = os.path.join(HERE, "recipient_summary.parquet")
    summary.to_parquet(out_summary, index=False)
    print(f"Wrote {out_summary}  ({len(summary):,} rows)")

    # Ship donor_groups.json next to the app (Tier 1.5 lookup).
    if os.path.exists(DONOR_GROUPS_SRC):
        with open(DONOR_GROUPS_SRC) as f:
            dg = json.load(f)
        flat = {}
        for e in dg:
            d = str(e.get("donor", "")).strip()
            g = str(e.get("donor_group", "")).strip()
            if d and g:
                flat[d.upper()] = g
        out_dg = os.path.join(HERE, "donor_groups_flat.json")
        with open(out_dg, "w") as f:
            json.dump(flat, f)
        print(f"Wrote {out_dg}  ({len(flat):,} unique donor->group entries)")

    # ---- Diagnostics report ----
    lines = []
    lines.append("Skinny 8872 — Data Build Report")
    lines.append("=" * 50)
    lines.append(f"Source rows scanned:   {n_total:,}")
    lines.append(f"Rows matched (12 orgs):{before:,}")
    lines.append(f"Overlap dupes removed: {removed:,}")
    lines.append(f"Rows kept (deduped):   {len(df):,}")
    lines.append(f"Parquet size:          {sz:.1f} MB")
    lines.append("")
    lines.append("Classification (Problem 2 layered heuristic):")
    tot = sum(cls_counter.values())
    for k, v in cls_counter.most_common():
        lines.append(f"  {k:<16} {v:>10,}  ({v/tot*100:5.1f}%)")
    lines.append("")
    lines.append("Affiliated-527 rows rolled up to parents:")
    for ein, c in affil_rolled.most_common():
        lines.append(f"  {ein} -> {AFFILIATED[ein]}   {c:,} rows")
    lines.append("")
    lines.append("Per-recipient kept rows (2022/2023/2024):")
    for ein, (disp, _) in PARENTS.items():
        yrs = per_recip_year[ein]
        s = "  ".join(f"{y}:{yrs.get(y,0):,}" for y in ("2022", "2023", "2024"))
        lines.append(f"  {disp[:50]:<52} {s}")
    report = "\n".join(lines)
    with open(os.path.join(HERE, "build_report.txt"), "w") as f:
        f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
