"""
qc_runner.py — Quality-control comparison for the Skinny 8872 Explorer.

Mirrors the old app's QC (Comparative Donor Analysis, Tab 2) for 2022, 2023 and
2024, but runs it on the NEW skinny IRS dataset using the NEW shared logic in
explorer_core.py (layered classification + 3-tier donor grouping + affiliated-527
rollup). It then compares the resulting match rates against the OLD app's
published QC results.

Tab-2 settings reproduced exactly from QC_2022/2023/2024.py:
  - Donor type: Non-Individual
  - Date range: full calendar year
  - Group A: DGA + RGA, threshold $0
  - Group B: none
  - Answer key: the human-verified DGA/RGA Discrepancy Report for that year
  - Match metric: |DB - XL| < $1 -> "Yes"; <= $2000 -> "Close match"; else "No"

Usage:  python qc_runner.py
"""

import os
import re
import sys
import glob
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import explorer_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
UPGRADE_ROOT = os.path.dirname(HERE)
PARQUET = os.path.join(HERE, "Prepared Data", "skinny_8872_contributions.parquet")
DONOR_GROUPS = os.path.join(HERE, "Prepared Data", "donor_groups_flat.json")
DISCREP = os.path.join(UPGRADE_ROOT, "Discrepancy Reports")
OLD_QC_DIR = os.path.join(UPGRADE_ROOT, "Old 8872 Explorer QC Results")
OUT_DIR = os.path.join(HERE, "QC Results")

DGA = "Democratic Governors Association (52-1304889)"
RGA = "Republican Governors Association (11-3655877)"
GROUP_A = [DGA, RGA]

YEARS = {
    "2022": os.path.join(DISCREP, "2022 DGA_RGA Discrepancy Report.csv"),
    "2023": os.path.join(DISCREP, "2023 DGA _ RGA Discrepency.xlsx"),
    "2024": os.path.join(DISCREP, "2024 DGA_RGA Discrepancy Report - Final Report.csv"),
}


def parse_dollar(val):
    if pd.isna(val) or str(val).strip() == "":
        return 0.0
    s = str(val).replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def load_answer_key(path):
    """Return DataFrame[Company Name, XL DGA, XL RGA]; skip rows with no dollars."""
    if path.lower().endswith(".xlsx"):
        raw = pd.read_excel(path, sheet_name=0)
    else:
        raw = pd.read_csv(path)
    cols = {c: str(c) for c in raw.columns}
    name_col = next((c for c in raw.columns if str(c).strip() in ("Company Name", "DisplayName")), None)
    dga_col = next((c for c in raw.columns if re.search(r"DGA", str(c)) and "Match" not in str(c)), None)
    rga_col = next((c for c in raw.columns if re.search(r"RGA", str(c)) and "Match" not in str(c)), None)
    rows = []
    for _, r in raw.iterrows():
        name = str(r.get(name_col, "") or "").strip()
        if not name:
            continue
        dga = parse_dollar(r.get(dga_col, 0))
        rga = parse_dollar(r.get(rga_col, 0))
        if dga == 0 and rga == 0:
            continue
        rows.append({"Company Name": name, "XL DGA": dga, "XL RGA": rga})
    return pd.DataFrame(rows)


def run_year_new(year, df, donor_groups_map, addr_idx):
    """Run the new app's Tab-2 logic for one year; return (sheet2_df, summary)."""
    start = pd.Timestamp(f"{year}-01-01")
    end = pd.Timestamp(f"{year}-12-31")
    addr_to_donors, donor_to_addrs, donor_raw_addrs = addr_idx

    filtered = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    filtered = filtered[filtered["donor_type"] == "Non-Individual"]

    unique_donors = filtered["donor"].dropna().unique().tolist()
    clusters = core.build_donor_clusters(
        unique_donors, addr_to_donors, donor_to_addrs, donor_raw_addrs, donor_groups_map)
    filtered["canonical_donor"] = filtered["donor"].map(clusters)

    ga = filtered[filtered["recipient"].isin(GROUP_A)]
    totals = ga.groupby("canonical_donor")["amount"].sum()
    qualifying = set(totals[totals >= 0].index)

    name_counts = (filtered[filtered["canonical_donor"].isin(qualifying)]
                   .groupby(["canonical_donor", "donor"]).size()
                   .reset_index(name="n").sort_values("n", ascending=False)
                   .drop_duplicates("canonical_donor"))
    disp = dict(zip(name_counts["canonical_donor"], name_counts["donor"]))

    rb = (ga[ga["canonical_donor"].isin(qualifying)]
          .groupby(["canonical_donor", "recipient"])["amount"].sum()
          .unstack(fill_value=0).reset_index())
    rb["Donor"] = rb["canonical_donor"].map(disp)
    rb = rb.rename(columns={DGA: "DB DGA", RGA: "DB RGA"})
    for c in ("DB DGA", "DB RGA"):
        if c not in rb.columns:
            rb[c] = 0.0
    qc_donors = rb[["Donor", "DB DGA", "DB RGA"]].copy()
    return qc_donors


def match_against_key(qc_donors, ak_df):
    """Replicate QC Sheet-2 matching (variant exact + prefix fallback)."""
    qc_variant_map = {}
    for _, q in qc_donors.iterrows():
        for v in core.build_variants(q["Donor"]):
            qc_variant_map.setdefault(v, (q["Donor"], q["DB DGA"], q["DB RGA"]))

    rows = []
    for _, ak in ak_df.iterrows():
        ak_name, xl_dga, xl_rga = ak["Company Name"], ak["XL DGA"], ak["XL RGA"]
        matched, db_dga, db_rga = None, 0.0, 0.0
        ak_variants = core.build_variants(ak_name)
        for v in ak_variants:
            if v in qc_variant_map:
                matched, db_dga, db_rga = qc_variant_map[v]
                break
        if matched is None:  # prefix fallback
            best, best_len = None, 0
            for ak_v in ak_variants:
                w = len(ak_v.split())
                if (w >= 2 and len(ak_v) < 10) or (w == 1 and len(ak_v) < 7):
                    continue
                for qc_v, val in qc_variant_map.items():
                    w2 = len(qc_v.split())
                    if (w2 >= 2 and len(qc_v) < 10) or (w2 == 1 and len(qc_v) < 7):
                        continue
                    shorter, longer = (ak_v, qc_v) if len(ak_v) <= len(qc_v) else (qc_v, ak_v)
                    if not longer.startswith(shorter):
                        continue
                    if len(shorter.split()) == 1 and not (longer == shorter or longer.startswith(shorter + " ")):
                        continue
                    if len(shorter) > best_len:
                        best, best_len = val, len(shorter)
                if best:
                    break
            if best:
                matched, db_dga, db_rga = best

        def status(db, xl):
            d = abs(db - xl)
            return "Yes" if d < 1 else ("Close match" if d <= 2000 else "No")

        dga_s, rga_s = status(db_dga, xl_dga), status(db_rga, xl_rga)
        rank = {"Yes": 2, "Close match": 1, "No": 0}
        both = {2: "Yes", 1: "Close match", 0: "No"}[min(rank[dga_s], rank[rga_s])]
        neither = "Yes" if dga_s == "No" and rga_s == "No" else "No"
        rows.append({"Company Name": ak_name, "XL DGA": xl_dga, "DB DGA": db_dga,
                     "XL RGA": xl_rga, "DB RGA": db_rga, "DGA Match": dga_s,
                     "RGA Match": rga_s, "Both Match": both, "Neither Match": neither,
                     "Matched Donor": matched or "(none)"})
    return pd.DataFrame(rows)


def summarize(sheet2):
    n = len(sheet2)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "both_yes": (sheet2["Both Match"] == "Yes").sum(),
        "both_close": (sheet2["Both Match"] == "Close match").sum(),
        "dga_yes": (sheet2["DGA Match"] == "Yes").sum(),
        "rga_yes": (sheet2["RGA Match"] == "Yes").sum(),
        "neither": (sheet2["Neither Match"] == "Yes").sum(),
    }


def old_baseline(year):
    """Count match rates from the OLD app's latest published QC xlsx for the year."""
    files = [f for f in glob.glob(os.path.join(OLD_QC_DIR, f"{year} QC Results", "QC*.xlsx"))
             if "~$" not in f]
    if not files:
        return None, None
    latest = max(files, key=os.path.getmtime)
    am = pd.read_excel(latest, sheet_name="Amount Matching")
    am = am[am["Company Name"].notna()]
    am = am[~am["Company Name"].astype(str).str.startswith(("SUMMARY", "Total", "Both Match",
                                                            "Neither", "DGA Match", "RGA Match"))]
    return summarize(am), os.path.basename(latest)


def pct(x, n):
    return f"{x/n*100:.1f}%" if n else "—"


def main():
    print("Loading skinny parquet + donor groups ...")
    df = pd.read_parquet(PARQUET)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    dg = core.load_donor_groups(DONOR_GROUPS)
    print(f"  {len(df):,} rows, {len(dg):,} donor-group entries")
    print("Building address index ...")
    addr_idx = core.build_address_index(df)

    os.makedirs(OUT_DIR, exist_ok=True)
    results = {}
    for year, ak_path in YEARS.items():
        print(f"\n===== {year} =====")
        ak = load_answer_key(ak_path)
        print(f"  answer key: {len(ak)} companies")
        qc_donors = run_year_new(year, df, dg, addr_idx)
        print(f"  new app non-individual DGA/RGA donors: {len(qc_donors)}")
        sheet2 = match_against_key(qc_donors, ak)
        new_sum = summarize(sheet2)
        old_sum, old_file = old_baseline(year)
        results[year] = (new_sum, old_sum)
        sheet2.to_excel(os.path.join(OUT_DIR, f"QC_{year}_new.xlsx"), index=False)
        print(f"  old baseline file: {old_file}")

    # ---- Comparison readout ----
    print("\n" + "=" * 78)
    print(" QC COMPARISON  —  NEW Skinny app  vs.  OLD app published results")
    print(" Metric = share of answer-key companies whose DGA/RGA $ totals match")
    print("=" * 78)
    header = f"{'Year':<6}{'Metric':<14}{'OLD':>14}{'NEW':>14}{'Δ (pts)':>12}"
    for year in YEARS:
        new_sum, old_sum = results[year]
        n_new, n_old = new_sum["n"], (old_sum["n"] if old_sum else 0)
        print("-" * 78)
        print(f"{year}   answer-key companies: NEW n={n_new}   OLD n={n_old}")
        print(header)
        for label, key in [("Both Match", "both_yes"), ("DGA Match", "dga_yes"),
                           ("RGA Match", "rga_yes"), ("Neither", "neither")]:
            op = pct(old_sum[key], n_old) if old_sum else "—"
            npv = pct(new_sum[key], n_new) if n_new else "—"
            if old_sum and n_new and n_old:
                delta = new_sum[key] / n_new * 100 - old_sum[key] / n_old * 100
                ds = f"{delta:+.1f}"
            else:
                ds = "—"
            print(f"{'':<6}{label:<14}{op:>14}{npv:>14}{ds:>12}")

    print("\nPer-year new QC sheets saved to:", OUT_DIR)
    return results


if __name__ == "__main__":
    main()
