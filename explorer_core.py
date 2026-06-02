"""
explorer_core.py — Shared logic for the Skinny 8872 Contribution Explorer.

Pure (non-Streamlit) functions used by BOTH the Streamlit app (app.py) and the
QC harness (qc_runner.py), so the two are guaranteed to use identical
normalization, classification, and donor-grouping logic.

Contents:
  * Name normalization + variant generation         (verbatim from the old app)
  * NAME_ALIAS_GROUPS + alias lookup  ............... Problem 3, Tier 1
  * Address tiebreaker helpers
  * classify() ..................................... Problem 2 (layered heuristic)
  * load_donor_groups() ............................ Problem 3, Tier 1.5
  * build_donor_clusters() ......................... Problem 3, 3-tier hierarchy
"""

import re
import os
import json
import pandas as pd
from rapidfuzz import fuzz
from collections import defaultdict

# ---------------------------------------------------------------------------
# Name normalization & matching helpers
# ---------------------------------------------------------------------------

# Abbreviation maps for expanding/contracting common business terms
ABBREVS = {
    'ASSOC': 'ASSOCIATION', 'ASSN': 'ASSOCIATION',
    'INS': 'INSURANCE', 'CORP': 'CORPORATION', 'CO': 'COMPANY',
    'GRP': 'GROUP', 'INTL': 'INTERNATIONAL', 'NATL': 'NATIONAL',
    'MGMT': 'MANAGEMENT', 'SVCS': 'SERVICES', 'SVC': 'SERVICE',
    'GOVT': 'GOVERNMENT', 'ADMIN': 'ADMINISTRATION',
    'TECH': 'TECHNOLOGY', 'TECHS': 'TECHNOLOGIES',
    'PHARMA': 'PHARMACEUTICAL', 'MFG': 'MANUFACTURING',
    'CTR': 'CENTER', 'DEV': 'DEVELOPMENT',
}
ABBREVS_REV = {}
for _k, _v in ABBREVS.items():
    if _v not in ABBREVS_REV:
        ABBREVS_REV[_v] = _k

LEGAL_RE = re.compile(
    r'\s+(INC|LLC|LTD|LP|LLP|PA|PC|PLLC|CORP|CORPORATION|COMPANY|CO|'
    r'INCORPORATED|LIMITED|PAC)\s*$')

US_STATES_RE = re.compile(
    r'\s+OF\s+(IOWA|OHIO|MICHIGAN|TEXAS|FLORIDA|CALIFORNIA|ILLINOIS|INDIANA|'
    r'KENTUCKY|TENNESSEE|GEORGIA|VIRGINIA|NORTH CAROLINA|SOUTH CAROLINA|'
    r'PENNSYLVANIA|NEW YORK|NEW JERSEY|MARYLAND|COLORADO|ARIZONA|NEVADA|'
    r'WISCONSIN|MINNESOTA|MISSOURI|KANSAS|OKLAHOMA|ARKANSAS|MISSISSIPPI|'
    r'ALABAMA|LOUISIANA|OREGON|WASHINGTON|UTAH|MONTANA|NEBRASKA|IDAHO|'
    r'WYOMING|MAINE|VERMONT|NEW HAMPSHIRE|CONNECTICUT|MASSACHUSETTS|'
    r'RHODE ISLAND|DELAWARE|HAWAII|ALASKA|WEST VIRGINIA|NORTH DAKOTA|'
    r'SOUTH DAKOTA|NEW MEXICO|AMERICA|THE UNITED STATES)\b', re.IGNORECASE)


def normalize_donor(name):
    """Full normalization: parentheticals, IN-KIND, apostrophes, &->AND, /->space, punctuation."""
    s = name.upper().strip()
    s = re.sub(r'\s*\([^)]*\)', '', s)          # strip parenthetical content
    s = re.sub(r'\s*[-\u2013]?\s*IN[\s-]*KIND\b', '', s)
    s = re.sub(r'N/A$', '', s)
    s = re.sub(r"'", '', s)                      # strip apostrophes entirely
    s = re.sub(r'&', ' AND ', s)
    s = re.sub(r'/', ' ', s)
    # Collapse initials like "U.S." → "US", "A.T." → "AT" BEFORE removing dots
    s = re.sub(r'\b([A-Z])\.([A-Z])\.?', r'\1\2', s)
    s = re.sub(r'[.\-,;:"\"]', " ", s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def normalize_donor_no_amp(name):
    """Like normalize_donor but removes & entirely (for H&R, M&T, S&C)."""
    s = name.upper().strip()
    s = re.sub(r'\s*\([^)]*\)', '', s)          # strip parenthetical content
    s = re.sub(r'\s*[-\u2013]?\s*IN[\s-]*KIND\b', '', s)
    s = re.sub(r'N/A$', '', s)
    s = re.sub(r"'", '', s)                      # strip apostrophes entirely
    s = re.sub(r'&', ' ', s)
    s = re.sub(r'/', ' ', s)
    s = re.sub(r'[.\-,;:"\"]', " ", s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def expand_abbrevs(name):
    """Expand abbreviations: ASSOC->ASSOCIATION, INS->INSURANCE, etc."""
    return ' '.join(ABBREVS.get(w, w) for w in name.split())


def contract_abbrevs(name):
    """Contract words to abbreviations: ASSOCIATION->ASSOC, etc."""
    return ' '.join(ABBREVS_REV.get(w, w) for w in name.split())


def strip_legal(name):
    """Recursively strip trailing legal suffixes (INC, LLC, LTD, etc.)."""
    prev = ''
    while prev != name:
        prev = name
        name = LEGAL_RE.sub('', name).strip()
    return name


def strip_geo(name):
    """Strip geographic qualifiers like 'of Iowa', 'of Ohio'."""
    return US_STATES_RE.sub('', name).strip()


def no_the(s):
    """Strip leading 'THE ' from a name."""
    return s[4:] if s.startswith('THE ') else s


def build_variants(name):
    """Build a set of normalized variants for exact matching."""
    sn = normalize_donor(name)
    sn2 = normalize_donor_no_amp(name)
    variants = set()
    for base in [sn, sn2]:
        nl = strip_legal(base)
        ex = expand_abbrevs(base)
        co = contract_abbrevs(base)
        ex_nl = strip_legal(ex)
        co_nl = strip_legal(co)
        for v in [base, nl, ex, co, ex_nl, co_nl]:
            variants.add(v)
            variants.add(no_the(v))
    variants.discard('')

    # Alias expansion (Problem 3, Tier 1): if any variant belongs to a
    # NAME_ALIAS_GROUP, add every canonical form in that group. Mirrors the QC
    # harness so a term like "ACE American Insurance Company" also resolves to
    # its alias "Chubb".
    matched_gids = set()
    for v in variants:
        gid = _alias_lookup.get(v)
        if gid is not None:
            matched_gids.add(gid)
    for gid in matched_gids:
        variants |= _alias_group_canonicals[gid]

    return variants


# ---------------------------------------------------------------------------
# Name alias groups — known alternate names for the same entity
# ---------------------------------------------------------------------------
NAME_ALIAS_GROUPS = [
    ["Reynolds American, Inc.", "RAI Services"],
    ["Energy Transfer Group", "Energy Transfer, LP", "Sunoco"],
    ["Philip Morris International", "PMI Global Services Inc.", "Global Services Inc.", "PHILIP MORRIS USA"],
    ["National Rifle Association of America", "NRA INSTITUTE FOR LEGISLATIVE ACTION"],
    ["Marathon Petroleum Company", "Marathon Oil Company"],
    ["American Electric Power", "AEP", "AEP Service Corporation"],
    ["Poet Energy", "Poet, LLC"],
    ["J&J Ventures Gaming LLC", "Jandj Ventures Gaming"],
    ["Conoco Phillips", "Phillips 66"],
    ["NextEra Energy Resources LLC", "NextEra Energy Point Beach LLC"],
    ["HNTB Corporation", "HNTB Holdings LTD. PAC"],
    ["Butler Snow LLP", "Butler Snow PAC"],
    ["CDR Enterprises LLC", "CDR Maguire Inc."],
    ["Correct Care Solutions", "Wellpath"],
    ["DLZ Indiana LLC", "DLZ Industrial LLC"],
    ["American Beverage Association Fund for Consumer Choice", "ABA"],
    ["Alliance Health Care Sharing Ministries", "ALLIANCE OF HEALTHCARE SHARING MINISTRIES"],
    ["McKesson Corporation", "McKesson Health Solutions LLC", "McKesson Corporation Employee PAC"],
    ["ITS Inc", "ITS, Inc."],
    ["Acentra Health", "Client Network Services Inc.", "CNSI"],
    ["Archer Daniels Midland Company", "ADM"],
    ["CHS, Inc.", "CHS Inc. PAC"],
    ["Motion Picture Association of America", "MPAA"],
    ["Endeavor", "UFC", "International Merchandising Company", "Zuffa, LLC"],
    ["Husch Blackwell LLP", "Husch Blackwell Strategies", "Husch Blackwell Political Action Committee"],
    ["AMGEN", "Amgen Inc State Political Contributions Acct"],
    ["TransCanada Keystone Pipeline, LP", "TC Energy", "TransCanada USA Pipeline Services LLC"],
    ["BGR Government Affairs, LLC", "BGR Group"],
    ["Kleo, Inc.", "ClassWallet"],
    ["The American Association of Nurse Anesthesiology", "AANA", "CRNA PAC"],
    ["The Boeing Company", "The Boeing Company PAC"],
    ["Tyler Technologies", "NIC-USA", "NIC, Inc."],
    ["Verra Mobility", "American Traffic Solutions"],
    ["Magna Services of America, Inc", "Magna International"],
    ["ACE American Insurance Company", "Chubb", "ACE American Insurance Co"],
    ["BrightSpring Health Services", "ResCare Inc", "Brightspring"],
    ["Cambium Learning Inc", "Lexia Learning Systems LLC"],
    ["Cencora", "AmerisourceBergen Corporation"],
    ["Parsons", "Parsons Corporation"],
    ["Wireless Internet Service Providers Association", "WISPA"],
    ["Motorola", "Motorola Solutions", "Motorola Solutions PAC Multicandidate Committee"],
    ["Fidelity Corporate Services", "Fidelity Investments"],
    ["America's Health Insurance Plans", "AHIP"],
    ["General Motors Corporation", "General Motors Company PAC", "GM"],
    ["Meijer Corporation", "Meijer, Inc."],
    ["Planet Fitness Franchise", "Planet Fitness Independent Franchisee Council"],
    ["Prenax, Inc.", "SAP"],
    ["United Healthcare", "UnitedHealth Group", "UHG", "UnitedHealthcare Services"],
    ["United Parcel Service", "UPS", "UPSPAC"],
    ["United Service Automobile Association", "USAA"],
    ["National Beer Wholesalers Association", "NBWA"],
    ["Winning Connections", "John Jameson"],
    ["HDR Engineering, Inc.", "HDR, Inc. Employees Owners PAC", "HDR inc", "HDR INC EMPLOYEE OWNERS PAC"],
    ["DaVita, Inc.", "Total Renal Care", "TRC"],
    ["Duane Morris LLP Cmte", "Duane Morris LLP Government Committee Federal Fund"],
    ["Gainwell Technologies", "Health Management Systems, Inc.", "Gainwell"],
    ["Novartis Services Inc.", "Novartis Finance Corp", "NOVARTIS"],
    ["Bombardier", "Bombardier Aerospace USA Inc.", "LearJet Inc."],
    ["Verizon", "Verizon Wireless", "Verizon Communications Inc"],
    ["Waterfront Construction Corporation", "LeFrak & Newport"],
    ["Comcast", "Comcast Financial Agency Corporation", "Comcast Business", "Comcast Financial Agency Corp"],
    ["Invenergy Clean Power LLC", "Invenergy LLC"],
    ["Philadelphia 76ers", "Harris Blitzer"],
    ["Eli Lilly and Company", "Eli Lilly and Company PAC"],
    ["Scientific Games International", "SGI", "Scientific Games LLC"],
    ["Holland & Knight, LLP", "EDPAC"],
    ["International Alliance of Theatrical Stage Employees Fed PAC", "IATSE"],
    ["National Media Corporation", "Lonestar Logos"],
    ["Southern California Gas Company", "SoCalGas"],
    ["Stewart Associates Land Development", "The Stewart Companies"],
    ["TBA of NJ LLC", "Tonio Burgos"],
    ["Aspen Skiing Company", "Aspen Snowmass", "Aspen One"],
    ["Education Reform Now Advocacy", "DFER"],
    ["Darden Restaurants, Inc.", "GMRI Inc.", "Darden"],
    ["Association of Dental Support Organizations", "ADSO"],
    ["American Council of Life Insurers", "ACLI"],
    ["ATU Action Fund", "ATU Special Holding Account"],
    ["International Association of Fire Fighters", "IAFF"],
    ["IUPAT Legislative Educational Committee", "IUPAT Political Action Together Political Comm."],
    ["Fresenius Medical Care", "Dialysis is Life Support", "FMC"],
    ["Alliant Energy", "Alliant Energy Corp Services Inc."],
    ["Mylan Inc.", "Viatris"],
    ["Lyft", "Advanced Technology Alliance"],
    ["Working for Working Americans", "Carpenters", "Carpenters Action Fund"],
    ["DRIVE Committee", "Teamsters"],
    ["Amoco", "bp"],
    ["Coca-Cola North America", "Coca-Cola Bottling Company", "Coca-Cola Consolidated, Inc.", "Liberty Coca-Cola Beverages"],
    ["United Automobile Workers V-CAP", "UAW"],
    ["Metropolitan Milwaukee Association of Commerce", "MMAC"],
    ["Pfizer Inc.", "Pfizer PAC"],
    ["AB Foundation", "AB PAC"],
    ["Western-Southern Life Insurance", "THE WESTERN AND SOUTHERN LIFE INSURANCE COMPANY"],
    ["Advance Financial", "ADVANCE FINANCIAL ADMINISTRATION LLC"],
    ["Peninsula Pacific Entertainment", "PENINSULA PACIFIC ENTERTAINMENT DEVELOPMENT LLC"],
    ["Stride", "K12 Management", "K12 Management Inc.", "Stride / K12 Management Inc."],
    ["Deloitte Services", "Deloitte Consulting", "Deloitte and Touche"],
    ["BlueCross BlueShield Association", "Blue Cross Blue Shield Association", "BLUECROSS BLUE SHIELD ASSOCIATION", "BCBSA"],
    ["Mondelez International", "Mondelez Global LLC"],
    ["WSP USA Corp", "WSP USA ADMINISTRATION INC"],
    # --- 2022 QC corrections ---
    ["Stellantis", "Fiat Chrysler Automobiles", "FIAT CHRYSLER AUTOMOBILES US LLC", "Formerly, Fiat Chrysler Automobiles"],
    ["Faegre Baker Daniels LLP", "Faegre Drinker Biddle and Reath LLP", "FAEGRE DRINKER LLP"],
    ["Prudential Financial", "Prudential AP", "PRUDENTIAL"],
    ["Lockheed Martin Corp Employees PAC", "LOCKHEED MARTIN EMPLOYEES PAC", "Lockheed Martin Corporation"],
    ["National Association of Home Builders", "National Association Home Builders", "NATIONAL ASSOC OF HOME BUILDERS", "NAHB"],
    ["American HealthCare Association", "AMERICAN HEALTH CARE ASSOCIATION", "AHCA"],
    ["Elevance Health Inc", "Anthem, Inc.", "ANTHEM INC"],
    ["CGI Technologies and Solutions Inc.", "CGI Technologies Solutions", "CGI TECHNOLOGIES AND SOLUTIONS"],
    ["Anheuser-Busch Companies", "ANHEUSER BUSCH COMPANIES", "ANHEUSER-BUSCH"],
    ["Karen Buchwald Wright Revocable Trust", "KAREN B WRIGHT REVOCABLE TRUST"],
    # --- 2024 QC parenthetical aliases ---
    ["Accenture", "Accenture PAC"],
    ["Advanced Energy United", "Advanced Energy Industries, Inc."],
    ["AECOM Technology Corporation", "AECOM"],
    ["Altice", "Cablevision Systems", "CSC Holdings LLC"],
    ["Amazon.com Services LLC", "Amazon.com, Inc."],
    ["American Academy of Orthopaedic Surgeons", "AAOS", "Political Action Committee of the AAOS"],
    ["American Clean Power Association", "ACP"],
    ["American Express", "American Express Travel Related Services"],
    ["American Federation of Teachers", "American Federation of Teachers COPE", "AFT Solidarity"],
    ["American Fuel and Petrochemical Manufacturers", "AFPM"],
    ["American Gas Association", "AGA"],
    ["American Hospital Association", "AHA"],
    ["American Hotel and Lodging Association", "AHLA"],
    ["American Petroleum Institute", "API"],
    ["American Property Casualty Insurance Association", "APCIA"],
    ["AMR HoldCo Inc.", "Global Medical Response"],
    ["Associated Builders and Contractors of Iowa Inc.", "Associated General Contractors of Iowa PAC"],
    ["AstraZeneca", "Alexion Pharmaceuticals Inc.", "Zeneca Inc"],
    ["AT&T", "AT and T Inc", "Atandt Services Inc"],
    ["Berkshire Hathaway Energy", "Berkshire Hathaway Energy Company"],
    ["Brownstein Hyatt Farber Schreck, LLP", "BHFS"],
    ["Caterpillar", "Carolina 1926, LLC", "Gregory Poole Equipment", "CAT", "Carolina Tractor Equipment Co"],
    ["Centene Management Company LLC", "Carolina Complete Health Network", "Centene Corporation PAC"],
    ["Center for American Progress", "CAP"],
    ["Center for Secure and Modern Elections", "CSME"],
    ["Chesapeake Realty Partners", "Honeygo Village LLC"],
    ["Cigna Health and Life Insurance Company", "Cigna Holding Co."],
    ["CLEAR", "Secure Identity LLC", "CLEAR Secure Identity Inc."],
    ["CNO Services LLC", "CNO Financial"],
    ["CNX Gas Company LLC", "CNX Resources Corporation"],
    ["Communications Workers of America", "Communications Workers of Amer Working Voices COPE", "CWA"],
    ["Cozen O'Connor Attorneys", "Cozen O'Connor PC"],
    ["CRH Americas", "Oldcastle Materials, Inc."],
    ["CVS Pharmacy Inc.", "Aetna"],
    ["Data Recognition Corporation", "DRC"],
    ["Delaware North Companies", "Delaware North Companies Inc"],
    ["Devils Arena Entertainment LLC", "Harris Blitzer Sports & Entertainment"],
    ["Door Dash, Inc", "DoorDash"],
    ["Edison Electric Institute", "EEI"],
    ["EDS Holdco LLC", "Emergency Disaster Services"],
    ["Entertainment Software Association", "ESA"],
    ["Federal Express PAC", "FedEx"],
    ["Genentech, Inc.", "Genentech USA"],
    ["Genting", "GAI PAC"],
    ["GlaxoSmithKline", "GSK", "GlaxoSmithKline Consumer Health Care LP", "Polaris Solutions LLC"],
    ["Global Companies LLC", "Global Partners"],
    ["Gopuff", "GoBrands, Inc. dba Gopuff"],
    ["Green Thumb Industries", "Vision Management Services"],
    ["HCA Healthcare", "HCA Management Services", "Hospital Corporation of America"],
    ["Healthcare Distribution Alliance", "HDA"],
    ["HillCo Partners, LLC", "Hilco Redevelopment"],
    ["IBEW PAC", "IBEW PAC Educational Fund"],
    ["Independence Blue Cross", "IBX"],
    ["International Association of Amusement Parks and Attractions", "IAAPA", "Int'l Assoc of Amusement Parks & Attractions"],
    ["International Union of Operating Engineers", "IUOE"],
    ["Ironworkers Political Action League", "Ironworkers Political Education Fund"],
    ["JBS USA", "S&C Resale Company"],
    ["K-Solv Group", "K Solv Group LLC", "Garner Environmental Services"],
    ["Kraken", "Payward, Inc."],
    ["Major League Central Fund", "MLB"],
    ["Mastercard", "MASTERCARD INCORPORATED", "MasterCard International"],
    ["Molina Healthcare, Inc.", "Molina Healthcare Inc PAC"],
    ["Motion Picture Association, Inc.", "MPA", "Motion Picture Assoc of America CA PAC"],
    ["National Association of Realtors", "NAR"],
    ["National Association of Water Companies", "NAWC"],
    ["National Education Association", "NEA", "NEA Advocacy Fund"],
    ["National Restaurant Association", "NRA"],
    ["Natural Resources Defense Council", "NRDC Action Fund", "NRDC"],
    ["New Mexico Health Care Association", "NMHCA"],
    ["NiSource", "NiSource Corporate Services", "NiSource Inc PAC", "Columbia Gas Co. of Kentucky", "Columbia Gas of Pennsylvania"],
    ["NOLA Education LLC", "Star Academy"],
    ["North Carolina Health Care Facilities Assoc Inc", "NCHCFA"],
    ["North Carolina Medical Society NC Medical Society", "N Carolinians for Affordable Health Care"],
    ["Nuclear Energy Institute", "NEI"],
    ["Occidental Petroleum Corporation", "Oxy"],
    ["OpenRoad Fund", "Brightstone Bridge"],
    ["Oracle Corporation", "Oracle America Inc"],
    ["Pace-O-Matic", "POM"],
    ["PENN Entertainment", "Penn National Gaming"],
    ["Pennsylvania Health Care Association", "PHCA"],
    ["PepsiCo Inc.", "Pepsi Bottling Ventures"],
    ["Physicians Advocacy Institute Inc", "PAI"],
    ["Raytheon", "RTX", "Employees of RTX Corporation PAC"],
    ["Reproductive Freedom for All", "formerly NARAL Pro Choice America"],
    ["Reworld", "formerly Covanta Energy Corporation"],
    ["SEVITA HEALTH", "Sevita formerly The Mentor Network"],
    ["Sheet Metal Workers Union Local 19", "SMART", "Sheet Metal, Air, Rail and Transportation Political Education League"],
    ["Snap Inc.", "Snapchat"],
    ["Solar Energy Industries Association", "SEIA"],
    ["Soo Line West Railroad", "CPKC"],
    ["Sports Betting Alliance", "SBA"],
    ["Sullivan Brothers Investments LLC", "Sullivan Environmental Services Inc."],
    ["Summit Ridge Energy", "Community Solar Action Fund"],
    ["Total Wine & More", "Retail Services & Systems, Inc."],
    ["Travelers Indemnity Company", "The Travelers Indemnity Company"],
    ["Union Pacific Railroad Company", "UP RAILROAD COMPANY"],
    ["United Food and Comm Wrkrs Active Ballot Fund-UFCW", "UFCW Working Families Advocacy Project"],
    ["Volkswagen Group of America, Inc.", "VW"],
    ["Walmart", "WAL PAC"],
    ["Water Sports Industry Association", "WSIA"],
    ["Withers Automotive", "Chrysler of Lawrenceburg"],
    ["Zurich North American Insurance", "Zurich American Insurance Company"],
    # --- 2022/2023 QC parenthetical aliases ---
    ["Majestic Realty Co.", "Majestic Reality Co"],
    ["Pinnacle West", "Arizona Public Service"],
    ["Unite USA Inc.", "Unite Us"],
    ["Caplin Family Offices Inc", "Pulse Clinical"],
    ["DentaQuest", "Dentaquest PAC-TN-C"],
    ["Huntsman International", "Huntsman Building Solutions"],
    ["Alvarez and Marsal Holdings, LLC", "AM Holdings"],
    ["Google, Inc.", "Google LLC"],
    ["Master Builders of Iowa", "Master Builders of Iowa PAC"],
    ["Tyson Foods Inc", "Tyson"],
    ["Ice Miller LLP", "Ice Miller PAC Ohio"],
    ["Massachusetts Mutual Life Insurance Co", "MassMutual"],

    ["Rittenhouse Consulting Group", "Rittenhouse Consulting Group LLC"],
    ["The Williams Companies", "The Williams Companies Inc."],
    ["Compass Strategies LLC", "Compass Strategies Public Affiars LLC"],
    ["CSX Transportation Inc.", "CSX Corporation"],
    ["Enbridge US Inc.", "Enbridge"],
    ["American Federation of Government Employees", "AFGE"],
    ["Conduent Business Services LLC", "Conduent Inc Political Action Committee"],
    ["Innovative Emergency Management Inc", "IEM"],
    ["Bayer", "Bayer US LLC"],
    ["International Union of Painter & Allied Trades", "Intl Union of Painters & Allied Trades", "IUPAT"],
    ["Mid-Atlantic Laborers Pol Edu Fund", "MALPEF"],
    ["Johnson & Johnson", "Johnson & Johnson PAC"],
    ["Mallinckrodt Pharmaceuticals", "Mallinckrodt LLC"],
    ["Keystone PF Acquistino LLC", "Argonne Capital"],
    ["Microsoft", "Microsoft Corporation"],
    ["Exelon Corporation", "Exelon Business Services"],
    ["Democratic Lieutenant Governors Association", "DLGA PAC"],
    ["Service Employees International Union", "SEIU"],

    ["Pursuit Advocacy", "Pursuit Advocacy, LLC"],
    ["Vote Blue Majority", "Vote Blue Majority Inc."],
]


def _compute_alias_canonicals(name):
    """Generate all canonical forms for a name (for alias matching)."""
    sn = normalize_donor(name)
    sn2 = normalize_donor_no_amp(name)
    results = set()
    for base in [sn, sn2]:
        nl = strip_legal(base)
        ex = expand_abbrevs(base)
        co = contract_abbrevs(base)
        ex_nl = strip_legal(ex)
        co_nl = strip_legal(co)
        for v in [base, nl, ex, co, ex_nl, co_nl]:
            results.add(v)
            results.add(no_the(v))
    results.discard('')
    return results


# Build alias lookup at module level
_alias_group_canonicals = {}  # group_id -> set of all canonical forms
_alias_lookup = {}            # canonical -> group_id
for _gid, _group in enumerate(NAME_ALIAS_GROUPS):
    _all_canons = set()
    for _raw_name in _group:
        _all_canons |= _compute_alias_canonicals(_raw_name)
    _alias_group_canonicals[_gid] = _all_canons
    for _c in _all_canons:
        _alias_lookup[_c] = _gid


# ---------------------------------------------------------------------------
# Address tiebreaker helpers for donor clustering
# ---------------------------------------------------------------------------
STREET_ABBREVS = {
    'ST': 'STREET', 'STR': 'STREET', 'RD': 'ROAD', 'DR': 'DRIVE',
    'AVE': 'AVENUE', 'AV': 'AVENUE', 'BLVD': 'BOULEVARD', 'LN': 'LANE',
    'CT': 'COURT', 'PL': 'PLACE', 'PKWY': 'PARKWAY', 'PKY': 'PARKWAY',
    'CIR': 'CIRCLE', 'TER': 'TERRACE', 'HWY': 'HIGHWAY', 'WAY': 'WAY',
    'N': 'NORTH', 'S': 'SOUTH', 'E': 'EAST', 'W': 'WEST',
    'NE': 'NORTHEAST', 'NW': 'NORTHWEST', 'SE': 'SOUTHEAST', 'SW': 'SOUTHWEST',
}

SECONDARY_RE = re.compile(
    r'\b(SUITE|STE|OFFICE|OFC|APT|APARTMENT|FLOOR|FL|BLDG|BUILDING|UNIT|RM|ROOM|#)\s*\.?\s*(\w+)',
    re.IGNORECASE
)


def normalize_street(street):
    """Normalize a street address to a base form for comparison."""
    if not street or str(street).strip() in ('', 'nan', 'None'):
        return ''
    s = str(street).upper().strip()
    s = SECONDARY_RE.sub('', s)
    s = re.sub(r'[.,#\-/]', ' ', s)
    words = s.split()
    expanded = [STREET_ABBREVS.get(w, w) for w in words]
    s = ' '.join(expanded)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_secondary(street):
    """Extract secondary address component (suite/office/apt/floor number)."""
    if not street:
        return None
    match = SECONDARY_RE.search(str(street).upper())
    if match:
        return match.group(2).strip()
    return None


def addresses_compatible(street1, city1, state1, street2, city2, state2):
    """Check if two addresses are compatible for tiebreaker matching.

    Returns:
      True  — addresses match (same base street, compatible secondary)
      False — addresses conflict (same building, different suite) or different location
      None  — can't determine (missing data)
    """
    norm1 = normalize_street(street1)
    norm2 = normalize_street(street2)
    if not norm1 or not norm2:
        return None

    c1 = str(city1 or '').upper().strip().replace('-', ' ')
    c2 = str(city2 or '').upper().strip().replace('-', ' ')
    s1 = str(state1 or '').upper().strip()
    s2 = str(state2 or '').upper().strip()

    if s1 != s2:
        return False
    if c1 and c2 and fuzz.ratio(c1, c2) < 80:
        return False
    if fuzz.ratio(norm1, norm2) < 75:
        return False

    sec1 = extract_secondary(street1)
    sec2 = extract_secondary(street2)
    if sec1 and sec2 and sec1 != sec2:
        return False

    return True


def extract_search_names(search_term):
    """Extract multiple search names from parentheticals and slashes."""
    names = [search_term.strip()]
    # Extract parenthetical names
    parens = re.findall(r'\(([^)]+)\)', search_term)
    for p in parens:
        if len(p.strip()) > 2:
            names.append(p.strip())
    # Base name without parentheticals
    base = re.sub(r'\s*\([^)]*\)', '', search_term).strip()
    if base and base != search_term.strip():
        names.append(base)
    # Split on " / "
    if ' / ' in search_term:
        for part in search_term.split(' / '):
            part = re.sub(r'\s*\([^)]*\)', '', part).strip()
            if len(part) > 2:
                names.append(part)
    return list(dict.fromkeys(names))  # deduplicate preserving order


def make_acronym(name):
    """Generate an acronym from a multi-word name.
    'International Business Machines' → 'IBM'
    'American Broadcasting Company' → 'ABC'
    """
    words = re.sub(r"[^A-Z0-9\s]", "", name.upper()).split()
    # Strip trailing legal suffixes (Inc, LLC, Corp, etc.)
    trailing_suffixes = {"INC", "LLC", "LTD", "CO", "CORP", "CORPORATION",
                         "LP", "LLP", "PA", "PC", "PLLC"}
    while words and words[-1] in trailing_suffixes:
        words.pop()
    # Remove filler words but keep substantive words like "Company", "Group"
    filler = {"THE", "OF", "AND", "FOR", "A", "AN"}
    meaningful = [w for w in words if w not in filler]
    if len(meaningful) >= 2:
        return "".join(w[0] for w in meaningful)
    return ""


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
# Problem 3, Tier 1.5 — load the human-validated donor_groups.json lookup
# ---------------------------------------------------------------------------
def load_donor_groups(path):
    """Return {UPPER(raw_donor_name): canonical_group_name}.

    Accepts either the flattened dict produced by prepare_data.py
    (donor_groups_flat.json) or the original list-of-dicts donor_groups.json.
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    flat = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if k and v:
                flat[str(k).upper()] = str(v)
    else:
        for e in data:
            d = str(e.get("donor", "")).strip()
            g = str(e.get("donor_group", "")).strip()
            if d and g:
                flat[d.upper()] = g
    return flat


# ---------------------------------------------------------------------------
# build_address_index — company-only address lookup for the clustering tiebreaker
# ---------------------------------------------------------------------------
def build_address_index(_df):
    """Build address → donors lookup for address-based matching.
    Only indexes non-individual (company) donors to avoid pulling in individuals.

    Returns:
        addr_to_donors: dict of (norm_street, state, zip5) → set of donor names
        donor_to_addrs: dict of donor name → set of (norm_street, state, zip5)
        donor_raw_addrs: dict of donor_upper → set of (street_raw, city, state) for tiebreaker
    """
    def _norm_street(street):
        if pd.isna(street) or not str(street).strip():
            return ""
        s = str(street).upper().strip()
        s = re.sub(r"[.\-,;:#'\"]", " ", s)
        s = re.sub(r"\bSTREET\b", "ST", s)
        s = re.sub(r"\bAVENUE\b", "AVE", s)
        s = re.sub(r"\bBOULEVARD\b", "BLVD", s)
        s = re.sub(r"\bDRIVE\b", "DR", s)
        s = re.sub(r"\bLANE\b", "LN", s)
        s = re.sub(r"\bROAD\b", "RD", s)
        s = re.sub(r"\bSUITE\b", "STE", s)
        s = re.sub(r"\bFLOOR\b", "FL", s)
        s = re.sub(r"\bAPARTMENT\b", "APT", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    addr_to_donors = defaultdict(set)
    donor_to_addrs = defaultdict(set)
    # Only index non-individual (company) donors
    company_df = _df[_df['is_company'] == True]
    for _, row in company_df[["donor", "street", "state", "zip_code"]].drop_duplicates().iterrows():
        donor = row["donor"]
        street_n = _norm_street(row.get("street", ""))
        state = str(row.get("state", "")).upper().strip() if pd.notna(row.get("state")) else ""
        zip5 = str(row.get("zip_code", "")).strip()[:5] if pd.notna(row.get("zip_code")) else ""
        if street_n and state and len(street_n) > 5:
            key = (street_n, state, zip5)
            addr_to_donors[key].add(donor)
            donor_to_addrs[donor].add(key)

    # Build per-donor raw address lookup for tiebreaker matching
    donor_raw_addrs = defaultdict(set)
    for _, row in _df[['donor', 'street', 'city', 'state']].drop_duplicates().iterrows():
        donor_raw_addrs[str(row['donor']).upper()].add((
            str(row.get('street', '') or ''),
            str(row.get('city', '') or ''),
            str(row.get('state', '') or ''),
        ))

    return dict(addr_to_donors), dict(donor_to_addrs), dict(donor_raw_addrs)


# ---------------------------------------------------------------------------
# Problem 3 — three-tier donor grouping
#   Tier 1   : NAME_ALIAS_GROUPS (human alias table)           -- Strategy 0
#   Tier 1.5 : donor_groups.json (human/QC-validated groupings) -- Strategy 0.5
#   Tier 3   : fuzzy / prefix / address variant matching        -- Strategies 1-4
# (Tier 2, Bonica CID grouping, is not available: the IRS POFD build carries no
#  bonica.cid column, so there is nothing to group on. Documented in README.)
# ---------------------------------------------------------------------------
def build_donor_clusters(_unique_donors, _addr_to_donors, _donor_to_addrs,
                         _donor_raw_addrs, _donor_groups_map=None):
    """Cluster donors. Returns dict mapping each raw donor name -> canonical
    cluster name. Uses union-find across the tiers described above."""
    from rapidfuzz import fuzz as _fuzz

    # Canonical form for each donor
    raw_to_canonical = {}
    for donor in _unique_donors:
        canonical = no_the(strip_legal(expand_abbrevs(normalize_donor(donor))))
        if not canonical:
            canonical = normalize_donor(donor)
        raw_to_canonical[donor] = canonical

    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Tier 1 (Strategy 0): NAME_ALIAS_GROUPS
    alias_group_reps = {}
    for donor in _unique_donors:
        canon = raw_to_canonical[donor]
        gid = _alias_lookup.get(canon)
        if gid is None:
            for alt in [
                no_the(strip_legal(normalize_donor(donor))),
                no_the(strip_legal(contract_abbrevs(normalize_donor(donor)))),
                normalize_donor(donor),
                strip_legal(normalize_donor(donor)),
            ]:
                gid = _alias_lookup.get(alt)
                if gid is not None:
                    break
        if gid is not None:
            if gid in alias_group_reps:
                union(canon, alias_group_reps[gid])
            else:
                alias_group_reps[gid] = canon

    # Tier 1.5 (Strategy 0.5): donor_groups.json human-validated groupings.
    # Union every donor sharing the same pre-computed donor_group label.
    if _donor_groups_map:
        dg_reps = {}
        for donor in _unique_donors:
            g = _donor_groups_map.get(str(donor).upper())
            if not g:
                continue
            canon = raw_to_canonical[donor]
            if g in dg_reps:
                union(canon, dg_reps[g])
            else:
                dg_reps[g] = canon

    # Tier 3 (Strategy 2): Prefix match on canonical forms
    canonicals = sorted(set(raw_to_canonical.values()))
    for i, c1 in enumerate(canonicals):
        c1_len = len(c1)
        if c1_len < 7:
            continue
        for j in range(i + 1, len(canonicals)):
            c2 = canonicals[j]
            if len(c2) < 7:
                continue
            shorter, longer = (c1, c2) if c1_len <= len(c2) else (c2, c1)
            s_words = len(shorter.split())
            if longer.startswith(shorter):
                if s_words >= 2 and len(shorter) >= 10:
                    union(c1, c2)
                elif s_words == 1 and len(shorter) >= 7 and (longer == shorter or longer.startswith(shorter + " ")):
                    union(c1, c2)
            elif not c2.startswith(c1[:3]):
                break

    # Reverse lookup for address tiebreaker
    canonical_to_raw = defaultdict(set)
    for donor, canon in raw_to_canonical.items():
        canonical_to_raw[canon].add(donor)

    def donors_share_address(canon1, canon2):
        for d1 in canonical_to_raw.get(canon1, set()):
            for addr1 in _donor_raw_addrs.get(d1.upper(), set()):
                for d2 in canonical_to_raw.get(canon2, set()):
                    for addr2 in _donor_raw_addrs.get(d2.upper(), set()):
                        result = addresses_compatible(
                            addr1[0], addr1[1], addr1[2],
                            addr2[0], addr2[1], addr2[2])
                        if result is True:
                            return True
                        if result is False:
                            return False
        return None

    # Tier 3 (Strategies 3+4): Fuzzy match with address tiebreaker
    prefix_blocks = defaultdict(list)
    for canon in canonicals:
        if len(canon) >= 15 and len(canon.split()) >= 2:
            prefix_blocks[canon[:5]].append(canon)

    for block in prefix_blocks.values():
        for i in range(len(block)):
            for j in range(i + 1, len(block)):
                if find(block[i]) == find(block[j]):
                    continue
                min_len = min(len(block[i]), len(block[j]))
                base_threshold = 92 if min_len < 20 else 90
                score = _fuzz.ratio(block[i], block[j])
                if score >= base_threshold:
                    union(block[i], block[j])
                elif score >= (base_threshold - 5):
                    if donors_share_address(block[i], block[j]) is True:
                        union(block[i], block[j])

    # Final mapping: raw donor -> shortest canonical in its cluster
    all_canonicals = sorted(set(raw_to_canonical.values()))
    root_groups = defaultdict(list)
    for canon in all_canonicals:
        root_groups[find(canon)].append(canon)
    canon_to_cluster = {}
    for root, members in root_groups.items():
        cluster_name = min(members, key=len)
        for m in members:
            canon_to_cluster[m] = cluster_name

    return {donor: canon_to_cluster.get(raw_to_canonical[donor], raw_to_canonical[donor])
            for donor in _unique_donors}
