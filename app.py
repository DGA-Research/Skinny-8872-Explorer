"""
Skinny 8872 Contribution Explorer — Streamlit App
=================================================

Campaign-finance search tool for IRS Form 8872 contribution data, restricted to
the 12 original recipient organizations ("Skinny" build) so the whole thing fits
comfortably in Streamlit Community Cloud's free tier without crashing.

Upgrades vs. the old app (see README.md):
  * Problem 1 (crashes): loads a 24 MB pre-filtered parquet (~1.14M rows) once,
    shared across sessions via @st.cache_data -- no multi-GB in-RAM dataset.
  * Problem 2 (individual vs. non-individual): layered classification baked into
    the data at build time (explorer_core.classify).
  * Problem 3 (donor grouping): 3-tier hierarchy in explorer_core.build_donor_clusters
    (NAME_ALIAS_GROUPS -> donor_groups.json -> fuzzy/address).
  * Affiliated-527 rollup: contributions to affiliated groups are counted under
    the parent org (baked into the parquet).

All matching/grouping logic lives in explorer_core.py and is shared with the QC
harness (qc_runner.py), so what the QC validated is exactly what the app runs.
"""

import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from rapidfuzz import fuzz

import explorer_core as core
from explorer_core import (
    normalize_donor, normalize_donor_no_amp, expand_abbrevs, contract_abbrevs,
    strip_legal, strip_geo, no_the, build_variants, make_acronym,
    extract_search_names, addresses_compatible, NAME_ALIAS_GROUPS,
    _alias_lookup, _alias_group_canonicals,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PARQUET = os.path.join(HERE, "Prepared Data", "skinny_8872_contributions.parquet")
DONOR_GROUPS_PATH = os.path.join(HERE, "Prepared Data", "donor_groups_flat.json")

st.set_page_config(
    page_title="Skinny 8872 Contribution Explorer",
    page_icon="\U0001F3DB\uFE0F",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="8872 Contribution Explorer",
    page_icon="🏛️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------
try:
    APP_PASSWORD = st.secrets.get("APP_PASSWORD", "$h0w-me-the-m0n3y")
except Exception:
    # No secrets.toml present (e.g. local run) — fall back to the default.
    APP_PASSWORD = "$h0w-me-the-m0n3y"

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🏛️ 8872 Contribution Explorer")
    st.markdown("Please enter the password to access this tool.")
    pwd = st.text_input("Password", type="password")
    if st.button("Login", type="primary"):
        if pwd == APP_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()



# ---------------------------------------------------------------------------
# Load & cache the database (Problem 1 — pre-filtered parquet, loaded once)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading contribution database\u2026")
def load_data():
    if not os.path.exists(PARQUET):
        return pd.DataFrame()
    df = pd.read_parquet(PARQUET)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    # donor_type / is_company are pre-computed at build time (Problem 2).
    return df


@st.cache_resource(show_spinner=False)
def get_donor_groups():
    """Tier 1.5 human-validated donor groupings (Problem 3)."""
    return core.load_donor_groups(DONOR_GROUPS_PATH)


@st.cache_data(show_spinner=False)
def get_unique_donors(_df):
    return sorted(_df["donor"].dropna().unique().tolist())


@st.cache_data(show_spinner="Building address index\u2026")
def build_address_index(_df):
    return core.build_address_index(_df)


@st.cache_data(show_spinner="Building search index…")
def build_donor_index(_donor_list):
    """Pre-compute normalized variants and stripped forms for all donors."""
    variants = {}
    stripped = {}
    for donor in _donor_list:
        variants[donor] = build_variants(donor)
        stripped[donor] = (
            strip_legal(normalize_donor(donor)),
            strip_legal(expand_abbrevs(normalize_donor(donor))),
            strip_legal(normalize_donor_no_amp(donor)),
        )
    return variants, stripped



@st.cache_data(show_spinner="Building donor clusters\u2026")
def build_donor_clusters(_unique_donors, _addr_to_donors, _donor_to_addrs, _donor_raw_addrs):
    """3-tier clustering (Problem 3), shared with the QC harness via explorer_core."""
    return core.build_donor_clusters(
        _unique_donors, _addr_to_donors, _donor_to_addrs, _donor_raw_addrs,
        get_donor_groups())


df = load_data()

if df.empty:
    st.error(
        "No contribution data found. Expected the pre-built parquet at "
        "Prepared Data/skinny_8872_contributions.parquet. Run "
        "`python 'Prepared Data/prepare_data.py'` to build it."
    )
    st.stop()

unique_donors = get_unique_donors(df)
addr_to_donors, donor_to_addrs, donor_raw_addrs = build_address_index(df)
donor_variants_cache, donor_stripped_cache = build_donor_index(unique_donors)



# ---------------------------------------------------------------------------
# Fuzzy donor search (Tab 1)
# ---------------------------------------------------------------------------
def fuzzy_find_donors(search_term, donor_list):
    """Find donor names matching the search term.

    Uses normalized variants, prefix matching, acronym detection,
    fuzzy matching, geo stripping fallback, and address confidence signals.
    Returns list of (donor_name, score, match_type) sorted by score descending.
    """
    if not search_term.strip():
        return []

    # Extract search names (handles parentheticals and slashes)
    search_names = extract_search_names(search_term)

    results = []
    seen = set()

    for term in search_names:
        # Build search term variants and normalized forms
        s_vars = build_variants(term)
        s_nl = strip_legal(normalize_donor(term))
        s_exp_nl = strip_legal(expand_abbrevs(normalize_donor(term)))
        s_nl2 = strip_legal(normalize_donor_no_amp(term))
        term_upper = term.strip().upper()
        term_acronym = make_acronym(term_upper)

        # 0. Alias group matching — find all donors that belong to the same
        #    alias group as the search term and add them as "alias match"
        matched_gids = set()
        for v in s_vars:
            gid = _alias_lookup.get(v)
            if gid is not None:
                matched_gids.add(gid)
        if matched_gids:
            alias_canonicals = set()
            for gid in matched_gids:
                alias_canonicals |= _alias_group_canonicals[gid]
            for donor in donor_list:
                if donor in seen:
                    continue
                d_vars = donor_variants_cache.get(donor, set())
                if alias_canonicals & d_vars:
                    results.append((donor, 99, "alias match"))
                    seen.add(donor)

        for donor in donor_list:
            if donor in seen:
                continue

            d_vars = donor_variants_cache.get(donor, set())
            best_score = 0
            match_type = ""

            # 1. Exact variant match (highest priority)
            if s_vars & d_vars:
                best_score = 100
                match_type = "exact match"

            # 2. Prefix match
            if best_score < 96:
                for sv in s_vars:
                    if len(sv) < 2:
                        continue
                    for dv in d_vars:
                        if len(dv) < 2:
                            continue
                        shorter, longer = (sv, dv) if len(sv) <= len(dv) else (dv, sv)
                        if longer.startswith(shorter):
                            n_words = len(shorter.split())
                            # Multi-word: require 2+ words and 6+ chars
                            # Single-word short (< 6 chars): must match as a
                            # complete first word (e.g. "CVS" matches "CVS PHARMACY"
                            # but not inside "MCVS CORP")
                            if n_words >= 2 and len(shorter) >= 6:
                                best_score = 96
                                match_type = "prefix match"
                                break
                            elif n_words == 1 and len(shorter) >= 6:
                                best_score = 96
                                match_type = "prefix match"
                                break
                            elif n_words == 1 and len(shorter) >= 2:
                                # Short single word — only match if it's a
                                # complete word at the start of the longer name
                                if longer == shorter or longer.startswith(shorter + " "):
                                    best_score = 96
                                    match_type = "prefix match"
                                    break
                    if best_score >= 96:
                        break

            # 3. Acronym matching
            if best_score < 95:
                donor_upper = donor.upper()
                donor_acronym = make_acronym(donor_upper)
                term_norm_simple = re.sub(r"[.\-,']", "", term_upper)
                term_norm_simple = re.sub(r"\s+", " ", term_norm_simple).strip()
                # Search term IS an acronym matching donor's acronym
                if len(term_norm_simple) >= 2 and donor_acronym and term_norm_simple == donor_acronym:
                    best_score = max(best_score, 95)
                    match_type = "acronym match"
                # Search term is a full name, donor is its acronym
                if term_acronym and len(term_acronym) >= 2:
                    donor_norm_simple = re.sub(r"[.\-,']", "", donor_upper)
                    donor_norm_simple = re.sub(r"\s+", " ", donor_norm_simple).strip()
                    if len(donor_norm_simple.split()) == 1 and donor_norm_simple == term_acronym:
                        best_score = max(best_score, 93)
                        match_type = "acronym match"
                    if donor_acronym == term_acronym and len(term_acronym) >= 3:
                        best_score = max(best_score, 90)
                        match_type = "acronym match"

            # 4. Fuzzy matching on normalized/expanded/stripped forms
            if best_score < 72:
                d_nl, d_exp_nl, d_nl2 = donor_stripped_cache.get(donor, ('', '', ''))
                best_fuzzy = 0
                for sv in [s_nl, s_exp_nl, s_nl2]:
                    if not sv or len(sv) < 2:
                        continue
                    for dv in [d_nl, d_exp_nl, d_nl2]:
                        if not dv or len(dv) < 2:
                            continue
                        ratio = fuzz.ratio(sv, dv)
                        partial = fuzz.partial_ratio(sv, dv)
                        score = max(ratio, partial)
                        if score > best_fuzzy:
                            best_fuzzy = score
                if best_fuzzy > best_score:
                    best_score = best_fuzzy
                    match_type = "fuzzy match"

            # Threshold: 72 (user can deselect false positives)
            if best_score >= 72 and donor not in seen:
                results.append((donor, best_score, match_type))
                seen.add(donor)

    # --- Geo fallback: if sparse results, try geo-stripped search terms ---
    if len(results) < 5:
        for term in search_names:
            sn = normalize_donor(term)
            sn_geo = strip_geo(sn)
            if sn_geo != sn and sn_geo:
                geo_vars = build_variants(sn_geo)
                for donor in donor_list:
                    if donor in seen:
                        continue
                    d_vars = donor_variants_cache.get(donor, set())
                    # Exact variant match with geo-stripped term
                    if geo_vars & d_vars:
                        results.append((donor, 94, "geo-variant match"))
                        seen.add(donor)
                        continue
                    # Prefix match with geo-stripped term
                    matched_prefix = False
                    for sv in geo_vars:
                        if len(sv) < 2:
                            continue
                        for dv in d_vars:
                            if len(dv) < 2:
                                continue
                            shorter, longer = (sv, dv) if len(sv) <= len(dv) else (dv, sv)
                            if longer.startswith(shorter):
                                n_words = len(shorter.split())
                                if n_words >= 2 and len(shorter) >= 6:
                                    results.append((donor, 94, "geo-prefix match"))
                                    seen.add(donor)
                                    matched_prefix = True
                                    break
                                elif n_words == 1 and len(shorter) >= 6:
                                    results.append((donor, 94, "geo-prefix match"))
                                    seen.add(donor)
                                    matched_prefix = True
                                    break
                                elif n_words == 1 and len(shorter) >= 2:
                                    if longer == shorter or longer.startswith(shorter + " "):
                                        results.append((donor, 94, "geo-prefix match"))
                                        seen.add(donor)
                                        matched_prefix = True
                                        break
                        if matched_prefix:
                            break

    # --- Address confidence signal ---
    MAX_COLOCATED = 5
    high_confidence = {name for name, score, _ in results if score >= 86}

    # Boost borderline fuzzy matches that share an address with a high-confidence match
    boosted = []
    for name, score, mtype in results:
        if 72 <= score <= 85:
            shares_addr = False
            for addr_key in donor_to_addrs.get(name, []):
                co_located = addr_to_donors.get(addr_key, set())
                if len(co_located) <= MAX_COLOCATED and (co_located & high_confidence):
                    shares_addr = True
                    break
            if shares_addr:
                boosted.append((name, 88, f"address-confirmed {mtype}"))
            else:
                boosted.append((name, score, mtype))
        else:
            boosted.append((name, score, mtype))
    results = boosted

    # Discover co-located donors of high-confidence matches
    address_hits = set()
    for name in high_confidence:
        for addr_key in donor_to_addrs.get(name, []):
            co_located = addr_to_donors.get(addr_key, set())
            if len(co_located) <= MAX_COLOCATED:
                address_hits |= co_located
    for donor in address_hits - seen:
        results.append((donor, 85, "address match"))
        seen.add(donor)

    # Sort by score descending, then alphabetically
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


# ---------------------------------------------------------------------------
# Sidebar — global info
# ---------------------------------------------------------------------------
st.sidebar.title("🏛️ 8872 Explorer")
st.sidebar.markdown(f"**{len(df):,}** contributions  •  **{df['recipient'].nunique()}** recipients")
st.sidebar.markdown(f"**Date range:** {df['date'].min():%m/%d/%Y} – {df['date'].max():%m/%d/%Y}")
st.sidebar.divider()
st.sidebar.markdown(
    f"**Individuals:** {len(df[df['donor_type'] == 'Individual']):,}  \n"
    f"**Non-Individuals:** {len(df[df['donor_type'] == 'Non-Individual']):,}"
)
st.sidebar.divider()
st.sidebar.markdown(
    "**To avoid data duplication in the database, donors who donated multiple "
    "contributions in the same amount on the same day may have had contributions "
    "removed. Please check line-item data against the relevant PDF filings for accuracy.**"
)


DOWNLOAD_CAP = 50000  # Max rows for CSV download generation


def _lazy_csv(df_slice, display_cols, col_names, key_prefix, section_key, label, filename):
    """Render a download button that only generates CSV when clicked.

    Uses st.session_state to defer CSV generation until the user actually
    requests the download, keeping memory low on large result sets.
    """
    csv_state_key = f"{key_prefix}{section_key}_csv"
    n_rows = len(df_slice)

    if n_rows > DOWNLOAD_CAP:
        st.caption(f"Too many results ({n_rows:,}) for download — add more filters to narrow below {DOWNLOAD_CAP:,}.")
        return

    if st.button(f"📥 {label}", key=f"{key_prefix}prep_{section_key}"):
        dl = df_slice[display_cols].copy()
        dl.columns = col_names
        dl["Date"] = dl["Date"].dt.strftime("%m/%d/%Y")
        dl = dl.sort_values(["Donor", "Date"])
        st.session_state[csv_state_key] = dl.to_csv(index=False)
        del dl

    if csv_state_key in st.session_state:
        st.download_button(
            f"⬇️ Download {filename}",
            data=st.session_state[csv_state_key],
            file_name=filename,
            mime="text/csv",
            key=f"{key_prefix}dl_{section_key}",
        )


def display_results(results, key_prefix=""):
    """Show results separated into Individual and Non-Individual sections.

    Uses lazy CSV generation — CSV strings are only built when the user
    clicks a download button, keeping memory usage low on large result sets.
    """
    if results.empty:
        st.info("No contributions match your criteria.")
        return

    DISPLAY_CAP = 5000

    # Summary metrics (computed from FULL results)
    m1, m2 = st.columns(2)
    m1.metric("Total Amount", f"${results['amount'].sum():,.0f}")
    m2.metric("Unique Donors", f"{results['donor'].nunique():,}")

    display_cols = [
        "donor_type", "donor", "employer", "occupation",
        "street", "city", "state", "zip_code",
        "amount", "date", "recipient", "recipient_party"
    ]
    col_names = [
        "Donor Type", "Donor", "Employer", "Occupation",
        "Street", "City", "State", "ZIP",
        "Amount", "Date", "Recipient", "Party"
    ]

    individuals = results[results["donor_type"] == "Individual"]
    non_individuals = results[results["donor_type"] == "Non-Individual"]

    # --- Non-Individuals ---
    st.markdown(f"#### Non-Individual Donors ({len(non_individuals):,} contributions, "
                f"{non_individuals['donor'].nunique():,} unique donors, "
                f"${non_individuals['amount'].sum():,.0f} total)")

    if not non_individuals.empty:
        ni_display = non_individuals[display_cols].copy()
        ni_display.columns = col_names
        ni_display["Date"] = ni_display["Date"].dt.strftime("%m/%d/%Y")
        ni_display = ni_display.sort_values(["Donor", "Date"])

        if len(ni_display) > DISPLAY_CAP:
            st.caption(f"Showing first {DISPLAY_CAP:,} of {len(ni_display):,} results.")
            st.dataframe(ni_display.head(DISPLAY_CAP), use_container_width=True, hide_index=True)
        else:
            st.dataframe(ni_display, use_container_width=True, hide_index=True)
        del ni_display

        _lazy_csv(non_individuals, display_cols, col_names, key_prefix,
                  "non_ind", "Prepare Non-Individual CSV", "non_individual_results.csv")
    else:
        st.info("No non-individual contributions match your criteria.")

    st.divider()

    # --- Individuals ---
    st.markdown(f"#### Individual Donors ({len(individuals):,} contributions, "
                f"{individuals['donor'].nunique():,} unique donors, "
                f"${individuals['amount'].sum():,.0f} total)")

    if not individuals.empty:
        ind_display = individuals[display_cols].copy()
        ind_display.columns = col_names
        ind_display["Date"] = ind_display["Date"].dt.strftime("%m/%d/%Y")
        ind_display = ind_display.sort_values(["Donor", "Date"])

        if len(ind_display) > DISPLAY_CAP:
            st.caption(f"Showing first {DISPLAY_CAP:,} of {len(ind_display):,} results.")
            st.dataframe(ind_display.head(DISPLAY_CAP), use_container_width=True, hide_index=True)
        else:
            st.dataframe(ind_display, use_container_width=True, hide_index=True)
        del ind_display

        _lazy_csv(individuals, display_cols, col_names, key_prefix,
                  "ind", "Prepare Individual CSV", "individual_results.csv")
    else:
        st.info("No individual contributions match your criteria.")


tab1, tab2 = st.tabs(["📋 Contribution Search", "🔍 Comparative Donor Analysis"])

# ===========================
# TAB 1: Contribution Search
# ===========================
with tab1:
    st.header("Contribution Search")
    show_recipients = st.toggle("Show recipients", value=False, key="show_recipients")
    st.caption("Filter contributions by donor, recipient, date, and amount — similar to FEC individual contributions search.")

    # --- Filter controls in columns ---
    col_left, col_right = st.columns(2)

    with col_left:
        with st.expander("🧑 Donor Filters", expanded=True):
            donor_search = st.text_input(
                "Donor name search",
                help="Type a donor name and click 'Find Matching Donors' to see fuzzy matches. Supports acronyms (e.g., IBM finds International Business Machines).",
                placeholder="e.g., CVS, IBM, EQT Corporation",
                key="tab1_donor_search",
            )

            # --- Fuzzy match button + multiselect ---
            # Initialize persistent batch of selected donors
            if "tab1_batch_donors" not in st.session_state:
                st.session_state["tab1_batch_donors"] = []   # list of (name, score, mtype)
            if "tab1_batch_names" not in st.session_state:
                st.session_state["tab1_batch_names"] = set()  # for fast lookup

            btn_col1, btn_col2 = st.columns([3, 1])
            with btn_col1:
                find_clicked = st.button("🔍 Find Matching Donors", key="tab1_find_donors")
            with btn_col2:
                clear_clicked = st.button("🗑️ Clear", key="tab1_clear_donors")

            if clear_clicked:
                st.session_state["tab1_batch_donors"] = []
                st.session_state["tab1_batch_names"] = set()
                st.session_state["tab1_donor_matches"] = []
                if "tab1_selected_donors" in st.session_state:
                    del st.session_state["tab1_selected_donors"]
                st.rerun()

            if find_clicked:
                if donor_search.strip():
                    search_terms = [t.strip() for t in donor_search.split(";") if t.strip()]
                    new_matches = []
                    seen = set(st.session_state["tab1_batch_names"])
                    for term in search_terms:
                        matches = fuzzy_find_donors(term, unique_donors)
                        for donor_name, score, match_type in matches:
                            if donor_name not in seen:
                                new_matches.append((donor_name, score, match_type))
                                seen.add(donor_name)
                    st.session_state["tab1_donor_matches"] = new_matches
                else:
                    st.session_state["tab1_donor_matches"] = []

            # Show new matches for selection (from latest search)
            new_matches = st.session_state.get("tab1_donor_matches", [])
            if new_matches:
                new_options = [name for name, score, mtype in new_matches]
                new_labels = {
                    name: f"{name}  ({mtype}, {score}%)"
                    for name, score, mtype in new_matches
                }
                st.caption(f"Found {len(new_matches)} new matching donor name(s). Select the ones to add:")
                newly_selected = st.multiselect(
                    "New matches",
                    options=new_options,
                    default=new_options,  # Pre-select all new matches
                    format_func=lambda x: new_labels.get(x, x),
                    key="tab1_new_matches_select",
                    label_visibility="collapsed",
                )
                # Add selected new matches to the persistent batch
                if st.button("➕ Add to batch", key="tab1_add_batch"):
                    for name in newly_selected:
                        if name not in st.session_state["tab1_batch_names"]:
                            # Find the match info
                            for n, s, m in new_matches:
                                if n == name:
                                    st.session_state["tab1_batch_donors"].append((n, s, m))
                                    st.session_state["tab1_batch_names"].add(n)
                                    break
                    st.session_state["tab1_donor_matches"] = []
                    if "tab1_new_matches_select" in st.session_state:
                        del st.session_state["tab1_new_matches_select"]
                    st.rerun()

            # Show the accumulated batch (deduplicate as safeguard)
            raw_batch = st.session_state.get("tab1_batch_donors", [])
            seen_batch = set()
            batch = []
            for item in raw_batch:
                if item[0] not in seen_batch:
                    batch.append(item)
                    seen_batch.add(item[0])
            st.session_state["tab1_batch_donors"] = batch
            st.session_state["tab1_batch_names"] = seen_batch
            selected_donors = []
            if batch:
                batch_options = [name for name, score, mtype in batch]
                batch_labels = {
                    name: f"{name}  ({mtype}, {score}%)"
                    for name, score, mtype in batch
                }
                st.caption(f"**{len(batch)} donor(s) in batch.** Deselect any to exclude from search:")
                selected_donors = st.multiselect(
                    "Selected donors",
                    options=batch_options,
                    default=batch_options,
                    format_func=lambda x: batch_labels.get(x, x),
                    key="tab1_selected_donors",
                    label_visibility="collapsed",
                )

            donor_type_filter = st.multiselect(
                "Donor type",
                options=["Individual", "Non-Individual"],
                help="Filter by individual or non-individual (organizational) donors.",
            )
            donor_state = st.multiselect(
                "Donor state",
                options=sorted(df["state"].dropna().unique()),
            )
            donor_city = st.text_input("Donor city", placeholder="e.g., Washington")
            donor_employer = st.text_input("Employer", placeholder="e.g., Google")
            donor_occupation = st.text_input("Occupation", placeholder="e.g., Attorney")

        with st.expander("💰 Amount", expanded=False):
            amount_col1, amount_col2 = st.columns(2)
            with amount_col1:
                min_amount = st.number_input("Minimum ($)", min_value=0, value=0, step=100)
            with amount_col2:
                max_amount = st.number_input("Maximum ($)", min_value=0, value=0, step=100,
                                             help="Leave at 0 for no maximum")

    with col_right:
        with st.expander("🏢 Recipient Filters", expanded=True):
            recipient_filter = st.multiselect(
                "Recipient(s)",
                options=sorted(df["recipient"].unique()),
            )
            party_filter = st.multiselect(
                "Recipient party",
                options=["Democrats", "Republicans"],
            )

        with st.expander("📅 Date Range", expanded=True):
            date_col1, date_col2 = st.columns(2)
            with date_col1:
                start_date = st.date_input(
                    "Start date",
                    value=df["date"].min().date(),
                    min_value=df["date"].min().date(),
                    max_value=df["date"].max().date(),
                )
            with date_col2:
                end_date = st.date_input(
                    "End date",
                    value=df["date"].max().date(),
                    min_value=df["date"].min().date(),
                    max_value=df["date"].max().date(),
                )

    # --- Apply filters ---
    if st.button("🔎 Search", key="search_tab1", type="primary"):
        results = df

        # Date filter
        results = results[
            (results["date"] >= pd.Timestamp(start_date)) &
            (results["date"] <= pd.Timestamp(end_date))
        ]

        # Donor type filter
        if donor_type_filter:
            results = results[results["donor_type"].isin(donor_type_filter)]

        # Donor name filter — use the user's selected donors from fuzzy matching
        if selected_donors:
            results = results[results["donor"].isin(selected_donors)]

        # State filter
        if donor_state:
            results = results[results["state"].isin(donor_state)]

        # City filter
        if donor_city.strip():
            results = results[results["city"].str.upper().str.contains(donor_city.strip().upper(), na=False)]

        # Employer filter
        if donor_employer.strip():
            results = results[results["employer"].str.upper().str.contains(donor_employer.strip().upper(), na=False)]

        # Occupation filter
        if donor_occupation.strip():
            results = results[results["occupation"].str.upper().str.contains(donor_occupation.strip().upper(), na=False)]

        # Recipient filter
        if recipient_filter:
            results = results[results["recipient"].isin(recipient_filter)]

        # Party filter
        if party_filter:
            results = results[results["recipient_party"].isin(party_filter)]

        # Amount filter
        if min_amount > 0:
            results = results[results["amount"] >= min_amount]
        if max_amount > 0:
            results = results[results["amount"] <= max_amount]

        # Deduplicate results (safeguard against any upstream duplication)
        results = results.drop_duplicates()

        # --- Display results ---
        st.divider()
        st.subheader(f"Results: {len(results):,} contributions")
        display_results(results, key_prefix="tab1_")

        # --- Recipient breakdown (when toggled) ---
        if show_recipients and not results.empty:
            st.divider()
            n_recip = results['recipient'].nunique()
            rep_results = results[results['recipient_party'] == 'Republicans']
            dem_results = results[results['recipient_party'] == 'Democrats']
            n_rep = rep_results['recipient'].nunique()
            n_dem = dem_results['recipient'].nunique()

            st.markdown(f"**{n_recip} recipients: {n_rep} Republican, {n_dem} Democratic**")

            # Build combined summary for download
            recip_summary_rows = []

            if n_rep > 0:
                with st.expander(f"🔴 Republican ({n_rep} recipients)", expanded=False):
                    rep_totals = rep_results.groupby('recipient')['amount'].sum().sort_values(ascending=False)
                    for recip, total in rep_totals.items():
                        st.markdown(f"- **{recip}**: ${total:,.0f}")
                        recip_summary_rows.append({'Party': 'Republican', 'Recipient': recip, 'Total Amount': total})

            if n_dem > 0:
                with st.expander(f"🔵 Democratic ({n_dem} recipients)", expanded=False):
                    dem_totals = dem_results.groupby('recipient')['amount'].sum().sort_values(ascending=False)
                    for recip, total in dem_totals.items():
                        st.markdown(f"- **{recip}**: ${total:,.0f}")
                        recip_summary_rows.append({'Party': 'Democratic', 'Recipient': recip, 'Total Amount': total})

            if recip_summary_rows:
                recip_dl = pd.DataFrame(recip_summary_rows)
                csv_recip = recip_dl.to_csv(index=False)
                st.download_button(
                    "📥 Download Recipient Breakdown (CSV)",
                    data=csv_recip,
                    file_name="recipient_breakdown.csv",
                    mime="text/csv",
                    key="tab1_dl_recip_breakdown",
                )


# ===========================
# TAB 2: Comparative Donor Analysis
# ===========================
with tab2:
    st.header("Comparative Donor Analysis")
    st.caption(
        "Find donors who gave one group of recipients ≥ X dollars "
        "but gave another group ≤ Y dollars over a date range. "
        "Similar donor names are automatically grouped using fuzzy matching."
    )

    # --- Donor type filter ---
    comp_donor_type = st.multiselect(
        "Donor type",
        options=["Individual", "Non-Individual"],
        key="comp_donor_type",
        help="Filter by individual or non-individual (organizational) donors. Leave empty for all.",
    )

    # --- Date range ---
    st.subheader("📅 Date Range")
    dc1, dc2 = st.columns(2)
    with dc1:
        comp_start = st.date_input(
            "Start date",
            value=df["date"].min().date(),
            min_value=df["date"].min().date(),
            max_value=df["date"].max().date(),
            key="comp_start",
        )
    with dc2:
        comp_end = st.date_input(
            "End date",
            value=df["date"].max().date(),
            min_value=df["date"].min().date(),
            max_value=df["date"].max().date(),
            key="comp_end",
        )

    st.divider()

    # --- Group A: gave >= X ---
    st.subheader('Group A — Gave ≥ X to these recipients')
    group_a_recipients = st.multiselect(
        "Select recipient(s) for Group A",
        options=sorted(df["recipient"].unique()),
        key="group_a_recip",
    )
    group_a_threshold = st.number_input(
        "Minimum total given to Group A ($)",
        min_value=0,
        value=1000,
        step=100,
        key="group_a_thresh",
    )

    st.divider()

    # --- Group B: gave <= Y ---
    st.subheader('Group B — Gave ≤ Y to these recipients')
    group_b_recipients = st.multiselect(
        "Select recipient(s) for Group B",
        options=sorted(df["recipient"].unique()),
        key="group_b_recip",
    )
    group_b_threshold = st.number_input(
        "Maximum total given to Group B ($)",
        min_value=0,
        value=0,
        step=100,
        key="group_b_thresh",
        help="Donors who gave this amount or less (including $0) to Group B will be included.",
    )

    st.divider()

    if st.button("🔎 Run Comparative Analysis", key="search_tab2", type="primary"):
        if not group_a_recipients:
            st.warning("Please select at least one recipient for Group A.")
            st.stop()

        # Filter to date range
        date_mask = (
            (df["date"] >= pd.Timestamp(comp_start)) &
            (df["date"] <= pd.Timestamp(comp_end))
        )
        filtered = df[date_mask].copy()

        # Apply donor type filter
        if comp_donor_type:
            filtered = filtered[filtered["donor_type"].isin(comp_donor_type)]

        # Build donor clusters on demand (lazy-loaded to reduce startup memory)
        donor_clusters = build_donor_clusters(
            filtered["donor"].dropna().unique().tolist(),
            addr_to_donors, donor_to_addrs, donor_raw_addrs)

        # Add canonical donor name for cross-recipient fuzzy grouping
        filtered["canonical_donor"] = filtered["donor"].map(donor_clusters)

        # --- Compute Group A totals per canonical donor ---
        group_a_data = filtered[filtered["recipient"].isin(group_a_recipients)]
        group_a_totals = (
            group_a_data.groupby("canonical_donor")["amount"]
            .sum()
            .reset_index()
            .rename(columns={"amount": "group_a_total"})
        )
        qualifying_a = set(
            group_a_totals[group_a_totals["group_a_total"] >= group_a_threshold]["canonical_donor"]
        )

        # --- Compute Group B totals per canonical donor ---
        if group_b_recipients:
            group_b_data = filtered[filtered["recipient"].isin(group_b_recipients)]
            group_b_totals = (
                group_b_data.groupby("canonical_donor")["amount"]
                .sum()
                .reset_index()
                .rename(columns={"amount": "group_b_total"})
            )
            exceeding_b = set(
                group_b_totals[group_b_totals["group_b_total"] > group_b_threshold]["canonical_donor"]
            )
        else:
            exceeding_b = set()

        # Final qualifying canonical donors
        qualifying_canonical = qualifying_a - exceeding_b

        if not qualifying_canonical:
            st.info("No donors match your criteria.")
            st.stop()

        # Get all raw donor names in qualifying clusters
        qualifying_donors = set(
            filtered[filtered["canonical_donor"].isin(qualifying_canonical)]["donor"]
        )

        # Map canonical → most common raw donor name (for display)
        name_counts = (
            filtered[filtered["canonical_donor"].isin(qualifying_canonical)]
            .groupby(["canonical_donor", "donor"])
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
            .drop_duplicates("canonical_donor")
        )
        canonical_to_display = dict(zip(name_counts["canonical_donor"], name_counts["donor"]))

        st.subheader(f"Found {len(qualifying_canonical):,} matching donors")

        # --- Summary by recipient ---
        all_recipients = set(group_a_recipients) | set(group_b_recipients or [])
        relevant_data = filtered[
            (filtered["canonical_donor"].isin(qualifying_canonical)) &
            (filtered["recipient"].isin(all_recipients))
        ]

        # Split summaries by donor type
        for dtype in ["Non-Individual", "Individual"]:
            dtype_data = relevant_data[relevant_data["donor_type"] == dtype]
            if dtype_data.empty:
                st.markdown(f"#### {dtype} Donors")
                st.info(f"No {dtype.lower()} donors match your criteria.")
                continue

            st.markdown(f"#### {dtype} Donors ({dtype_data['canonical_donor'].nunique():,} donors, "
                        f"${dtype_data['amount'].sum():,.0f} total)")

            # Per-recipient breakdown (primary view)
            recipient_breakdown = (
                dtype_data.groupby(["canonical_donor", "recipient"])["amount"]
                .sum()
                .unstack(fill_value=0)
                .reset_index()
            )
            # Map canonical names to display names
            recipient_breakdown["Donor"] = recipient_breakdown["canonical_donor"].map(canonical_to_display)
            recipient_breakdown = recipient_breakdown.drop(columns=["canonical_donor"])
            # Move Donor to first column
            cols = ["Donor"] + [c for c in recipient_breakdown.columns if c != "Donor"]
            recipient_breakdown = recipient_breakdown[cols]
            # Add total column
            num_cols = [c for c in recipient_breakdown.columns if c != "Donor"]
            recipient_breakdown["Total"] = recipient_breakdown[num_cols].sum(axis=1)
            recipient_breakdown = recipient_breakdown.sort_values("Total", ascending=False)
            recipient_breakdown.columns.name = None

            # Format for display
            fmt_recip = recipient_breakdown.copy()
            for col in fmt_recip.columns:
                if col != "Donor":
                    fmt_recip[col] = fmt_recip[col].apply(lambda x: f"${x:,.0f}")
            st.dataframe(fmt_recip, use_container_width=True, hide_index=True)

            # Download (unformatted numbers for CSV)
            summary_csv = recipient_breakdown.to_csv(index=False)
            st.download_button(
                f"📥 Download {dtype} Summary (CSV)",
                data=summary_csv,
                file_name=f"comparative_{dtype.lower().replace('-', '_')}_summary.csv",
                mime="text/csv",
                key=f"dl_summary_{dtype}",
            )

            st.divider()

        # --- Line items (all types combined, separated in display) ---
        st.markdown("#### Contribution Line Items")
        line_items = filtered[
            (filtered["donor"].isin(qualifying_donors)) &
            (filtered["recipient"].isin(all_recipients))
        ].copy()
        display_results(line_items, key_prefix="tab2_")
