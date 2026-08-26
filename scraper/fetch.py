#!/usr/bin/env python3
"""
DeKalb County, Georgia -- Motivated Seller Lead Scraper
=======================================================

Collects newly recorded / distressed-property public records for DeKalb County GA,
matches them to the official DeKalb ArcGIS parcel dataset (owner + site address +
mailing address), consolidates multiple distress signals onto a single property,
scores motivation, and writes JSON + a GoHighLevel-ready CSV.

SOURCES (all verified reachable as of build date)
-------------------------------------------------
1. Clerk of Superior Court land records -- Pioneer "LandmarkWeb" 1.5.89.0
   https://deeds.dekalbcountyga.gov/LandmarkWeb
   JS application. Driven with Playwright. The underlying search XHR is
   discovered at runtime by network interception (never hard-coded/fabricated).

2. DeKalb County Tax Parcels -- ArcGIS FeatureServer (REST, no browser needed)
   https://dcgis.dekalbcountyga.gov/hosted/rest/services/Tax_Parcels/FeatureServer/0
   MaxRecordCount = 2000, supportsPagination = true. Field names below were read
   off the live service definition.

3. Foreclosure / probate / tax-sale legal advertisements
   https://www.georgiapublicnotice.com  (Georgia Press Association, free)
   Searchable by county + category + date range. The Champion (DeKalb's official
   legal organ per O.C.G.A. 9-13-140) publishes into this database.
   Fallback: https://www.dekalblegalnotices.com weekly legal-section PDFs.

4. DeKalb Tax Commissioner tax sale listing (structured table WITH parcel IDs)
   https://publicaccess.dekalbtaxga.gov  (formerly publicaccess.dekalbtax.org)

Run:            python scraper/fetch.py
Debug headful:  HEADLESS=false python scraper/fetch.py
Selector recon: LANDMARK_DISCOVERY=1 HEADLESS=false python scraper/fetch.py

Nothing in this file bypasses a CAPTCHA, a login, a paywall, or a robots
restriction. Every source is public, rate limited, and retried politely.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup

try:
    from dateutil import parser as dateparser
except ImportError:  # pragma: no cover
    dateparser = None

# Playwright is imported lazily inside the async scrapers so that the parcel /
# static-page half of the program still runs on a machine without browsers.

# =============================================================================
# CONFIGURATION
# =============================================================================

COUNTY = "DeKalb"
STATE = "Georgia"
STATE_ABBR = "GA"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

CLERK_URL = "https://deeds.dekalbcountyga.gov/LandmarkWeb"
PARCEL_LAYER = "https://dcgis.dekalbcountyga.gov/hosted/rest/services/Tax_Parcels/FeatureServer/0"
PARCEL_API = PARCEL_LAYER + "/query"
LEGAL_NOTICE_SEARCH_URL = "https://www.georgiapublicnotice.com/search.aspx"
LEGAL_NOTICE_FALLBACK_URL = "https://www.dekalblegalnotices.com/"
TAX_SALE_URL = "https://dekalbtaxga.gov/property-tax/delinquent-taxes/"
TAX_SALE_LISTING_URLS = [
    "https://publicaccess.dekalbtaxga.gov/forms/htmlframe.aspx?mode=content%2Fsearch%2Ftax_sale_listing.html",
    "https://publicaccess.dekalbtax.org/forms/htmlframe.aspx?mode=content%2Fsearch%2Ftax_sale_listing.html",
]

# --- runtime knobs -----------------------------------------------------------
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() not in ("false", "0", "no")
LANDMARK_DISCOVERY = os.getenv("LANDMARK_DISCOVERY", "").strip().lower() in ("1", "true", "yes")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "45"))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "60000"))
POLITE_DELAY = float(os.getenv("POLITE_DELAY", "1.2"))          # seconds between page hits
PARCEL_PAGE_SIZE = int(os.getenv("PARCEL_PAGE_SIZE", "2000"))    # service MaxRecordCount
PARCEL_CACHE_HOURS = int(os.getenv("PARCEL_CACHE_HOURS", "72"))
MAX_LANDMARK_PAGES = int(os.getenv("MAX_LANDMARK_PAGES", "40"))
MAX_NOTICE_DETAILS = int(os.getenv("MAX_NOTICE_DETAILS", "250"))
SKIP_SOURCES = {s.strip().upper() for s in os.getenv("SKIP_SOURCES", "").split(",") if s.strip()}

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
CONTACT_NOTE = os.getenv("CONTACT_NOTE", "RevampRealtyGroup-LeadResearch")

# --- paths -------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
DASH_DIR = REPO_ROOT / "dashboard"
CACHE_DIR = DATA_DIR / ".cache"
for _d in (DATA_DIR, DASH_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

RECORDS_JSON_PATHS = [DASH_DIR / "records.json", DATA_DIR / "records.json"]
GHL_CSV_PATHS = [DATA_DIR / "ghl_leads.csv", DASH_DIR / "ghl_leads.csv"]
SEEN_STATE_PATH = DATA_DIR / "seen_documents.json"
UNKNOWN_DOCTYPES_PATH = DATA_DIR / "unmapped_doc_types.json"
DISCOVERY_PATH = DATA_DIR / "landmark_discovery.json"
PARCEL_CACHE_PATH = CACHE_DIR / "parcels.json"

# =============================================================================
# LANDMARKWEB SELECTOR CONFIG
# =============================================================================
# LandmarkWeb is a vendor product (Pioneer Technology Group). Counties upgrade it
# on their own schedule, so every selector below is a *candidate list* tried in
# order, and every one can be overridden by an environment variable without
# editing code. If DeKalb re-skins the portal, run with LANDMARK_DISCOVERY=1 and
# the recon dump lands in data/landmark_discovery.json.

# =============================================================================
# DOCUMENT TYPE MAP
# =============================================================================
# NOTE: these are matched case-insensitively as *substrings* against whatever
# DeKalb's LandmarkWeb actually returns in its document-type column. They are
# Georgia terms, not Florida abbreviations. Anything that fails to map is written
# to data/unmapped_doc_types.json so the list can be grown from real data.

DOCUMENT_TYPE_MAP: Dict[str, List[str]] = {
    "RELLP": [
        "RELEASE OF LIS PENDENS", "RELEASE LIS PENDENS", "CANCELLATION OF LIS PENDENS",
        "CANCEL LIS PENDENS", "RELLP",
    ],
    "LP": [
        "LIS PENDENS", "NOTICE OF LIS PENDENS", "NOTICE LIS PENDENS",
    ],
    "FC": [
        "NOTICE OF SALE UNDER POWER", "SALE UNDER POWER", "FORECLOSURE",
        "DEED UNDER POWER", "FORECLOSURE DEED", "NOTICE OF FORECLOSURE",
    ],
    "TAX": [
        "TAX SALE", "TAX DEED", "TAX FI FA", "TAX FIFA", "TAX EXECUTION",
        "TAX COMMISSIONER EXECUTION",
    ],
    "TAXLIEN": [
        "FEDERAL TAX LIEN", "NOTICE OF FEDERAL TAX LIEN", "IRS LIEN", "IRS TAX LIEN",
        "STATE TAX LIEN", "CORPORATE TAX LIEN", "CORP TAX LIEN", "FEDERAL LIEN",
        "GA DEPARTMENT OF REVENUE", "DEPARTMENT OF REVENUE LIEN",
    ],
    "JUD": [
        "JUDGMENT", "CERTIFIED JUDGMENT", "DOMESTIC JUDGMENT", "FOREIGN JUDGMENT",
        "FI FA", "FIFA", "FI. FA.", "WRIT OF FIERI FACIAS", "FIERI FACIAS",
        "GENERAL EXECUTION", "GENERAL EXECUTION DOCKET",
    ],
    "MECH": [
        "MECHANIC LIEN", "MECHANICS LIEN", "MECHANIC'S LIEN",
        "MATERIALMAN", "MATERIALMEN", "CLAIM OF LIEN",
    ],
    "HOA": [
        "HOA LIEN", "HOMEOWNER", "HOMEOWNERS ASSOCIATION LIEN",
        "CONDOMINIUM ASSOCIATION LIEN", "CONDO LIEN", "ASSOCIATION LIEN",
        "PROPERTY OWNERS ASSOCIATION",
    ],
    "MED": [
        "MEDICAID LIEN", "MEDICAID", "DEPARTMENT OF COMMUNITY HEALTH",
        "HOSPITAL LIEN", "GOVERNMENT LIEN",
    ],
    "PRO": [
        "EXECUTOR", "EXECUTRIX", "ADMINISTRATOR", "ADMINISTRATRIX",
        "YEAR'S SUPPORT", "YEARS SUPPORT", "ASSENT TO DEVISE", "AFFIDAVIT OF HEIRSHIP",
        "HEIRSHIP", "ESTATE OF", "LETTERS TESTAMENTARY", "PROBATE",
    ],
    "NOC": [
        "NOTICE OF COMMENCEMENT",
    ],
    "LIEN": [
        "LIEN",  # deliberately last-resort: only reached if nothing above matched
    ],
}

# Order matters. More specific categories must be tested before generic ones.
CATEGORY_ORDER = ["RELLP", "LP", "FC", "TAX", "TAXLIEN", "MECH", "HOA", "MED", "PRO", "NOC", "JUD", "LIEN"]

CAT_LABELS = {
    "LP": "Lis Pendens",
    "FC": "Pre-Foreclosure",
    "TAX": "Tax Delinquent / Tax Sale",
    "JUD": "Judgment",
    "TAXLIEN": "Tax Lien",
    "LIEN": "Lien",
    "MECH": "Mechanic / Materialman Lien",
    "HOA": "HOA / Condo Lien",
    "MED": "Government / Medicaid Lien",
    "PRO": "Probate / Estate",
    "NOC": "Notice of Commencement",
    "RELLP": "Released Lis Pendens",
    "UNK": "Other Recorded Document",
}

CAT_FLAGS = {
    "LP": "Lis pendens",
    "FC": "Pre-foreclosure",
    "TAX": "Tax delinquent",
    "JUD": "Judgment lien",
    "TAXLIEN": "Tax lien",
    "LIEN": "Lien",
    "MECH": "Mechanic lien",
    "HOA": "HOA lien",
    "MED": "Government lien",
    "PRO": "Probate / estate",
}

# Categories that count as "real" distress for the stacking bonus.
DISTRESS_CATEGORIES = {"LP", "FC", "TAX", "JUD", "TAXLIEN", "LIEN", "MECH", "HOA", "MED", "PRO"}

# Documents that cancel out an earlier distress signal.
RELEASE_TERMS = [
    "RELEASE OF LIEN", "RELEASE OF LIS PENDENS", "CANCELLATION", "CANCEL",
    "SATISFACTION", "WITHDRAWAL", "RELEASE OF JUDGMENT", "RELEASE OF FIFA",
]

ENTITY_TOKENS = {
    "LLC", "LLLP", "LLP", "LP", "INC", "CORP", "CORPORATION", "COMPANY", "CO",
    "TRUST", "TRUSTEE", "ESTATE", "HOLDINGS", "HOLDING", "PROPERTIES", "PROPERTY",
    "INVESTMENTS", "INVESTMENT", "PARTNERS", "PARTNERSHIP", "LTD", "ASSOCIATION",
    "ASSOC", "BANK", "NA", "FUND", "GROUP", "ENTERPRISES", "VENTURES", "REALTY",
    "HOMES", "CAPITAL", "MANAGEMENT", "SERVICES", "CHURCH", "CITY", "COUNTY",
    "AUTHORITY", "DEPARTMENT", "STATE", "UNITED", "USA", "COMMISSION",
}

NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "MD", "DDS", "ESQ"}

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-7s %(name)-14s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("dekalb")
logging.getLogger("urllib3").setLevel(logging.WARNING)

SOURCE_REPORT: Dict[str, Dict[str, Any]] = {}


def record_source_result(name: str, ok: bool, count: int = 0, error: str = "") -> None:
    """
    A source that ran without raising but returned nothing has NOT worked, and
    reporting it as "working" hides the only failure that matters. Anything
    that yields zero records is recorded as a problem unless it was
    deliberately skipped.
    """
    if ok and count == 0 and "skip" not in error.lower() and name not in ("browser",):
        ok = False
        error = error or "ran but found no records"
    SOURCE_REPORT[name] = {"ok": ok, "count": count, "error": error[:400]}


# =============================================================================
# GENERIC UTILITIES
# =============================================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Scraper-Purpose": CONTACT_NOTE,
    })
    return s


def retry(times: int = MAX_RETRIES, base_delay: float = 1.5, label: str = ""):
    """Decorator: retry a *sync* callable with exponential backoff + jitter."""
    def outer(fn):
        def inner(*args, **kwargs):
            last = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - deliberate catch-all
                    last = exc
                    if attempt == times:
                        break
                    wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.6)
                    log.warning("%s attempt %d/%d failed (%s); retrying in %.1fs",
                                label or fn.__name__, attempt, times, exc, wait)
                    time.sleep(wait)
            raise last  # type: ignore[misc]
        return inner
    return outer


async def aretry(coro_fn, *args, times: int = MAX_RETRIES, base_delay: float = 2.0,
                 label: str = "", **kwargs):
    """Retry an *async* callable with exponential backoff + jitter."""
    last: Optional[Exception] = None
    for attempt in range(1, times + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == times:
                break
            wait = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.8)
            log.warning("%s attempt %d/%d failed (%s); retrying in %.1fs",
                        label or getattr(coro_fn, "__name__", "task"),
                        attempt, times, exc, wait)
            await asyncio.sleep(wait)
    raise last  # type: ignore[misc]


def safe_write_json(path: Path, payload: Any) -> None:
    """Serialize first, then atomically replace. A crash mid-write can't corrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, path)


def safe_read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read %s (%s); using default", path, exc)
    return default


def parse_date(value: Any) -> Optional[datetime]:
    """Very forgiving date parser. Returns tz-naive datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        # ArcGIS returns epoch milliseconds
        try:
            ms = float(value)
            if ms > 1e11:
                ms /= 1000.0
            return datetime.utcfromtimestamp(ms)
        except Exception:  # noqa: BLE001
            return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d-%b-%Y", "%d-%b-%y",
                "%b %d, %Y", "%B %d, %Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 4].strip(), fmt)
        except ValueError:
            continue
    if dateparser is not None:
        try:
            return dateparser.parse(text, fuzzy=True, dayfirst=False).replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            pass
    return None


def fmt_date(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


MONEY_RE = re.compile(r"\$\s*([\d]{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def parse_money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    text = str(value)
    m = MONEY_RE.search(text)
    if not m:
        text2 = re.sub(r"[^\d.]", "", text)
        try:
            v = float(text2)
            return v if v > 0 else None
        except ValueError:
            return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def sha_key(*parts: Any) -> str:
    raw = "||".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.replace("\xa0", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# =============================================================================
# NAME NORMALIZATION + VARIANT GENERATION
# =============================================================================

def normalize_name(raw: Any) -> str:
    """Uppercase, strip punctuation noise, collapse whitespace. Keeps entity words."""
    text = clean_text(raw).upper()
    if not text:
        return ""
    text = text.replace("&", " AND ")
    text = re.sub(r"\bL\.?\s?L\.?\s?C\.?\b", "LLC", text)
    text = re.sub(r"\bL\.?\s?P\.?\b", "LP", text)
    text = re.sub(r"\bINC\.?\b", "INC", text)
    text = text.replace(".", " ")
    text = text.replace(",", " , ")
    text = re.sub(r"[^\w,\s'\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*,\s*", ", ", text)
    return text.strip(" ,")


def is_entity(name: str) -> bool:
    """True when the owner is a company/trust/government rather than a person."""
    tokens = set(re.split(r"[\s,]+", normalize_name(name)))
    return bool(tokens & ENTITY_TOKENS)


def name_tokens(name: str) -> List[str]:
    norm = normalize_name(name)
    toks = [t for t in re.split(r"[\s,]+", norm) if t]
    return [t for t in toks if t not in NAME_SUFFIXES and len(t) > 0]


def name_variants(raw: Any) -> List[str]:
    """
    Produce the lookup strings this name could be indexed under.

    Deeds index people as "LAST FIRST MIDDLE"; the parcel roll may hold
    "LAST, FIRST M" or "FIRST LAST". Entities are never permuted -- taking
    "REVAMP HOME BUYERS LLC" apart would be destructive.
    """
    norm = normalize_name(raw)
    if not norm:
        return []
    out = {norm, norm.replace(",", "").strip()}
    if is_entity(norm):
        return sorted(v for v in out if v)

    toks = name_tokens(norm)
    if not toks:
        return sorted(out)

    if len(toks) == 2:
        a, b = toks
        out |= {f"{a} {b}", f"{b} {a}", f"{a}, {b}", f"{b}, {a}"}
    elif len(toks) == 3:
        # Every ordering of all three tokens, plus every ordering of the tokens
        # that are not bare middle initials. Never emit a fragment that drops a
        # real name part -- "A SMITH" as an index key is an invitation to
        # attach a lien to a stranger.
        from itertools import permutations
        for perm in permutations(toks):
            out.add(" ".join(perm))
            out.add(f"{perm[0]}, {' '.join(perm[1:])}")
        substantial = [t for t in toks if len(t) > 1]
        if len(substantial) == 2:
            x, y = substantial
            out |= {f"{x} {y}", f"{y} {x}", f"{x}, {y}", f"{y}, {x}"}
    elif len(toks) > 3:
        first, last = toks[0], toks[-1]
        out |= {" ".join(toks), f"{last} {first}", f"{first} {last}",
                f"{last}, {first}", f"{' '.join(toks[1:])} {first}"}

    return sorted(v.strip(" ,") for v in out if v.strip(" ,"))


def token_signature(raw: Any) -> str:
    """
    Order-independent key. "SMITH JOHN A" and "JOHN A SMITH" both collapse to
    "A JOHN SMITH", which is what makes cross-source matching work without
    resorting to fuzzy string distance (and its false positives).
    """
    toks = name_tokens(raw)
    if not toks:
        return ""
    if is_entity(raw):
        return " ".join(toks)
    return " ".join(sorted(toks))


def core_signature(raw: Any) -> str:
    """Signature ignoring single-letter middle initials, for LAST+FIRST matching."""
    toks = [t for t in name_tokens(raw) if len(t) > 1]
    if not toks:
        return ""
    if is_entity(raw):
        return " ".join(toks)
    return " ".join(sorted(toks))


def split_person_name(raw: str) -> Tuple[str, str]:
    """
    Best-effort First / Last split for the GHL CSV.
    Entities are returned whole in the first slot with an empty last name.
    """
    norm = normalize_name(raw)
    if not norm:
        return "", ""
    if is_entity(norm):
        return clean_text(raw), ""

    if "," in norm:
        left, _, right = norm.partition(",")
        last = left.strip().title()
        first = " ".join(right.split()).title()
        return first, last

    toks = [t for t in norm.split() if t]
    toks_ns = [t for t in toks if t not in NAME_SUFFIXES]
    if len(toks_ns) == 1:
        return toks_ns[0].title(), ""
    if len(toks_ns) == 2:
        # Deed indexes are overwhelmingly LAST FIRST.
        return toks_ns[1].title(), toks_ns[0].title()
    # LAST FIRST MIDDLE -> First="FIRST MIDDLE", Last="LAST"
    return " ".join(toks_ns[1:]).title(), toks_ns[0].title()


# =============================================================================
# ADDRESS NORMALIZATION
# =============================================================================

STREET_ABBR = {
    "STREET": "ST", "ROAD": "RD", "DRIVE": "DR", "AVENUE": "AVE", "LANE": "LN",
    "COURT": "CT", "CIRCLE": "CIR", "BOULEVARD": "BLVD", "PLACE": "PL",
    "TERRACE": "TER", "TRAIL": "TRL", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "SQUARE": "SQ", "POINT": "PT", "CROSSING": "XING", "RUN": "RUN", "WAY": "WAY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT", "BUILDING": "BLDG",
}

PO_BOX_RE = re.compile(r"\bP\.?\s?O\.?\s?BOX\b", re.I)


DIRECTIONALS = {"N", "S", "E", "W"}


def normalize_address(raw: Any) -> str:
    text = clean_text(raw).upper()
    if not text:
        return ""
    # Drop periods *without* inserting a space, so "N.E." collapses to "NE"
    # rather than splitting into two tokens. Then blank out other punctuation.
    text = text.replace(".", "")
    text = re.sub(r"[^\w\s]", " ", text)
    parts = [STREET_ABBR.get(p, p) for p in text.split()]

    # Re-join directionals that were already space-separated in the source
    # ("PEACHTREE ST N E" -> "PEACHTREE ST NE").
    merged: List[str] = []
    for tok in parts:
        if (merged and tok in DIRECTIONALS and merged[-1] in DIRECTIONALS
                and len(merged[-1]) == 1):
            merged[-1] = merged[-1] + tok
        else:
            merged.append(tok)
    return re.sub(r"\s+", " ", " ".join(merged)).strip()


def address_key(street: Any, zip_code: Any = "") -> str:
    """Match key: normalized street line, optionally salted with the 5-digit ZIP."""
    s = normalize_address(street)
    if not s:
        return ""
    z = re.sub(r"\D", "", str(zip_code or ""))[:5]
    return f"{s}|{z}" if z else s


def is_po_box(raw: Any) -> bool:
    return bool(PO_BOX_RE.search(clean_text(raw)))


ADDRESS_IN_TEXT_RE = re.compile(
    r"\b(\d{1,6}[A-Z]?\s+(?:[A-Z0-9'.\-]+\s+){0,5}?"
    r"(?:ST|STREET|RD|ROAD|DR|DRIVE|AVE|AVENUE|LN|LANE|CT|COURT|CIR|CIRCLE|"
    r"BLVD|BOULEVARD|PL|PLACE|TER|TERRACE|TRL|TRAIL|PKWY|PARKWAY|HWY|HIGHWAY|"
    r"WAY|RUN|XING|CROSSING|SQ|SQUARE|PT|POINT|PATH|BEND|RIDGE|CHASE|WALK|"
    r"CV|COVE|GLN|GLEN|LOOP|MNR|MANOR|OVERLOOK|PASS|VIEW|VLG|VILLAGE)"
    r"(?:\s+(?:NE|NW|SE|SW|N|S|E|W))?)\b",
    re.I,
)


def extract_address_from_text(text: str) -> str:
    """Pull the most plausible street address out of a block of notice prose."""
    if not text:
        return ""
    candidates = ADDRESS_IN_TEXT_RE.findall(clean_text(text).upper())
    if not candidates:
        return ""
    # The longest hit is nearly always the full street line rather than a fragment.
    return max(candidates, key=len).strip()


ZIP_RE = re.compile(r"\b(3\d{4})(?:-\d{4})?\b")


def extract_zip_from_text(text: str) -> str:
    m = ZIP_RE.search(text or "")
    return m.group(1) if m else ""


# =============================================================================
# BROWSER PREFLIGHT / SELF-REPAIR
# =============================================================================
# Installing the `playwright` pip package does NOT install the Chromium binary
# it drives -- that is a separate download. When the binary is missing, every
# browser-backed source dies with "Executable doesn't exist".
#
# Retrying that is pointless: a missing file will not appear on the second
# attempt. So instead the scraper detects that specific failure, installs the
# browser itself, and carries on. If the install cannot happen (no network, no
# disk, locked-down runner), the browser sources are switched off for the run
# and the HTTP-only sources still produce a lead list.

BROWSER_AVAILABLE: Optional[bool] = None   # None = not yet checked
BROWSER_ERROR: str = ""

_MISSING_BROWSER_SIGNS = (
    "executable doesn't exist",
    "executable does not exist",
    "please run the following command",
    "playwright install",
    "looks like playwright was just installed",
    "browsertype.launch",
    "no such file or directory",
)


def _is_missing_browser(exc: Exception) -> bool:
    """Distinguish 'the browser isn't installed' from 'the website is down'."""
    return any(sign in str(exc).lower() for sign in _MISSING_BROWSER_SIGNS)


def _run_install(cmd: List[str], timeout: int = 900) -> Tuple[bool, str]:
    import subprocess
    log.info("  running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "the download timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not start the installer: {exc}"
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout or "").strip()


def install_chromium() -> bool:
    """
    Install the Chromium binary. Returns True on success.

    Two attempts, because they fail for different reasons:

      1. `--with-deps` also installs OS libraries through apt. That is the more
         complete install, but apt can fail for reasons that have nothing to do
         with the browser -- one unrelated broken package repository on the
         machine is enough to sink it.
      2. Plain `playwright install chromium` only downloads the browser. On a
         GitHub Actions runner the OS libraries are already present, so this
         very often succeeds after step 1 failed.

    Trying only the first would throw away a working install over an unrelated
    apt problem.
    """
    log.warning("Chromium is not installed. Installing it now -- a one-time "
                "download of roughly 150 MB, usually a couple of minutes.")

    is_root = getattr(os, "geteuid", lambda: 1)() == 0
    attempts: List[Tuple[str, List[str]]] = []
    if is_root:
        attempts.append(("with system libraries",
                         [sys.executable, "-m", "playwright", "install",
                          "--with-deps", "chromium"]))
    attempts.append(("browser only",
                     [sys.executable, "-m", "playwright", "install", "chromium"]))

    for label, cmd in attempts:
        ok, err = _run_install(cmd)
        if ok:
            log.info("  Chromium installed successfully (%s)", label)
            return True
        log.warning("  install attempt (%s) failed", label)
        for line in err.splitlines()[-4:]:
            log.warning("    %s", line[:200])
        if "apt" in err.lower() or "deb" in err.lower():
            log.info("  that failure came from the system package manager, not "
                     "the browser -- retrying with the browser download only")

    log.error("  every install attempt failed")
    if not is_root:
        log.error("  on a personal machine, run this once by hand:")
        log.error("    python -m playwright install --with-deps chromium")
    return False


async def ensure_browser() -> bool:
    """
    Confirm a browser can actually start, repairing the install once if needed.
    The result is cached, so the check costs a second or two per run, not per
    source. Never raises -- callers get a plain True/False.
    """
    global BROWSER_AVAILABLE, BROWSER_ERROR
    if BROWSER_AVAILABLE is not None:
        return BROWSER_AVAILABLE

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        BROWSER_ERROR = ("the playwright package is not installed "
                         "(pip install -r scraper/requirements.txt)")
        log.error("Browser sources disabled: %s", BROWSER_ERROR)
        BROWSER_AVAILABLE = False
        record_source_result("browser", False, 0, BROWSER_ERROR)
        return False

    for attempt in (1, 2):
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
                version = browser.version
                await browser.close()
            log.info("Browser ready: Chromium %s", version)
            BROWSER_AVAILABLE = True
            record_source_result("browser", True, 0)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and _is_missing_browser(exc):
                if install_chromium():
                    continue          # installed -- try launching once more
                BROWSER_ERROR = "Chromium could not be installed"
            else:
                BROWSER_ERROR = f"Chromium would not start: {str(exc)[:200]}"
            break

    log.error("Browser sources disabled: %s", BROWSER_ERROR)
    log.warning("The run continues without them. Parcel data and any source "
                "reachable over plain HTTP still work, so you will still get a "
                "lead file -- it will just be missing the clerk portal and the "
                "legal-notice site.")
    BROWSER_AVAILABLE = False
    record_source_result("browser", False, 0, BROWSER_ERROR)
    return False


# =============================================================================
# SOURCE 1: DEKALB ARCGIS TAX PARCELS
# =============================================================================
# Field names below were read directly off the live layer definition at
# https://dcgis.dekalbcountyga.gov/hosted/rest/services/Tax_Parcels/FeatureServer/0
# The layer reports MaxRecordCount=2000 and supportsPagination=true.

PARCEL_FIELDS = [
    "PARCELID", "LOWPARCELID",
    "OWNERNME1", "OWNERNME2",
    "SITEADDRESS", "ADDRESS_NUMBER", "FULL_STREET_NAME", "UNIT_TYPE", "UNIT_NO",
    "CITY", "STATE", "ZIP",
    "PSTLADDRESS", "PSTLCITY", "PSTLSTATE", "PSTLZIP5", "PSTLZIP4",
    "PRPRTYDSCRP", "CLASSCD", "CLASSDSCRP", "USECD", "USEDSCRP",
    "TOTAPR1", "LASTUPDATE",
]


class ParcelIndex:
    """
    Downloads the full DeKalb parcel roll once per run (cached on disk between
    runs) and builds the lookup tables used for address enrichment.

    Lookups, in descending order of trustworthiness:
      by_parcel_id   -- exact, authoritative
      by_address     -- normalized site address (+ZIP)
      by_name_exact  -- normalized owner string as printed on the roll
      by_signature   -- order-independent token signature
      by_core        -- signature with middle initials dropped
    """

    def __init__(self) -> None:
        self.parcels: List[Dict[str, Any]] = []
        self.by_parcel_id: Dict[str, Dict[str, Any]] = {}
        self.by_address: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_name_exact: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_signature: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.by_core: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.loaded_from_cache = False

    # ---------------------------------------------------------------- fetching
    @staticmethod
    @retry(label="arcgis-count")
    def _fetch_count(session: requests.Session) -> int:
        resp = session.get(
            PARCEL_API,
            params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"ArcGIS error: {data['error']}")
        return int(data.get("count", 0))

    @staticmethod
    @retry(label="arcgis-page")
    def _fetch_page(session: requests.Session, offset: int, size: int) -> List[Dict[str, Any]]:
        resp = session.get(
            PARCEL_API,
            params={
                "where": "1=1",
                "outFields": ",".join(PARCEL_FIELDS),
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": size,
                "orderByFields": "OBJECTID ASC",
                "f": "json",
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"ArcGIS error at offset {offset}: {data['error']}")
        return [f.get("attributes", {}) for f in data.get("features", [])]

    def load(self, session: requests.Session) -> None:
        cached = self._load_cache()
        if cached is not None:
            self.parcels = cached
            self.loaded_from_cache = True
            log.info("Parcel cache hit: %d parcels (skipping download)", len(self.parcels))
            self._build_indexes()
            record_source_result("arcgis_parcels", True, len(self.parcels))
            return

        try:
            total = self._fetch_count(session)
            log.info("ArcGIS reports %s DeKalb parcels; downloading attributes only", f"{total:,}")
        except Exception as exc:  # noqa: BLE001
            log.error("Could not read parcel count: %s", exc)
            total = 0

        rows: List[Dict[str, Any]] = []
        offset = 0
        consecutive_failures = 0
        while True:
            try:
                page = self._fetch_page(session, offset, PARCEL_PAGE_SIZE)
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                log.error("Parcel page at offset %d failed permanently: %s", offset, exc)
                if consecutive_failures >= 2:
                    break
                offset += PARCEL_PAGE_SIZE
                continue

            if not page:
                break
            rows.extend(page)
            offset += PARCEL_PAGE_SIZE
            if offset % 20000 == 0:
                log.info("  ... %s parcels downloaded", f"{len(rows):,}")
            if total and offset >= total + PARCEL_PAGE_SIZE:
                break
            if offset > 1_500_000:  # runaway guard
                log.warning("Parcel pagination guard tripped; stopping")
                break
            time.sleep(0.15)

        self.parcels = rows
        log.info("Parcel download complete: %s records", f"{len(rows):,}")
        if rows:
            self._save_cache(rows)
            record_source_result("arcgis_parcels", True, len(rows))
        else:
            record_source_result("arcgis_parcels", False, 0, "no parcels returned")
        self._build_indexes()

    # ------------------------------------------------------------------- cache
    def _load_cache(self) -> Optional[List[Dict[str, Any]]]:
        if os.getenv("PARCEL_CACHE", "1") == "0":
            return None
        blob = safe_read_json(PARCEL_CACHE_PATH)
        if not isinstance(blob, dict):
            return None
        try:
            fetched = datetime.fromisoformat(blob.get("fetched_at", ""))
        except Exception:  # noqa: BLE001
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if utcnow() - fetched > timedelta(hours=PARCEL_CACHE_HOURS):
            return None
        rows = blob.get("parcels")
        return rows if isinstance(rows, list) and rows else None

    def _save_cache(self, rows: List[Dict[str, Any]]) -> None:
        try:
            safe_write_json(PARCEL_CACHE_PATH,
                            {"fetched_at": utcnow().isoformat(), "parcels": rows})
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not write parcel cache: %s", exc)

    # ----------------------------------------------------------------- indexes
    @staticmethod
    def site_address(p: Dict[str, Any]) -> str:
        site = clean_text(p.get("SITEADDRESS"))
        if site and re.search(r"\d", site):
            return site
        num = clean_text(p.get("ADDRESS_NUMBER"))
        street = clean_text(p.get("FULL_STREET_NAME"))
        if not (num or street):
            return ""
        built = " ".join(x for x in (num, street) if x)
        utype = clean_text(p.get("UNIT_TYPE"))
        uno = clean_text(p.get("UNIT_NO"))
        if uno:
            built = f"{built} {utype or 'UNIT'} {uno}".strip()
        return built.strip()

    def _build_indexes(self) -> None:
        for p in self.parcels:
            try:
                pid = clean_text(p.get("PARCELID"))
                low = clean_text(p.get("LOWPARCELID"))
                for key in {normalize_parcel_id(pid), normalize_parcel_id(low)}:
                    if key and key not in self.by_parcel_id:
                        self.by_parcel_id[key] = p

                site = self.site_address(p)
                if site:
                    ak = address_key(site, p.get("ZIP"))
                    if ak:
                        self.by_address[ak].append(p)
                    bare = normalize_address(site)
                    if bare and bare != ak:
                        self.by_address[bare].append(p)

                for owner_field in ("OWNERNME1", "OWNERNME2"):
                    owner = clean_text(p.get(owner_field))
                    if not owner:
                        continue
                    self.by_name_exact[normalize_name(owner)].append(p)
                    for variant in name_variants(owner):
                        self.by_name_exact[variant].append(p)
                    sig = token_signature(owner)
                    if sig:
                        self.by_signature[sig].append(p)
                    core = core_signature(owner)
                    if core and core != sig:
                        self.by_core[core].append(p)
            except Exception as exc:  # noqa: BLE001
                log.debug("Skipping malformed parcel row: %s", exc)

        log.info("Parcel indexes built: %s parcel-ids, %s addresses, %s owner keys",
                 f"{len(self.by_parcel_id):,}", f"{len(self.by_address):,}",
                 f"{len(self.by_name_exact):,}")

    # ---------------------------------------------------------------- matching
    def match(self, parcel_id: str = "", prop_address: str = "",
              owner: str = "", legal: str = "") -> Tuple[Optional[Dict[str, Any]], float, str]:
        """
        Staged match. Returns (parcel, confidence, method).

        Deliberately conservative: an owner key that resolves to more than
        AMBIGUITY_LIMIT parcels is treated as no match rather than guessed,
        because attaching a foreclosure to the wrong house is worse than
        leaving the address blank.
        """
        AMBIGUITY_LIMIT = 3

        # 1. Parcel ID -- authoritative
        pk = normalize_parcel_id(parcel_id)
        if pk and pk in self.by_parcel_id:
            return self.by_parcel_id[pk], 1.0, "parcel_id"

        # 2. Property address printed on the document
        if prop_address:
            for key in (address_key(prop_address), normalize_address(prop_address)):
                if not key:
                    continue
                hits = self.by_address.get(key) or []
                if len(hits) == 1:
                    return hits[0], 0.92, "address"
                if 1 < len(hits) <= AMBIGUITY_LIMIT:
                    return hits[0], 0.72, "address_multi"

        # 3. Exact normalized owner name
        if owner:
            norm = normalize_name(owner)
            hits = self.by_name_exact.get(norm) or []
            hits = _dedupe_parcels(hits)
            if len(hits) == 1:
                return hits[0], 0.85, "owner_exact"
            if 1 < len(hits) <= AMBIGUITY_LIMIT:
                return hits[0], 0.6, "owner_exact_multi"

            # 4. Alternate name orderings / token signature
            sig = token_signature(owner)
            hits = _dedupe_parcels(self.by_signature.get(sig) or [])
            if len(hits) == 1:
                return hits[0], 0.75, "owner_signature"
            if 1 < len(hits) <= AMBIGUITY_LIMIT:
                return hits[0], 0.55, "owner_signature_multi"

            core = core_signature(owner)
            hits = _dedupe_parcels(self.by_core.get(core) or [])
            if len(hits) == 1:
                return hits[0], 0.65, "owner_core"

        # 5. Legal description clue: parcel id embedded in the legal text
        if legal:
            for cand in PARCEL_ID_IN_TEXT_RE.findall(legal.upper()):
                key = normalize_parcel_id(cand)
                if key and key in self.by_parcel_id:
                    return self.by_parcel_id[key], 0.8, "legal_parcel_id"

        return None, 0.0, "none"


def _dedupe_parcels(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for r in rows:
        pid = clean_text(r.get("PARCELID"))
        if pid and pid in seen:
            continue
        seen.add(pid)
        out.append(r)
    return out


# DeKalb parcel IDs look like "15 126 06 011" or "15-126-06-011".
PARCEL_ID_IN_TEXT_RE = re.compile(r"\b(\d{2}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{3})\b")


def normalize_parcel_id(raw: Any) -> str:
    text = clean_text(raw).upper()
    if not text:
        return ""
    return re.sub(r"[^0-9A-Z]", "", text)


# =============================================================================
# SOURCE 2: LANDMARKWEB (Clerk of Superior Court official records)
# =============================================================================

UNMAPPED_DOC_TYPES: set = set()

# Strings LandmarkWeb renders as page furniture. Without this filter the grid
# scrape happily returns column headers and help text as if they were records.
JUNK_ROW_MARKERS = [
    "GRANTEE SUB BLOCK LOT", "GRANTOR GRANTEE", "NO RECORDS", "NO RESULTS",
    "LOADING", "SEARCH RESULTS", "PLEASE WAIT", "ROWS PER PAGE",
    "SHOWING", "PREVIOUS NEXT", "DISCLAIMER", "COPYRIGHT",
]


def categorize(doc_type: str) -> Tuple[str, bool]:
    """Map a raw document-type string to (category, is_release)."""
    text = clean_text(doc_type).upper()
    if not text:
        return "UNK", False

    is_release = any(term in text for term in RELEASE_TERMS)

    for cat in CATEGORY_ORDER:
        for alias in DOCUMENT_TYPE_MAP.get(cat, []):
            if alias in text:
                return cat, is_release
    UNMAPPED_DOC_TYPES.add(text[:120])
    return "UNK", is_release


def looks_like_record(row: Dict[str, Any]) -> bool:
    """Reject page chrome that survived the DOM scrape."""
    blob = " ".join(str(v) for v in row.values() if v).upper()
    if len(blob) < 8:
        return False
    for marker in JUNK_ROW_MARKERS:
        if marker in blob and len(blob) < 160:
            return False
    # A real index row nearly always carries a date or a document number.
    return bool(row.get("doc_num") or row.get("filed"))


# --- flexible field mapping ---------------------------------------------------
# We do not know DeKalb's exact JSON keys or column order in advance, so keys are
# matched by intent. Whatever does not map is preserved in `_raw`.

FIELD_HINTS: Dict[str, List[str]] = {
    "doc_num": ["docnumber", "documentnumber", "docnum", "instrumentnumber",
                "instrument", "cfn", "clerkfilenumber", "recordid", "documentid"],
    "doc_type": ["doctype", "documenttype", "instrumenttype", "type", "kind",
                 "doctypedescription", "documenttypedesc"],
    "filed": ["recorddate", "recordeddate", "filedate", "fileddate", "filingdate",
              "daterecorded", "datefiled", "recdate"],
    "grantor": ["grantor", "directname", "firstparty", "party1", "from", "grantorname"],
    "grantee": ["grantee", "indirectname", "secondparty", "party2", "to", "granteename"],
    "legal": ["legal", "legaldescription", "description", "propertydescription",
              "subdivision", "briefLegal"],
    "amount": ["amount", "consideration", "debtamount", "value", "salesprice",
               "considerationamount"],
    "book": ["book", "bookno", "booknumber"],
    "page": ["page", "pageno", "pagenumber"],
    "parcel_id": ["parcel", "parcelid", "pin", "taxid"],
}


def _hint_lookup(key: str) -> Optional[str]:
    flat = re.sub(r"[^a-z]", "", str(key).lower())
    if not flat:
        return None
    for target, hints in FIELD_HINTS.items():
        for hint in hints:
            h = re.sub(r"[^a-z]", "", hint.lower())
            if flat == h or (len(h) >= 5 and h in flat):
                return target
    return None


def map_landmark_row(raw: Any) -> Optional[Dict[str, Any]]:
    """Turn one JSON object (or one array row) from LandmarkWeb into our shape."""
    row: Dict[str, Any] = {"_raw": raw}

    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (dict, list)):
                continue
            target = _hint_lookup(key)
            if target and not row.get(target):
                row[target] = clean_text(value)
    elif isinstance(raw, (list, tuple)):
        # Positional fallback for DataTables array rows. Order below reflects the
        # column order LandmarkWeb renders by default; adjust from the recon dump
        # if DeKalb reorders its grid.
        order = ["doc_num", "doc_type", "filed", "grantor", "grantee", "book", "page", "legal"]
        for i, value in enumerate(raw[:len(order)]):
            row[order[i]] = clean_text(value)
    else:
        return None

    if not (row.get("doc_num") or row.get("filed")):
        return None
    return row


class LandmarkScraper:
    """
    Self-configuring Playwright driver for DeKalb's LandmarkWeb portal.

    The design goal is that this never needs a human to fix a selector.

    Two facts are verified against the live DeKalb page and used as anchors:
      * the disclaimer's accept control is an <a> whose text is "Accept"
      * the search icons carry real title attributes -- "Filing Date Search",
        "Document Search", etc.

    Everything else is *discovered by experiment* rather than assumed:
      * date boxes are found by typing a date into each candidate input and
        reading the value back; a box that keeps a date-shaped value is a date
        box, whatever it happens to be called
      * the submit control is found by trying each candidate and checking
        whether results actually appeared
      * whatever combination works is written to
        data/landmark_learned_selectors.json and reused on later runs, and
        thrown away automatically the moment it stops working

    So if Pioneer ships a new version and renames every element, the next
    nightly run re-learns the page instead of returning zero rows.
    """

    LEARNED_PATH = DATA_DIR / "landmark_learned_selectors.json"

    # LandmarkWeb's search icons are javascript:void(0) anchors -- clicking them
    # from Playwright reliably does nothing and leaves you on the home page.
    # Every deployment of this vendor product also serves the search form at a
    # real URL, so we navigate there directly. The section name varies, so each
    # candidate is tried and verified by whether date fields actually appear.
    SEARCH_SECTIONS = [
        "searchCriteriaRecordDate",
        "searchCriteriaFilingDate",
        "searchCriteriaDateRange",
        "searchCriteriaDocument",
        "searchCriteriaName",
    ]

    @staticmethod
    def search_url(section: str) -> str:
        return (f"{CLERK_URL.rstrip('/')}/search/index"
                f"?theme=.blue&section={section}&quickSearchSelection=")

    # Kept as a fallback only.
    SEARCH_STRATEGIES = [
        ("filing_date", ["a[title='Filing Date Search']", "a[title*='Filing Date' i]"]),
        ("document", ["a[title='Document Search']"]),
    ]

    def __init__(self, start: datetime, end: datetime) -> None:
        self.start = start
        self.end = end
        self.captured: List[Any] = []
        self.xhr_urls: List[str] = []
        self.learned: Dict[str, Any] = safe_read_json(self.LEARNED_PATH, {}) or {}
        self.notes: List[str] = []

    # ------------------------------------------------------------ interception
    async def _on_response(self, response) -> None:
        try:
            url = response.url
            if "landmarkweb" not in url.lower():
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            body = await response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is not an error
            return

        self.xhr_urls.append(response.url)
        rows = self._extract_rows(body)
        if rows:
            log.info("  captured %d rows from XHR %s", len(rows), response.url.split("?")[0])
            self.captured.extend(rows)

    @staticmethod
    def _extract_rows(body: Any) -> List[Any]:
        """Find the list-of-records inside an arbitrary JSON envelope."""
        if isinstance(body, list):
            return [r for r in body if isinstance(r, (dict, list))]
        if not isinstance(body, dict):
            return []
        for key in ("data", "aaData", "Data", "results", "Results", "rows",
                    "Records", "records", "items", "d"):
            val = body.get(key)
            if isinstance(val, list) and val:
                return [r for r in val if isinstance(r, (dict, list))]
            if isinstance(val, dict):
                nested = LandmarkScraper._extract_rows(val)
                if nested:
                    return nested
        return []

    # ------------------------------------------------------- generic UI helper
    @staticmethod
    async def _try_click(page, selector: str, timeout: int = 6000) -> bool:
        try:
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _click_any(self, page, selectors: Sequence[str], label: str,
                         timeout: int = 6000) -> Optional[str]:
        for sel in selectors:
            if await self._try_click(page, sel, timeout):
                log.info("  %s  [%s]", label, sel)
                await page.wait_for_timeout(800)
                return sel
        return None

    # --------------------------------------------------- step 1: the disclaimer
    # This is the gate everything else depends on. Until it is accepted, all
    # ~17 search inputs sit in the DOM with visibility:hidden, so any probe
    # finds nothing and every later step fails for reasons that look unrelated.
    #
    # LandmarkWeb keeps its own record of whether the disclaimer is done, in a
    # hidden field named idAcceptedDisclaimerOncePerSession. That field is the
    # ground truth: rather than assuming a click landed, we click and then read
    # the site's own flag back.

    ACCEPT_FLAG = "idAcceptedDisclaimerOncePerSession"

    async def _disclaimer_done(self, page) -> Optional[bool]:
        """Read the site's own flag. None when the field isn't on the page."""
        try:
            return await page.evaluate(
                """(id) => {
                    const f = document.getElementById(id);
                    return f ? String(f.value).toLowerCase() === 'true' : null;
                }""", self.ACCEPT_FLAG)
        except Exception:  # noqa: BLE001
            return None

    async def _accept_disclaimer(self, page) -> bool:
        """
        Dismiss the public-records disclaimer, verifying against the site's own
        flag after every attempt. Several click methods are tried because a
        normal Playwright click can be swallowed by a modal that is mid-animation
        or overlaid by a backdrop -- which is what happened on the first runs.
        """
        state = await self._disclaimer_done(page)
        if state is True:
            log.info("  disclaimer already accepted for this session")
            return True
        if state is None:
            log.info("  no disclaimer on this page")
            return True

        # Give a modal that fades in a moment to settle before clicking.
        try:
            await page.wait_for_selector("#idAcceptYes", state="visible", timeout=8000)
        except Exception:  # noqa: BLE001
            pass

        attempts = [
            ("click", lambda: page.click("#idAcceptYes", timeout=5000)),
            ("forced click", lambda: page.click("#idAcceptYes", force=True, timeout=5000)),
            ("javascript click", lambda: page.evaluate(
                "() => { const e = document.getElementById('idAcceptYes');"
                " if (e) e.click(); }")),
            ("dispatched event", lambda: page.evaluate(
                """() => { const e = document.getElementById('idAcceptYes');
                   if (e) e.dispatchEvent(new MouseEvent('click',
                       {bubbles: true, cancelable: true, view: window})); }""")),
            ("text sweep", lambda: page.evaluate(
                r"""() => {
                    const re = /^(i\s+)?(accept|agree|continue)$/i;
                    for (const e of document.querySelectorAll('a,button,input')) {
                        const t = (e.innerText || e.value || '').trim();
                        if (re.test(t)) { e.click(); return t; }
                    }
                    return null;
                }""")),
        ]

        for label, action in attempts:
            try:
                await action()
            except Exception as exc:  # noqa: BLE001
                log.debug("  disclaimer %s failed: %s", label, exc)
                continue
            await page.wait_for_timeout(900)
            if await self._disclaimer_done(page) is True:
                log.info("  disclaimer accepted (%s)", label)
                self.learned["accept_method"] = label
                return True

        # Last resort: set the flag and hide the modal ourselves. This does not
        # bypass anything -- it is the same consent the Accept button records,
        # applied when the button will not take a click in a headless browser.
        try:
            await page.evaluate(
                """(id) => {
                    const f = document.getElementById(id);
                    if (f) f.value = 'true';
                    document.querySelectorAll('.modal, .modal-backdrop').forEach(m => {
                        m.style.display = 'none';
                        m.classList.remove('in', 'show');
                    });
                    document.body.classList.remove('modal-open');
                    document.body.style.overflow = '';
                }""", self.ACCEPT_FLAG)
            await page.wait_for_timeout(600)
            if await self._disclaimer_done(page) is True:
                log.warning("  disclaimer cleared directly (the button would not take a click)")
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("  direct disclaimer clear failed: %s", exc)

        log.error("  could not get past the disclaimer -- the search form will stay hidden")
        self.notes.append("disclaimer could not be accepted")
        return False

    async def _reveal_section(self, page, section: str) -> int:
        """
        Force the requested search panel to show, and report how many inputs
        became visible.

        Clicking Accept normally runs the site's own JS to un-hide the chosen
        panel. When that click has to be simulated, the panel can stay hidden
        even though the modal is gone. The dump gave us the lever: the hidden
        field goToSection carries the section name, which is also the id of the
        panel element -- so the panel can be shown directly.
        """
        try:
            return await page.evaluate(
                """(section) => {
                    const show = (el) => {
                        if (!el) return;
                        el.style.display = '';
                        el.style.visibility = 'visible';
                        el.classList.remove('hidden', 'hide');
                        el.removeAttribute('hidden');
                    };
                    // The panel itself, plus anything wrapping it.
                    let el = document.getElementById(section);
                    if (!el) {
                        el = document.querySelector('[id*="' + section + '"], .' + section);
                    }
                    let node = el;
                    while (node && node !== document.body) { show(node); node = node.parentElement; }

                    // Any leftover modal furniture that would still block clicks.
                    document.querySelectorAll('.modal-backdrop').forEach(m => m.remove());
                    document.body.classList.remove('modal-open');
                    document.body.style.overflow = '';

                    let visible = 0;
                    document.querySelectorAll('input').forEach(e => {
                        if (e.offsetParent !== null) visible++;
                    });
                    return visible;
                }""", section)
        except Exception as exc:  # noqa: BLE001
            log.debug("  reveal failed: %s", exc)
            return 0

    # ---------------------------------------------- step 2: probe the date boxes
    async def _probe_date_inputs(self, page, start_s: str, end_s: str) -> Optional[Tuple[str, str]]:
        """
        Identify the two date boxes empirically. Every visible text input gets a
        date typed into it; the ones that hold onto a date-shaped value are date
        boxes. This works regardless of what the fields are named.
        """
        try:
            handles = await page.evaluate(
                """(probe) => {
                    const out = [];
                    const els = Array.from(document.querySelectorAll(
                        "input[type=text], input[type=date], input[type=tel], input:not([type])"));
                    els.forEach((e, i) => {
                        // Do NOT skip readOnly: calendar-driven date fields are
                        // read-only by design and are exactly what we're after.
                        if (e.offsetParent === null || e.disabled) return;
                        if (!e.id && !e.name) { e.setAttribute('data-probe-id', 'probe' + i); }
                        const before = e.value;
                        try {
                            e.focus();
                            const wasRO = e.readOnly;
                            if (wasRO) e.readOnly = false;
                            e.value = probe;
                            if (wasRO) e.readOnly = true;
                            e.dispatchEvent(new Event('input', {bubbles: true}));
                            e.dispatchEvent(new Event('change', {bubbles: true}));
                        } catch (err) { return; }
                        const kept = e.value;
                        e.value = before;
                        e.dispatchEvent(new Event('input', {bubbles: true}));
                        const attrs = ((e.id||'') + ' ' + (e.name||'') + ' ' +
                                       (e.placeholder||'') + ' ' + (e.className||'')).toLowerCase();
                        out.push({
                            sel: e.id ? ('#' + CSS.escape(e.id))
                                 : (e.name ? ('input[name="' + e.name + '"]')
                                 : ('[data-probe-id="probe' + i + '"]')),
                            kept: kept,
                            attrs: attrs,
                            order: i
                        });
                    });
                    return out;
                }""",
                start_s,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("  date probe failed: %s", exc)
            return None

        date_shape = re.compile(r"\d{1,4}[/\-]\d{1,2}[/\-]\d{2,4}")
        candidates = []
        for h in handles or []:
            score = 0
            if date_shape.search(str(h.get("kept", ""))):
                score += 10          # it actually accepted and kept a date
            attrs = h.get("attrs", "")
            if any(w in attrs for w in ("date", "begin", "start", "from", "end", "to", "thru")):
                score += 5
            if any(w in attrs for w in ("name", "search-term", "keyword", "doctype")):
                score -= 4
            if score > 0:
                candidates.append((score, h["order"], h["sel"], attrs))

        candidates.sort(key=lambda c: (-c[0], c[1]))
        if len(candidates) < 2:
            log.warning("  date probe found %d usable date box(es)", len(candidates))
            return None

        # Prefer an explicit from/to pair when the attributes name them.
        frm = next((c for c in candidates if any(w in c[3] for w in ("from", "begin", "start"))), None)
        to = next((c for c in candidates if any(w in c[3] for w in ("to", "end", "thru"))
                   and (frm is None or c[2] != frm[2])), None)
        if not (frm and to):
            frm, to = candidates[0], candidates[1]
            if frm[1] > to[1]:       # keep DOM order: the earlier box is "from"
                frm, to = to, frm

        log.info("  date boxes identified: from=%s  to=%s", frm[2], to[2])
        return frm[2], to[2]

    @staticmethod
    async def _set_value(page, selector: str, value: str) -> bool:
        """Type into the field; fall back to setting it directly when the field
        is read-only (calendar pickers) and refuses keystrokes."""
        try:
            loc = page.locator(selector).first
            try:
                await loc.click(timeout=3000)
                await loc.fill(value, timeout=3000)
            except Exception:  # noqa: BLE001 - read-only fields reject fill()
                await page.evaluate(
                    """([sel, val]) => {
                        const e = document.querySelector(sel);
                        if (!e) return;
                        const ro = e.readOnly; if (ro) e.readOnly = false;
                        e.value = val;
                        e.dispatchEvent(new Event('input',  {bubbles:true}));
                        e.dispatchEvent(new Event('change', {bubbles:true}));
                        e.dispatchEvent(new Event('blur',   {bubbles:true}));
                        if (ro) e.readOnly = ro;
                    }""", [selector, value])
            await page.wait_for_timeout(150)
            got = await loc.input_value()
            return bool(got and got.strip())
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------- step 3: submit, then verify it
    async def _count_result_rows(self, page) -> int:
        """Rows in the biggest table that actually looks like a result grid."""
        try:
            return await page.evaluate(
                """() => {
                    let best = 0;
                    document.querySelectorAll('table').forEach(t => {
                        let real = 0;
                        t.querySelectorAll('tbody tr').forEach(r => {
                            if (r.querySelectorAll('td').length >= 3) real++;
                        });
                        if (real > best) best = real;
                    });
                    return best;
                }""")
        except Exception:  # noqa: BLE001
            return 0

    async def _submit_and_verify(self, page, date_to_sel: Optional[str]) -> bool:
        """
        Press the search button on the *form*, then prove a search really ran.

        Two lessons are baked in here. First, a bare text match like
        a:has-text('Search') also matches the "Search" item in the site's top
        navigation bar -- clicking that navigates away instead of searching, and
        the page it lands on has a small table that looks enough like results to
        fool a naive check. So candidates are scoped to the form and nav
        elements are excluded.

        Second, the fields are named beginDate-RecordDate / endDate-RecordDate,
        so the submit control almost certainly carries the same -RecordDate
        suffix. That suffix is derived from the date field we already found
        rather than guessed, which keeps working if the section name changes.
        """
        # Derive the section suffix, e.g. "#beginDate-RecordDate" -> "-RecordDate"
        suffix = ""
        if date_to_sel and "-" in date_to_sel:
            suffix = "-" + date_to_sel.split("-", 1)[1].strip("#")

        routes: List[Tuple[str, Any]] = []
        if self.learned.get("submit"):
            routes.append(("learned", self.learned["submit"]))
        if suffix:
            routes += [("suffix", f"#submit{suffix}"),
                       ("suffix", f"#search{suffix}"),
                       ("suffix", f"#btnSearch{suffix}"),
                       ("suffix", f"button[id$='{suffix}']"),
                       ("suffix", f"input[type=submit][id$='{suffix}']"),
                       ("suffix", f"a[id$='{suffix}']")]
        routes += [
            ("css", "#searchButton"),
            ("css", "#btnSearch"),
            ("css", "button#submit-Search"),
            # Scoped to a form, so the navigation bar cannot match.
            ("css", "form button:has-text('Search')"),
            ("css", "form input[type=submit][value*='Search' i]"),
            ("css", "input[type=submit][value*='Search' i]"),
            ("css", "button[type=submit]"),
        ]
        if date_to_sel:
            routes.append(("enter", date_to_sel))

        baseline = await self._count_result_rows(page)
        start_url = page.url

        for kind, target in routes:
            try:
                before_xhr = len(self.captured)
                if kind == "enter":
                    await page.locator(target).first.press("Enter")
                    log.info("  submitted by pressing Enter in the date box")
                else:
                    # Never click something inside the site navigation.
                    try:
                        in_nav = await page.evaluate(
                            """(sel) => {
                                const e = document.querySelector(sel);
                                if (!e) return null;
                                return !!e.closest('nav, .navbar, .nav, header, #menu');
                            }""", target)
                    except Exception:  # noqa: BLE001
                        in_nav = None
                    if in_nav:
                        log.debug("  skipping %s (it is in the navigation bar)", target)
                        continue
                    if not await self._try_click(page, target, timeout=4000):
                        continue
                    log.info("  submitted search  [%s]", target)

                try:
                    await page.wait_for_load_state("networkidle", timeout=25000)
                except Exception:  # noqa: BLE001
                    await page.wait_for_timeout(6000)
                await page.wait_for_timeout(2000)

                got_xhr = len(self.captured) > before_xhr
                rows = await self._count_result_rows(page)
                navigated_away = (page.url != start_url
                                  and "search" not in page.url.lower())

                if navigated_away:
                    log.info("  ... that click left the search page; going back")
                    try:
                        await page.go_back(wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                    except Exception:  # noqa: BLE001
                        pass
                    continue

                # Require a real jump in row count, not merely "a table exists".
                if got_xhr or rows > max(baseline, 2):
                    log.info("  search returned %d result rows", rows)
                    if kind not in ("enter",):
                        self.learned["submit"] = target
                    return True

                log.info("  ... that route produced %d rows (baseline %d); trying another",
                         rows, baseline)
            except Exception as exc:  # noqa: BLE001
                log.debug("submit route %s failed: %s", target, exc)
        return False

    # ------------------------------------------------------------------- table
    @staticmethod
    async def _scrape_visible_table(page) -> List[Dict[str, Any]]:
        """DOM fallback. Reads header cells so columns are matched by name."""
        out: List[Dict[str, Any]] = []
        try:
            tables = page.locator("table")
            for ti in range(min(await tables.count(), 6)):
                tbl = tables.nth(ti)
                headers = [clean_text(h).lower()
                           for h in await tbl.locator("thead th, tr:first-child th").all_inner_texts()]
                rows = tbl.locator("tbody tr")
                nrows = await rows.count()
                if nrows == 0 or not headers:
                    continue
                for ri in range(min(nrows, 500)):
                    cells = await rows.nth(ri).locator("td").all_inner_texts()
                    if not cells:
                        continue
                    rec: Dict[str, Any] = {}
                    for ci, cell in enumerate(cells):
                        head = headers[ci] if ci < len(headers) else f"col{ci}"
                        target = _hint_lookup(head)
                        if target and not rec.get(target):
                            rec[target] = clean_text(cell)
                    if rec:
                        rec["_raw"] = cells
                        out.append(rec)
                if out:
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("DOM table scrape failed: %s", exc)
        return out

    async def _page_through_results(self, page) -> List[Dict[str, Any]]:
        dom_rows: List[Dict[str, Any]] = []
        next_candidates = [
            "a.paginate_button.next:not(.disabled)",
            "#resultsTable_next:not(.disabled)",
            "li.next:not(.disabled) a",
            "a:has-text('Next'):not(.disabled)",
            "button:has-text('Next'):not([disabled])",
        ]
        for page_no in range(1, MAX_LANDMARK_PAGES + 1):
            if not self.captured:
                batch = await self._scrape_visible_table(page)
                if batch:
                    dom_rows.extend(batch)
                    log.info("  page %d: %d rows from DOM", page_no, len(batch))
                    # Show the first row so a suspiciously small result set can
                    # be identified as page furniture at a glance.
                    if page_no == 1 and len(batch) <= 3:
                        log.info("  sample row: %s", str(batch[0])[:240])

            advanced = False
            for sel in next_candidates:
                try:
                    nxt = page.locator(sel).first
                    if await nxt.count() and await nxt.is_visible() and await nxt.is_enabled():
                        await nxt.click()
                        await page.wait_for_timeout(int(POLITE_DELAY * 1000) + 1200)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=12000)
                        except Exception:  # noqa: BLE001
                            pass
                        advanced = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not advanced:
                log.info("  no further result pages after page %d", page_no)
                break
        return dom_rows

    # ------------------------------------------------------- failure diagnostics
    async def _write_help_file(self, page, reason: str) -> None:
        """
        Plain-English failure report. No selector knowledge required to act on
        it -- the instruction is simply to send the two files along.
        """
        shot = DATA_DIR / "landmark_failure.png"
        dump = DATA_DIR / "landmark_page_dump.html"
        try:
            await page.screenshot(path=str(shot), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            dump.write_text(await page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            controls = await page.evaluate(
                """() => Array.from(document.querySelectorAll('input,select,button,a'))
                    .filter(e => e.offsetParent !== null).slice(0, 250)
                    .map(e => ({tag: e.tagName, id: e.id || null, name: e.getAttribute('name'),
                                type: e.getAttribute('type'), title: e.getAttribute('title'),
                                cls: e.className || null,
                                text: (e.innerText || e.value || '').trim().slice(0, 60)}))"""
            )
        except Exception:  # noqa: BLE001
            controls = []

        # Every input in the document, visible or not. The previous dump only
        # captured visible controls, which is precisely why it came back empty
        # and told us nothing about whether the form had loaded at all.
        try:
            all_inputs = await page.evaluate(
                """() => Array.from(document.querySelectorAll('input,select,textarea'))
                    .slice(0, 300).map(e => ({
                        tag: e.tagName, id: e.id || null, name: e.getAttribute('name'),
                        type: e.getAttribute('type'), cls: e.className || null,
                        placeholder: e.getAttribute('placeholder'),
                        readOnly: !!e.readOnly, disabled: !!e.disabled,
                        visible: e.offsetParent !== null,
                        value: (e.value || '').slice(0, 40)}))"""
            )
        except Exception:  # noqa: BLE001
            all_inputs = []

        safe_write_json(DISCOVERY_PATH, {
            "captured_at": utcnow().isoformat(),
            "url": page.url,
            "reason": reason,
            "xhr_urls_seen": sorted(set(self.xhr_urls))[:80],
            "visible_controls": controls,
            "all_inputs_including_hidden": all_inputs,
            "input_count": len(all_inputs),
            "notes": self.notes,
        })

        (REPO_ROOT / "NEEDS_ATTENTION.md").write_text(
            "# The clerk's website changed\n\n"
            f"**When:** {utcnow().strftime('%B %d, %Y at %H:%M UTC')}\n\n"
            f"**What happened:** {reason}\n\n"
            "Everything else still ran. Your foreclosure notices, tax-sale records and\n"
            "parcel data were collected normally, so today's lead list is still usable --\n"
            "it's just missing the deed/lien records from the Clerk's portal.\n\n"
            "## What to do\n\n"
            "Nothing technical. Send these three files to Claude and say\n"
            "\"the clerk portal broke, here are the files\":\n\n"
            "1. `data/landmark_discovery.json`\n"
            "2. `data/landmark_failure.png`\n"
            "3. `data/landmark_page_dump.html`\n\n"
            "They contain a picture of the page and a list of every button and box on\n"
            "it, which is everything needed to update the scraper.\n\n"
            "## Will it fix itself?\n\n"
            "Often, yes. The scraper re-learns the page from scratch on every run, so a\n"
            "temporary outage or a slow-loading page usually clears by the next morning.\n"
            "If this file is still here after two or three days, send the files along.\n",
            encoding="utf-8",
        )
        log.warning("Wrote NEEDS_ATTENTION.md with plain-English recovery steps")

    # -------------------------------------------------------------------- main
    async def run(self) -> List[Dict[str, Any]]:
        from playwright.async_api import async_playwright

        start_s = self.start.strftime("%m/%d/%Y")
        end_s = self.end.strftime("%m/%d/%Y")
        dom_rows: List[Dict[str, Any]] = []
        succeeded = False
        failure_reason = "The search form could not be operated."

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 960},
                user_agent=USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
            )
            ctx.set_default_timeout(NAV_TIMEOUT_MS)
            ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            page = await ctx.new_page()
            page.on("response", lambda r: asyncio.create_task(self._on_response(r)))

            try:
                log.info("LandmarkWeb: opening %s", CLERK_URL)
                await page.goto(CLERK_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)

                # The disclaimer must clear before any search section will be
                # revealed, so deal with it up front on the home page.
                if not await self._accept_disclaimer(page):
                    failure_reason = ("The site's disclaimer could not be dismissed, "
                                      "so the search form never became visible.")
                await page.wait_for_timeout(1200)

                # --- Route A: go straight to the search form's own URL -----
                for section in self.SEARCH_SECTIONS:
                    url = self.search_url(section)
                    log.info("LandmarkWeb: opening the %s form", section)
                    try:
                        await page.goto(url, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2200)
                    except Exception as exc:  # noqa: BLE001
                        self.notes.append(f"{section}: navigation failed ({exc})")
                        continue

                    await self._accept_disclaimer(page)

                    # Verify a form actually rendered before probing it. Report
                    # hidden inputs separately -- "17 inputs, none visible" means
                    # something is covering the form, which is a different
                    # problem from "this page has no form at all".
                    try:
                        await page.wait_for_selector("input:visible", timeout=10000)
                    except Exception:  # noqa: BLE001
                        counts = await page.evaluate(
                            """() => {
                                const all = document.querySelectorAll('input');
                                let vis = 0;
                                all.forEach(e => { if (e.offsetParent !== null) vis++; });
                                return {total: all.length, visible: vis};
                            }""")
                        log.info("  form not visible yet (%d inputs, %d visible) "
                                 "-- revealing the panel directly",
                                 counts["total"], counts["visible"])
                        revealed = await self._reveal_section(page, section)
                        if revealed:
                            log.info("  panel revealed: %d inputs now visible", revealed)
                        else:
                            self.notes.append(
                                f"{section}: {counts['total']} inputs in DOM, "
                                f"{counts['visible']} visible, reveal failed")
                            continue

                    probe = await self._probe_date_inputs(page, start_s, end_s)
                    if not probe:
                        self.notes.append(f"{section}: form present but no date fields")
                        continue

                    frm_sel, to_sel = probe
                    ok_from = await self._set_value(page, frm_sel, start_s)
                    ok_to = await self._set_value(page, to_sel, end_s)
                    if not (ok_from and ok_to):
                        self.notes.append(f"{section}: date fields rejected input")
                        continue
                    log.info("  date range set: %s .. %s", start_s, end_s)

                    if await self._submit_and_verify(page, to_sel):
                        self.learned.update({"strategy": section, "url": url,
                                             "date_from": frm_sel, "date_to": to_sel,
                                             "learned_at": utcnow().isoformat()})
                        safe_write_json(self.LEARNED_PATH, self.learned)
                        log.info("  search succeeded; configuration saved for next run")
                        succeeded = True
                        break
                    failure_reason = ("The search form was filled in but the site "
                                      "returned no results.")
                    self.notes.append(f"{section}: submitted but no results appeared")

                # --- Route B: fall back to clicking the icons on the home page
                if not succeeded:
                    log.info("LandmarkWeb: direct URLs did not work; trying the icons")
                    await page.goto(CLERK_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                    await self._accept_disclaimer(page)
                    for strategy_name, selectors in self.SEARCH_STRATEGIES:
                        opened = await self._click_any(page, selectors,
                                                       "clicked search icon", timeout=8000)
                        if not opened:
                            continue
                        await page.wait_for_timeout(2500)
                        probe = await self._probe_date_inputs(page, start_s, end_s)
                        if not probe:
                            self.notes.append(f"icon {strategy_name}: no date fields after click")
                            continue
                        frm_sel, to_sel = probe
                        if not (await self._set_value(page, frm_sel, start_s)
                                and await self._set_value(page, to_sel, end_s)):
                            continue
                        if await self._submit_and_verify(page, to_sel):
                            succeeded = True
                            break

                if succeeded:
                    dom_rows = await self._page_through_results(page)
                else:
                    # The learned config is evidently stale -- discard it so the
                    # next run starts clean rather than repeating a dead path.
                    if self.LEARNED_PATH.exists():
                        try:
                            self.LEARNED_PATH.unlink()
                            log.info("  discarded stale learned configuration")
                        except Exception:  # noqa: BLE001
                            pass
                    await self._write_help_file(page, failure_reason)

                if LANDMARK_DISCOVERY:
                    await self._write_help_file(page, "discovery mode requested")

            except Exception as exc:  # noqa: BLE001
                log.error("LandmarkWeb navigation failed: %s", exc)
                try:
                    await self._write_help_file(page, f"The site could not be reached: {exc}")
                except Exception:  # noqa: BLE001
                    pass
                raise
            finally:
                await ctx.close()
                await browser.close()

        if not succeeded and not self.captured:
            log.warning("LandmarkWeb produced no records this run; other sources continue")
            return []

        # A successful run clears any stale help file.
        na = REPO_ROOT / "NEEDS_ATTENTION.md"
        if succeeded and na.exists():
            try:
                na.unlink()
            except Exception:  # noqa: BLE001
                pass

        source_rows = self.captured if self.captured else dom_rows
        origin = "xhr" if self.captured else "dom"
        log.info("LandmarkWeb raw rows: %d (via %s)", len(source_rows), origin)

        records: List[Dict[str, Any]] = []
        for raw in source_rows:
            try:
                mapped = raw if (isinstance(raw, dict) and "doc_num" in raw) else map_landmark_row(raw)
                if not mapped or not looks_like_record(mapped):
                    continue
                rec = self._to_lead(mapped)
                if rec:
                    records.append(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("Skipping bad LandmarkWeb row: %s", exc)

        log.info("LandmarkWeb usable records: %d", len(records))
        return records

    def _to_lead(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        doc_type = clean_text(row.get("doc_type"))
        cat, is_release = categorize(doc_type)
        filed_dt = parse_date(row.get("filed"))

        # Drop anything outside the window and anything we can't categorize.
        if filed_dt and not (self.start.date() <= filed_dt.date() <= self.end.date()):
            return None
        if cat == "UNK":
            return None

        doc_num = clean_text(row.get("doc_num"))
        book = clean_text(row.get("book"))
        page_no = clean_text(row.get("page"))
        if not doc_num and book and page_no:
            doc_num = f"BK{book}-PG{page_no}"
        if not doc_num:
            doc_num = sha_key("landmark", row.get("grantor"), doc_type, row.get("filed"))

        legal = clean_text(row.get("legal"))
        return {
            "doc_num": doc_num,
            "doc_type": doc_type or cat,
            "filed": fmt_date(filed_dt),
            "cat": cat,
            "cat_label": CAT_LABELS.get(cat, cat),
            "owner": clean_text(row.get("grantor")),
            "grantee": clean_text(row.get("grantee")),
            "amount": parse_money(row.get("amount")),
            "legal": legal,
            "parcel_id": clean_text(row.get("parcel_id")) or _parcel_from_legal(legal),
            "prop_address": extract_address_from_text(legal),
            "clerk_url": CLERK_URL,
            "source": "LandmarkWeb (Clerk of Superior Court)",
            "foreclosure_sale_date": None,
            "notice_number": "",
            "status": "released" if is_release else "active",
            "is_release": is_release,
        }


def _parcel_from_legal(legal: str) -> str:
    m = PARCEL_ID_IN_TEXT_RE.search((legal or "").upper())
    return m.group(1) if m else ""


async def scrape_landmark(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    if "LANDMARK" in SKIP_SOURCES:
        log.info("Skipping LandmarkWeb (SKIP_SOURCES)")
        record_source_result("landmarkweb", True, 0, "skipped")
        return []
    if not await ensure_browser():
        record_source_result("landmarkweb", False, 0, BROWSER_ERROR)
        return []
    scraper = LandmarkScraper(start, end)
    try:
        rows = await aretry(scraper.run, times=MAX_RETRIES, label="landmarkweb")
        record_source_result("landmarkweb", True, len(rows))
        return rows
    except Exception as exc:  # noqa: BLE001
        log.error("LandmarkWeb source failed after retries: %s", exc)
        record_source_result("landmarkweb", False, 0, str(exc))
        return []


# =============================================================================
# SOURCE 3: LEGAL NOTICES (foreclosure / probate / tax sale advertisements)
# =============================================================================
# Georgia uses non-judicial foreclosure. The operative public signal is the
# "Notice of Sale Under Power" advertised in the county's legal organ (The
# Champion for DeKalb, per O.C.G.A. 9-13-140), NOT a recorded "NOFC" document.
#
# Primary: georgiapublicnotice.com -- Georgia Press Association aggregator, free,
# filterable by county + category + date range. Champion notices land here.
# Fallback: dekalblegalnotices.com -- The Champion's own site, which publishes the
# weekly legal section as PDFs.

NOTICE_CATEGORIES = ["Foreclosures", "Tax Sales", "Probate Notices",
                     "Sheriff's/Marshal's Sales", "Debtors and Creditors"]

CATEGORY_TO_CAT = {
    "foreclosures": "FC",
    "tax sales": "TAX",
    "probate notices": "PRO",
    "sheriff's/marshal's sales": "FC",
    "debtors and creditors": "PRO",
}

FORECLOSURE_MARKERS = [
    "SALE UNDER POWER", "NOTICE OF SALE UNDER POWER", "FORECLOSURE",
    "SECURITY DEED", "ATTORNEY IN FACT", "POWER OF SALE",
]

# Georgia foreclosure sales happen on the first Tuesday of the month, and the
# ads say exactly that instead of printing a date: "sold on the first Tuesday in
# October 2026". Resolve it to a real date -- the sale date is what drives
# urgency on the call list.
FIRST_TUESDAY_RE = re.compile(r"first\s+Tuesday\s+in\s+([A-Za-z]+)\,?\s*(20\d{2})?", re.I)
SALE_DATE_RE = re.compile(
    r"(?:sale\s+date\s*[:\-]?\s*|will\s+be\s+sold\s+.{0,80}?\bon\s+)"
    r"([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2})",
    re.I,
)

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def first_tuesday(year: int, month: int) -> datetime:
    """Georgia courthouse-steps sale day."""
    d = datetime(year, month, 1)
    return d + timedelta(days=(1 - d.weekday()) % 7)  # Monday=0, Tuesday=1


def resolve_sale_date(body: str) -> Optional[datetime]:
    """Prefer an explicitly printed date; otherwise compute the advertised
    first Tuesday."""
    m = SALE_DATE_RE.search(body)
    if m:
        dt = parse_date(m.group(1))
        if dt:
            return dt
    ft = FIRST_TUESDAY_RE.search(body)
    if ft:
        month = MONTHS.get((ft.group(1) or "").lower())
        if month:
            year_txt = ft.group(2)
            if year_txt:
                year = int(year_txt)
            else:
                now = utcnow()  # no year printed: assume the next such month
                year = now.year if month >= now.month else now.year + 1
            try:
                return first_tuesday(year, month)
            except ValueError:
                return None
    return None


BOOK_PAGE_RE = re.compile(r"Deed\s+Book\s+([\w\-]+)\s*,?\s*Page\s+([\w\-]+)", re.I)
PRINCIPAL_RE = re.compile(
    r"(?:original\s+principal\s+amount\s+of|principal\s+balance\s+of|"
    r"indebtedness\s+in\s+the\s+(?:original\s+)?amount\s+of)\s*\$?\s*"
    r"([\d,]+(?:\.\d{2})?)", re.I)
BORROWER_RE = re.compile(
    r"(?:executed\s+by|given\s+by|granted\s+by|from)\s+([A-Z][A-Za-z'\.\- ]{3,60}?)"
    r"\s+to\s+", re.I)
NOTICE_NUM_RE = re.compile(r"\b(?:notice|ad|legal)\s*(?:no\.?|number|#)\s*[:\-]?\s*([\w\-]{4,20})", re.I)


def parse_notice_body(text: str, category_hint: str = "") -> Dict[str, Any]:
    """Pull structured fields out of a legal-advertisement body."""
    body = clean_text(text)
    upper = body.upper()

    cat = CATEGORY_TO_CAT.get(category_hint.strip().lower(), "")
    if not cat:
        if any(m in upper for m in FORECLOSURE_MARKERS):
            cat = "FC"
        elif "TAX SALE" in upper or "TAX EXECUTION" in upper or "FI FA" in upper:
            cat = "TAX"
        elif any(m in upper for m in ("ESTATE OF", "EXECUTOR", "ADMINISTRATOR",
                                      "PROBATE", "YEAR'S SUPPORT", "YEARS SUPPORT")):
            cat = "PRO"
        else:
            cat = "UNK"

    sale_dt = resolve_sale_date(body)

    borrower = ""
    bm = BORROWER_RE.search(body)
    if bm:
        borrower = clean_text(bm.group(1))
    if not borrower:
        em = re.search(r"ESTATE OF\s+([A-Z][A-Za-z'\.\- ]{3,60})", body, re.I)
        if em:
            borrower = clean_text(em.group(1))

    amount = None
    pm = PRINCIPAL_RE.search(body)
    if pm:
        amount = parse_money(pm.group(1))
    if amount is None:
        amount = parse_money(body) if "$" in body else None

    book = page_no = ""
    bpm = BOOK_PAGE_RE.search(body)
    if bpm:
        book, page_no = bpm.group(1), bpm.group(2)

    notice_no = ""
    nm = NOTICE_NUM_RE.search(body)
    if nm:
        notice_no = nm.group(1)

    lender = ""
    lm = re.search(r"(?:current\s+(?:secured\s+)?creditor|holder\s+of\s+the\s+security\s+deed)"
                   r"[^A-Za-z0-9]{0,20}(?:is\s+)?([A-Z][A-Za-z0-9'\.\,\- &]{4,70})", body, re.I)
    if lm:
        lender = clean_text(lm.group(1))

    return {
        "cat": cat,
        "owner": borrower,
        "prop_address": extract_address_from_text(body),
        "prop_zip": extract_zip_from_text(body),
        "amount": amount,
        "sale_date": sale_dt,
        "deed_book": book,
        "deed_page": page_no,
        "notice_number": notice_no,
        "lender": lender,
        "legal": body[:600],
    }


class LegalNoticeScraper:
    """
    georgiapublicnotice.com is ASP.NET WebForms driven entirely by __doPostBack,
    with the session id baked into the URL path. That rules out plain requests --
    Playwright it is.
    """

    def __init__(self, start: datetime, end: datetime) -> None:
        self.start = start
        self.end = end
        self.county_filtered = False

    @staticmethod
    async def _check_county(page, county: str) -> bool:
        for attempt in (
            f"input[type='checkbox'][value='{county}']",
            f"label:has-text('{county}') input[type='checkbox']",
            f"li:has-text('{county}') input[type='checkbox']",
        ):
            try:
                loc = page.locator(attempt).first
                if await loc.count():
                    await loc.check(timeout=6000)
                    return True
            except Exception:  # noqa: BLE001
                continue
        try:
            await page.get_by_label(county, exact=True).first.check(timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def run(self) -> List[Dict[str, Any]]:
        from playwright.async_api import async_playwright

        results: List[Dict[str, Any]] = []
        start_s = self.start.strftime("%m/%d/%Y")
        end_s = self.end.strftime("%m/%d/%Y")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
            ctx = await browser.new_context(
                viewport={"width": 1440, "height": 960},
                user_agent=USER_AGENT, locale="en-US", timezone_id="America/New_York",
            )
            # Short timeouts here on purpose: the previous run spent four
            # minutes waiting 60s at a time for elements that were never going
            # to appear. A missing control should fail in seconds.
            ctx.set_default_timeout(12000)
            page = await ctx.new_page()
            page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

            try:
                log.info("Legal notices: opening %s", LEGAL_NOTICE_SEARCH_URL)
                await page.goto(LEGAL_NOTICE_SEARCH_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)

                for opener in ("a:has-text('Advanced Search')", "text=Advanced Search"):
                    try:
                        await page.locator(opener).first.click(timeout=5000)
                        await page.wait_for_timeout(1200)
                        break
                    except Exception:  # noqa: BLE001
                        continue

                self.county_filtered = await self._check_county(page, "DeKalb")
                if self.county_filtered:
                    log.info("  DeKalb county filter applied")
                else:
                    log.warning("  could not tick the DeKalb checkbox; adding DeKalb to the "
                                "search text instead and filtering results client-side")

                # Date range
                for sel, val in ((["input[id*='txtDateFrom' i]", "input[id*='dpFrom' i]",
                                   "input[name*='From' i]"], start_s),
                                 (["input[id*='txtDateTo' i]", "input[id*='dpTo' i]",
                                   "input[name*='To' i]"], end_s)):
                    for s in sel:
                        try:
                            el = page.locator(s).first
                            if await el.count():
                                await el.fill(val)
                                break
                        except Exception:  # noqa: BLE001
                            continue

                for term in FORECLOSURE_MARKERS[:2] + ["ESTATE OF", "TAX SALE"]:
                    try:
                        rows = await self._search_term(page, term)
                        results.extend(rows)
                        await page.wait_for_timeout(int(POLITE_DELAY * 1000))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("  notice search for %r failed: %s", term, exc)

            finally:
                await ctx.close()
                await browser.close()

        return results

    async def _search_term(self, page, term: str) -> List[Dict[str, Any]]:
        # Without the county checkbox the search runs statewide, so put the
        # county in the query itself to keep the result set relevant.
        if not self.county_filtered:
            term = f"{term} DeKalb"
        log.info("  searching notices for %r", term)
        filled = False
        for sel in ("input[id*='txtSearch' i]", "input[type='search']",
                    "input[id*='Keyword' i]", "input[name*='search' i]"):
            try:
                el = page.locator(sel).first
                if await el.count():
                    await el.fill(term)
                    filled = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not filled:
            log.warning("  no search box found on notice site")
            return []

        for sel in ("input[type='submit'][value*='Search' i]", "button:has-text('Search')",
                    "a:has-text('Search')", "#btnSearch"):
            try:
                el = page.locator(sel).first
                if await el.count():
                    await el.click()
                    break
            except Exception:  # noqa: BLE001
                continue

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            await page.wait_for_timeout(4000)

        out: List[Dict[str, Any]] = []
        try:
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            blocks = soup.select("div.notice, tr.notice, div[id*='Result'] li, "
                                 "div[id*='Result'] div.item, table tr")
            seen_texts = set()
            for blk in blocks[:MAX_NOTICE_DETAILS]:
                text = clean_text(blk.get_text(" "))
                if len(text) < 120:
                    continue
                upper = text.upper()
                if "DEKALB" not in upper:
                    continue
                sig = sha_key(text[:300])
                if sig in seen_texts:
                    continue
                seen_texts.add(sig)

                link = ""
                a = blk.find("a", href=True) if hasattr(blk, "find") else None
                if a:
                    href = a["href"]
                    link = href if href.startswith("http") else \
                        "https://www.georgiapublicnotice.com/" + href.lstrip("/")

                parsed = parse_notice_body(text)
                if parsed["cat"] == "UNK":
                    continue
                out.append(self._to_lead(parsed, text, link))
        except Exception as exc:  # noqa: BLE001
            log.warning("  could not parse notice results: %s", exc)

        log.info("  -> %d DeKalb notices for %r", len(out), term)
        return out

    def _to_lead(self, parsed: Dict[str, Any], raw_text: str, url: str) -> Dict[str, Any]:
        cat = parsed["cat"]
        notice_no = parsed["notice_number"]
        dedupe_id = notice_no or sha_key(
            normalize_name(parsed["owner"]),
            normalize_address(parsed["prop_address"]),
            cat,
            fmt_date(parsed["sale_date"]),
        )
        return {
            "doc_num": f"NOTICE-{dedupe_id}",
            "doc_type": "Legal Notice / Sale Under Power" if cat == "FC" else f"Legal Notice ({cat})",
            "filed": fmt_date(utcnow().replace(tzinfo=None)),
            "cat": cat,
            "cat_label": CAT_LABELS.get(cat, cat),
            "owner": parsed["owner"],
            "grantee": parsed["lender"],
            "amount": parsed["amount"],
            "legal": parsed["legal"],
            "parcel_id": "",
            "prop_address": parsed["prop_address"],
            "prop_zip": parsed["prop_zip"],
            "clerk_url": url or LEGAL_NOTICE_SEARCH_URL,
            "source": "Georgia Public Notice (legal organ advertisement)",
            "foreclosure_sale_date": fmt_date(parsed["sale_date"]) or None,
            "notice_number": notice_no,
            "deed_book": parsed["deed_book"],
            "deed_page": parsed["deed_page"],
            "status": "active",
            "is_release": False,
            "_notice_dedupe": dedupe_id,
        }


@retry(label="champion-pdf-index")
def _fetch_champion_pdf_index(session: requests.Session) -> List[str]:
    resp = session.get(LEGAL_NOTICE_FALLBACK_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    pdfs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") and "legal" in href.lower():
            pdfs.append(href if href.startswith("http")
                        else LEGAL_NOTICE_FALLBACK_URL.rstrip("/") + "/" + href.lstrip("/"))
    return pdfs[:8]


def scrape_champion_fallback(session: requests.Session) -> List[Dict[str, Any]]:
    """
    Fallback only. The Champion posts the weekly legal section as a PDF; we can
    list them but text extraction needs a PDF library that is deliberately not a
    hard dependency. If pdfminer/pypdf happens to be installed we use it.
    """
    try:
        pdfs = _fetch_champion_pdf_index(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("Champion PDF index unavailable: %s", exc)
        record_source_result("champion_pdfs", False, 0, str(exc))
        return []

    if not pdfs:
        log.info("Champion fallback: no recent legal-section PDFs linked on the front page")
        record_source_result("champion_pdfs", True, 0, "no pdfs linked")
        return []

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        log.info("Champion fallback: found %d legal PDFs but pypdf is not installed; "
                 "skipping text extraction (pip install pypdf to enable)", len(pdfs))
        record_source_result("champion_pdfs", True, 0, "pypdf not installed")
        return []

    out: List[Dict[str, Any]] = []
    for url in pdfs[:2]:
        try:
            blob = session.get(url, timeout=HTTP_TIMEOUT).content
            reader = PdfReader(io.BytesIO(blob))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:40])
            for chunk in re.split(r"(?=NOTICE OF SALE UNDER POWER)", text, flags=re.I):
                if len(chunk) < 250 or "SALE UNDER POWER" not in chunk.upper():
                    continue
                parsed = parse_notice_body(chunk, "foreclosures")
                if not (parsed["owner"] or parsed["prop_address"]):
                    continue
                dedupe_id = sha_key(normalize_name(parsed["owner"]),
                                    normalize_address(parsed["prop_address"]),
                                    fmt_date(parsed["sale_date"]))
                out.append({
                    "doc_num": f"CHAMP-{dedupe_id}",
                    "doc_type": "Notice of Sale Under Power (The Champion)",
                    "filed": fmt_date(utcnow().replace(tzinfo=None)),
                    "cat": "FC", "cat_label": CAT_LABELS["FC"],
                    "owner": parsed["owner"], "grantee": parsed["lender"],
                    "amount": parsed["amount"], "legal": parsed["legal"],
                    "parcel_id": "", "prop_address": parsed["prop_address"],
                    "prop_zip": parsed["prop_zip"], "clerk_url": url,
                    "source": "The Champion legal section (PDF)",
                    "foreclosure_sale_date": fmt_date(parsed["sale_date"]) or None,
                    "notice_number": parsed["notice_number"],
                    "status": "active", "is_release": False,
                    "_notice_dedupe": dedupe_id,
                })
        except Exception as exc:  # noqa: BLE001
            log.warning("Champion PDF %s failed: %s", url, exc)

    record_source_result("champion_pdfs", True, len(out))
    return out


async def scrape_legal_notices(start: datetime, end: datetime,
                               session: requests.Session) -> List[Dict[str, Any]]:
    if "NOTICES" in SKIP_SOURCES:
        log.info("Skipping legal notices (SKIP_SOURCES)")
        record_source_result("legal_notices", True, 0, "skipped")
        return []
    rows: List[Dict[str, Any]] = []
    if await ensure_browser():
        scraper = LegalNoticeScraper(start, end)
        try:
            rows = await aretry(scraper.run, times=2, label="legal-notices")
            record_source_result("legal_notices", True, len(rows))
        except Exception as exc:  # noqa: BLE001
            log.error("Legal notice source failed: %s", exc)
            record_source_result("legal_notices", False, 0, str(exc))
    else:
        record_source_result("legal_notices", False, 0, BROWSER_ERROR)

    if not rows:
        log.info("Trying The Champion PDF fallback")
        rows = scrape_champion_fallback(session)
    return rows


# =============================================================================
# SOURCE 4: DEKALB TAX COMMISSIONER -- TAX SALE / DELINQUENT LISTING
# =============================================================================
# The listing carries a Parcel ID column, which is the single most valuable
# field in this whole program: parcel-ID matching never mis-attributes a lien.

TAX_COLUMN_HINTS = {
    "tax_sale_date": ["tax sale date", "sale date"],
    "parcel_id": ["parcel id", "parcel", "map ref"],
    "tax_sale_id": ["tax sale id"],
    "owner": ["owner"],
    "prop_address": ["address", "property address", "situs"],
    "defendant": ["defendant"],
    "levy_type": ["levy type"],
    "lien_book": ["lien book", "book"],
    "lien_page": ["page"],
    "levy_date": ["levy date"],
    "min_year": ["min year"],
    "max_year": ["max year"],
    "amount": ["total tax due", "amount due", "tax due", "total due"],
}


def _map_tax_header(header: str) -> Optional[str]:
    h = clean_text(header).lower()
    for target, hints in TAX_COLUMN_HINTS.items():
        for hint in hints:
            if h == hint or (len(hint) >= 5 and hint in h):
                return target
    return None


def _tax_rows_from_html(html: str, source_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    out: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        head_cells = table.find_all("th")
        if not head_cells:
            first = table.find("tr")
            head_cells = first.find_all("td") if first else []
        headers = [clean_text(c.get_text(" ")) for c in head_cells]
        mapped = [_map_tax_header(h) for h in headers]
        if not any(m == "parcel_id" for m in mapped):
            continue

        for tr in table.find_all("tr")[1:]:
            try:
                cells = [clean_text(td.get_text(" ")) for td in tr.find_all("td")]
                if not cells or len(cells) < 3:
                    continue
                rec: Dict[str, Any] = {}
                for i, cell in enumerate(cells):
                    key = mapped[i] if i < len(mapped) else None
                    if key and not rec.get(key):
                        rec[key] = cell
                if not rec.get("parcel_id"):
                    continue

                sale_dt = parse_date(rec.get("tax_sale_date"))
                levy_dt = parse_date(rec.get("levy_date"))
                years = ""
                if rec.get("min_year") and rec.get("max_year"):
                    years = f"{rec['min_year']}-{rec['max_year']}"

                out.append({
                    "doc_num": rec.get("tax_sale_id") or f"TAX-{normalize_parcel_id(rec['parcel_id'])}",
                    "doc_type": f"Tax Sale / FiFa ({rec.get('levy_type', 'DEK')})",
                    "filed": fmt_date(levy_dt or sale_dt),
                    "cat": "TAX",
                    "cat_label": CAT_LABELS["TAX"],
                    "owner": rec.get("owner", ""),
                    "grantee": "DeKalb County Tax Commissioner",
                    "amount": parse_money(rec.get("amount")),
                    "legal": f"Delinquent tax years {years}. Lien book {rec.get('lien_book','')} "
                             f"page {rec.get('lien_page','')}".strip(),
                    "parcel_id": rec.get("parcel_id", ""),
                    "prop_address": rec.get("prop_address", ""),
                    "clerk_url": source_url,
                    "source": "DeKalb Tax Commissioner (tax sale listing)",
                    "foreclosure_sale_date": fmt_date(sale_dt) or None,
                    "notice_number": rec.get("tax_sale_id", ""),
                    "status": "active",
                    "is_release": False,
                    "tax_sale_date": fmt_date(sale_dt),
                    "years_delinquent": years,
                })
            except Exception as exc:  # noqa: BLE001
                log.debug("Skipping bad tax row: %s", exc)
    return out


@retry(label="tax-listing-http")
def _fetch_tax_listing_http(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


async def _fetch_tax_listing_browser(url: str) -> str:
    """The public-access app initializes a session before serving content."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        ctx = await browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                        viewport={"width": 1400, "height": 900})
        ctx.set_default_timeout(NAV_TIMEOUT_MS)
        page = await ctx.new_page()
        try:
            # This public-access app hands out a session on the root page and
            # bounces deep links that arrive without one.
            root = url.split("/forms/")[0]
            try:
                await page.goto(root, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                log.debug("tax root visit failed: %s", exc)
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            try:
                await page.wait_for_selector("table", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            # The listing is often rendered inside an iframe.
            html = await page.content()
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    fhtml = await frame.content()
                    if "parcel" in fhtml.lower():
                        html += fhtml
                except Exception:  # noqa: BLE001
                    continue
            return html
        finally:
            await ctx.close()
            await browser.close()


async def scrape_tax_sales(session: requests.Session) -> List[Dict[str, Any]]:
    if "TAX" in SKIP_SOURCES:
        log.info("Skipping tax sales (SKIP_SOURCES)")
        record_source_result("tax_sales", True, 0, "skipped")
        return []

    rows: List[Dict[str, Any]] = []
    last_error = ""
    for url in TAX_SALE_LISTING_URLS:
        try:
            html = _fetch_tax_listing_http(session, url)
            rows = _tax_rows_from_html(html, url)
            if rows:
                log.info("Tax sale listing: %d rows via HTTP from %s", len(rows), url)
                break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.info("  HTTP fetch of %s failed (%s); trying browser", url, exc)

        if not await ensure_browser():
            continue        # HTTP path already tried above; nothing more to do
        try:
            html = await aretry(_fetch_tax_listing_browser, url, times=2, label="tax-listing-browser")
            rows = _tax_rows_from_html(html, url)
            if rows:
                log.info("Tax sale listing: %d rows via browser from %s", len(rows), url)
                break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("  browser fetch of %s failed: %s", url, exc)

    if rows:
        record_source_result("tax_sales", True, len(rows))
    else:
        log.warning("Tax sale listing produced no rows (%s)", last_error or "empty table")
        record_source_result("tax_sales", False, 0, last_error or "no rows parsed")
    return rows


# =============================================================================
# ENRICHMENT: attach parcel data
# =============================================================================

def enrich_with_parcels(records: List[Dict[str, Any]], parcels: ParcelIndex) -> Tuple[int, int]:
    matched = unmatched = 0
    for rec in records:
        try:
            parcel, confidence, method = parcels.match(
                parcel_id=rec.get("parcel_id", ""),
                prop_address=rec.get("prop_address", ""),
                owner=rec.get("owner", ""),
                legal=rec.get("legal", ""),
            )
            rec["match_confidence"] = round(confidence, 2)
            rec["match_method"] = method

            if not parcel:
                unmatched += 1
                rec.setdefault("prop_city", "")
                rec.setdefault("prop_state", STATE_ABBR)
                rec.setdefault("prop_zip", rec.get("prop_zip", ""))
                rec.setdefault("mail_address", "")
                rec.setdefault("mail_city", "")
                rec.setdefault("mail_state", "")
                rec.setdefault("mail_zip", "")
                rec.setdefault("owner_occupied", None)
                continue

            matched += 1
            site = ParcelIndex.site_address(parcel)
            rec["parcel_id"] = clean_text(parcel.get("PARCELID")) or rec.get("parcel_id", "")
            rec["prop_address"] = site or rec.get("prop_address", "")
            rec["prop_city"] = clean_text(parcel.get("CITY"))
            rec["prop_state"] = clean_text(parcel.get("STATE")) or STATE_ABBR
            rec["prop_zip"] = clean_text(parcel.get("ZIP")) or rec.get("prop_zip", "")

            rec["mail_address"] = clean_text(parcel.get("PSTLADDRESS"))
            rec["mail_city"] = clean_text(parcel.get("PSTLCITY"))
            rec["mail_state"] = clean_text(parcel.get("PSTLSTATE"))
            rec["mail_zip"] = clean_text(parcel.get("PSTLZIP5"))

            rec["parcel_owner"] = clean_text(parcel.get("OWNERNME1"))
            rec["assessed_value"] = parcel.get("TOTAPR1")
            rec["use_description"] = clean_text(parcel.get("USEDSCRP"))
            if not rec.get("legal"):
                rec["legal"] = clean_text(parcel.get("PRPRTYDSCRP"))

            rec["owner_occupied"] = determine_owner_occupancy(rec)
        except Exception as exc:  # noqa: BLE001
            unmatched += 1
            log.debug("Enrichment failed for %s: %s", rec.get("doc_num"), exc)
    return matched, unmatched


def determine_owner_occupancy(rec: Dict[str, Any]) -> Optional[bool]:
    prop = normalize_address(rec.get("prop_address"))
    mail = normalize_address(rec.get("mail_address"))
    if not prop or not mail:
        return None
    if is_po_box(rec.get("mail_address")):
        return False
    if prop == mail:
        return True
    # A mailing address that starts with the same house number + street is the
    # same property with a formatting difference, not an absentee owner.
    if prop.split() and mail.startswith(" ".join(prop.split()[:2])):
        return True
    return False


# =============================================================================
# DEDUPLICATION + PROPERTY CONSOLIDATION + RELEASE HANDLING
# =============================================================================

def dedupe_records(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Exact-document dedupe on source + doc_num (or the notice dedupe hash)."""
    seen: Dict[str, Dict[str, Any]] = {}
    dupes = 0
    for rec in records:
        key = rec.get("_notice_dedupe") or f"{rec.get('source','')}|{rec.get('doc_num','')}"
        key = key.strip().upper()
        if key in seen:
            dupes += 1
            prior = seen[key]
            # Newspaper ads run four consecutive weeks. Keep the earliest filing
            # date but remember that we saw it again today.
            pf, cf = parse_date(prior.get("filed")), parse_date(rec.get("filed"))
            if pf and cf and cf < pf:
                prior["filed"] = rec["filed"]
            prior["last_verified"] = fmt_date(utcnow().replace(tzinfo=None))
            if not prior.get("amount") and rec.get("amount"):
                prior["amount"] = rec["amount"]
            if not prior.get("prop_address") and rec.get("prop_address"):
                prior["prop_address"] = rec["prop_address"]
            continue
        rec["last_verified"] = fmt_date(utcnow().replace(tzinfo=None))
        seen[key] = rec
    return list(seen.values()), dupes


def property_key(rec: Dict[str, Any]) -> str:
    """Property identity, in priority order: parcel id, address, owner+legal."""
    pid = normalize_parcel_id(rec.get("parcel_id"))
    if pid:
        return f"PID:{pid}"
    addr = address_key(rec.get("prop_address"), rec.get("prop_zip"))
    if addr:
        return f"ADR:{addr}"
    sig = token_signature(rec.get("owner"))
    legal = normalize_address(rec.get("legal"))[:60]
    if sig:
        return f"OWN:{sig}|{legal}"
    return f"DOC:{rec.get('source','')}|{rec.get('doc_num','')}"


def apply_release_handling(records: List[Dict[str, Any]]) -> int:
    """
    A recorded release cancels the distress signal it points at. Without this
    the list fills up with lis pendens that were resolved months ago.
    """
    by_property: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_property[property_key(rec)].append(rec)

    released = 0
    for group in by_property.values():
        releases = [r for r in group if r.get("is_release") or r.get("cat") == "RELLP"]
        if not releases:
            continue
        for rel in releases:
            rel_dt = parse_date(rel.get("filed"))
            for other in group:
                if other is rel or other.get("is_release"):
                    continue
                if other.get("cat") not in DISTRESS_CATEGORIES:
                    continue
                other_dt = parse_date(other.get("filed"))
                # Only a release filed at/after the distress document counts.
                if rel_dt and other_dt and rel_dt < other_dt:
                    continue
                if rel.get("cat") == "RELLP" and other.get("cat") != "LP":
                    continue
                other["status"] = "released"
                other["released_by"] = rel.get("doc_num")
                released += 1
    return released


def consolidate_flags(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Build per-property context. John Smith's judgment (Monday), lis pendens
    (Wednesday) and foreclosure ad (Thursday) are one motivated seller with
    three stacked signals -- not three separate leads.
    """
    context: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        grouped[property_key(rec)].append(rec)

    for key, group in grouped.items():
        active = [r for r in group if r.get("status") != "released"]
        cats = {r.get("cat") for r in active if r.get("cat") in DISTRESS_CATEGORIES}
        amounts = [r["amount"] for r in active if isinstance(r.get("amount"), (int, float))]
        context[key] = {
            "categories": cats,
            "doc_count": len(group),
            "active_count": len(active),
            "max_amount": max(amounts) if amounts else None,
            "has_lp_and_fc": {"LP", "FC"}.issubset(cats),
            "distinct_distress": len(cats),
        }
    return context


# =============================================================================
# FLAGS + SELLER SCORE
# =============================================================================

CORP_TOKENS = {"LLC", "INC", "CORP", "CORPORATION", "LP", "LLP", "COMPANY",
               "HOLDINGS", "PROPERTIES", "INVESTMENTS"}


def build_flags(rec: Dict[str, Any], ctx: Dict[str, Any],
                start: datetime, end: datetime) -> List[str]:
    flags: List[str] = []

    # Distress categories present anywhere on this property, deduplicated.
    for cat in sorted(ctx.get("categories", set())):
        flag = CAT_FLAGS.get(cat)
        if flag and flag not in flags:
            flags.append(flag)

    if rec.get("cat") == "TAX" and "Tax sale" not in flags:
        if rec.get("tax_sale_date") or rec.get("foreclosure_sale_date"):
            flags.append("Tax sale")

    owner_tokens = set(name_tokens(rec.get("owner", "")))
    if owner_tokens & CORP_TOKENS:
        flags.append("LLC / corp owner")

    if rec.get("owner_occupied") is False:
        flags.append("Absentee owner")

    if ctx.get("distinct_distress", 0) >= 2:
        flags.append("Multiple distress signals")

    filed = parse_date(rec.get("filed"))
    if filed and start.date() <= filed.date() <= end.date():
        flags.append("New this week")

    if rec.get("status") == "released":
        flags.append("Released / resolved")

    # Preserve order, drop duplicates.
    out, seen = [], set()
    for f in flags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


MAJOR_DISTRESS_FLAGS = {
    "Lis pendens", "Pre-foreclosure", "Judgment lien", "Tax lien", "Tax delinquent",
    "Tax sale", "Mechanic lien", "HOA lien", "Government lien", "Probate / estate", "Lien",
}


def score_record(rec: Dict[str, Any], ctx: Dict[str, Any], flags: List[str]) -> int:
    score = 30

    major = [f for f in flags if f in MAJOR_DISTRESS_FLAGS]
    score += 10 * len(major)

    if ctx.get("has_lp_and_fc"):
        score += 20
    if ctx.get("distinct_distress", 0) >= 3:
        score += 20

    amount = ctx.get("max_amount") or rec.get("amount")
    if isinstance(amount, (int, float)):
        if amount > 100_000:
            score += 15
        elif amount > 50_000:
            score += 10

    if "New this week" in flags:
        score += 5
    if rec.get("prop_address"):
        score += 5
    if "Absentee owner" in flags:
        score += 5

    # A released lien is not a motivated seller.
    if rec.get("status") == "released":
        score = int(score * 0.4)
    # A notice of commencement means somebody is investing in the property.
    if rec.get("cat") == "NOC":
        score = min(score, 45)
    # A release is evidence a problem went away. It exists to downgrade the
    # document it cancels, not to become a lead in its own right.
    if rec.get("cat") == "RELLP" or rec.get("is_release"):
        score = min(score, 25)
    # Low-confidence matches should not outrank verified ones.
    if rec.get("match_confidence", 0) and rec["match_confidence"] < 0.6:
        score -= 5

    return max(0, min(100, score))


# =============================================================================
# OUTPUT
# =============================================================================

OUTPUT_FIELDS = [
    "doc_num", "doc_type", "filed", "cat", "cat_label", "owner", "grantee",
    "amount", "legal", "parcel_id", "prop_address", "prop_city", "prop_state",
    "prop_zip", "mail_address", "mail_city", "mail_state", "mail_zip",
    "owner_occupied", "clerk_url", "source", "foreclosure_sale_date",
    "notice_number", "status", "match_confidence", "match_method",
    "last_verified", "flags", "score",
]


def shape_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for field in OUTPUT_FIELDS:
        value = rec.get(field)
        if field == "prop_state":
            value = value or STATE_ABBR
        if field in ("amount", "score", "owner_occupied", "match_confidence"):
            out[field] = value
        elif field == "flags":
            out[field] = value or []
        else:
            out[field] = "" if value is None else value
    return out


def existing_lead_count() -> int:
    """How many leads are already published, if any."""
    for path in RECORDS_JSON_PATHS:
        blob = safe_read_json(path)
        if isinstance(blob, dict) and isinstance(blob.get("records"), list):
            if blob["records"]:
                return len(blob["records"])
    return 0


def write_outputs(records: List[Dict[str, Any]], start: datetime, end: datetime) -> Dict[str, Any]:
    # Never let a bad morning erase a good list. If every source failed but a
    # previous run published leads, keep those on the dashboard and only stamp
    # them as stale. Overwriting with an empty file would destroy the CSV the
    # user is actually calling from.
    if not records:
        prior = existing_lead_count()
        if prior:
            log.warning("No records collected this run -- keeping the %d leads already "
                        "published rather than overwriting them with an empty file.", prior)
            for path in RECORDS_JSON_PATHS:
                blob = safe_read_json(path)
                if isinstance(blob, dict):
                    blob["last_attempt_at"] = utcnow().isoformat()
                    blob["last_attempt_status"] = "no records collected; showing previous run"
                    blob["sources_report"] = SOURCE_REPORT
                    safe_write_json(path, blob)
            return safe_read_json(RECORDS_JSON_PATHS[0], {}) or {}

    shaped = [shape_record(r) for r in records]
    # score DESC, then filed DESC (blank dates sink to the bottom of their score tier)
    shaped.sort(key=lambda r: (-(r.get("score") or 0), _filed_sort_key(r.get("filed"))))

    payload = {
        "fetched_at": utcnow().isoformat(),
        "source": f"{COUNTY} County {STATE_ABBR} Public Records",
        "date_range": {"start": fmt_date(start), "end": fmt_date(end)},
        "total": len(shaped),
        "with_address": sum(1 for r in shaped if r.get("prop_address")),
        "sources_report": SOURCE_REPORT,
        "records": shaped,
    }

    for path in RECORDS_JSON_PATHS:
        safe_write_json(path, payload)
        log.info("Wrote %s (%d records)", path.relative_to(REPO_ROOT), len(shaped))
    return payload


def _filed_sort_key(value: Any) -> str:
    """
    Sort helper for descending dates via an ascending sort. Inverting each digit
    (9 - d) turns "2026-08-25" into a string that ascends as the date descends,
    and blanks map to the largest key so undated rows land last.
    """
    text = str(value or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return "~~~~~~~~~~"
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in text)


def collapse_to_properties(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One row per property for CRM import. Three documents on the Tate house is one
    motivated seller, not three contacts -- importing it three times means calling
    the same person three times. The highest-scoring document represents the
    property; its flag list already carries every signal found across all of them,
    and the union of document types is preserved for context.
    """
    best: Dict[str, Dict[str, Any]] = {}
    extra_types: Dict[str, List[str]] = defaultdict(list)
    for rec in records:
        key = property_key(rec)
        dt = rec.get("doc_type", "")
        if dt and dt not in extra_types[key]:
            extra_types[key].append(dt)
        cur = best.get(key)
        if cur is None or (rec.get("score") or 0) > (cur.get("score") or 0):
            best[key] = rec

    out = []
    for key, rec in best.items():
        merged = dict(rec)
        types = extra_types.get(key, [])
        if len(types) > 1:
            merged["doc_type"] = " | ".join(types[:4])
        out.append(merged)
    out.sort(key=lambda r: -(r.get("score") or 0))
    log.info("GHL export collapsed %d documents to %d unique properties",
             len(records), len(out))
    return out


def collapse_to_contacts(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Guarantee one row per human being.

    collapse_to_properties() already reduced many documents to one row per
    property, but a single owner can be distressed on several properties at
    once, which would put their name in the CSV more than once and create
    duplicate contacts on import. Here the same person (same normalized name at
    the same mailing address) is reduced to a single row: their highest-scoring
    property becomes the contact's property, and the fact that they own several
    distressed properties is preserved as a flag -- it is a strong buying
    signal, not noise.
    """
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        sig = token_signature(rec.get("owner")) or normalize_name(rec.get("owner"))
        mail = normalize_address(rec.get("mail_address"))
        if not sig:
            # No usable name: keep the row on its own so nothing is silently lost.
            sig = f"UNNAMED:{rec.get('doc_num', '')}"
        groups[f"{sig}|{mail}"].append(rec)

    out: List[Dict[str, Any]] = []
    merged_away = 0
    for group in groups.values():
        group.sort(key=lambda r: -(r.get("score") or 0))
        primary = dict(group[0])
        if len(group) > 1:
            merged_away += len(group) - 1
            flags = list(primary.get("flags") or [])
            note = f"Owns {len(group)} distressed properties"
            if note not in flags:
                flags.append(note)
            primary["flags"] = flags
            # Owning several distressed properties is itself a motivation signal.
            primary["score"] = min(100, (primary.get("score") or 0) + 5)
        out.append(primary)

    out.sort(key=lambda r: -(r.get("score") or 0))
    if merged_away:
        log.info("GHL export merged %d extra rows so each owner appears exactly once",
                 merged_away)
    return out


def export_ghl_csv(records: List[Dict[str, Any]]) -> None:
    columns = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]

    if not records:
        existing = [p for p in GHL_CSV_PATHS if p.exists() and p.stat().st_size > 200]
        if existing:
            log.warning("No records this run -- leaving the existing %s in place",
                        existing[0].name)
            return

    # Always collapse. One seller must never appear twice in the CRM import.
    records = collapse_to_contacts(collapse_to_properties(records))

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    written = 0
    for rec in records:
        try:
            first, last = split_person_name(rec.get("owner", ""))
            amount = rec.get("amount")
            writer.writerow({
                "First Name": first,
                "Last Name": last,
                "Mailing Address": rec.get("mail_address", ""),
                "Mailing City": rec.get("mail_city", ""),
                "Mailing State": rec.get("mail_state", ""),
                "Mailing Zip": rec.get("mail_zip", ""),
                "Property Address": rec.get("prop_address", ""),
                "Property City": rec.get("prop_city", ""),
                "Property State": rec.get("prop_state", STATE_ABBR),
                "Property Zip": rec.get("prop_zip", ""),
                "Lead Type": rec.get("cat_label", ""),
                "Document Type": rec.get("doc_type", ""),
                "Date Filed": rec.get("filed", ""),
                "Document Number": rec.get("doc_num", ""),
                "Amount/Debt Owed": f"{amount:.2f}" if isinstance(amount, (int, float)) else "",
                "Seller Score": rec.get("score", 0),
                "Motivated Seller Flags": "; ".join(rec.get("flags", []) or []),
                "Source": rec.get("source", ""),
                "Public Records URL": rec.get("clerk_url", ""),
            })
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("CSV row skipped for %s: %s", rec.get("doc_num"), exc)

    blob = buf.getvalue()

    # Final guarantee, checked against the bytes actually being written.
    names = [r.split(",")[0] + "|" + r.split(",")[1]
             for r in blob.splitlines()[1:] if r.count(",") > 2]
    dupes = len(names) - len(set(names))
    if dupes:
        log.warning("CSV still contains %d repeated names -- please report this", dupes)
    else:
        log.info("CSV verified: %d rows, every owner appears exactly once", written)

    for path in GHL_CSV_PATHS:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".csv.tmp")
        tmp.write_text(blob, encoding="utf-8-sig")
        os.replace(tmp, path)
        log.info("Wrote %s (%d rows)", path.relative_to(REPO_ROOT), written)


def push_to_gohighlevel(records: List[Dict[str, Any]]) -> None:
    """
    Optional. Runs only when both env vars are set; the CSV export never depends
    on it. Kept deliberately small so the auth/endpoint details can be filled in
    against whichever GHL API version the account is on.
    """
    api_key = os.getenv("GHL_API_KEY", "").strip()
    location_id = os.getenv("GHL_LOCATION_ID", "").strip()
    if not (api_key and location_id):
        log.info("GHL push skipped (GHL_API_KEY / GHL_LOCATION_ID not set)")
        return

    session = build_session()
    session.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Version": os.getenv("GHL_API_VERSION", "2021-07-28"),
    })
    endpoint = os.getenv("GHL_CONTACTS_ENDPOINT", "https://services.leadconnectorhq.com/contacts/")
    pushed = failed = 0
    for rec in records:
        if (rec.get("score") or 0) < int(os.getenv("GHL_MIN_SCORE", "60")):
            continue
        first, last = split_person_name(rec.get("owner", ""))
        body = {
            "locationId": location_id,
            "firstName": first, "lastName": last,
            "address1": rec.get("mail_address", ""),
            "city": rec.get("mail_city", ""),
            "state": rec.get("mail_state", ""),
            "postalCode": rec.get("mail_zip", ""),
            "source": rec.get("source", ""),
            "tags": ["dekalb-lead", rec.get("cat", "").lower()] + [
                f.lower().replace(" ", "-") for f in (rec.get("flags") or [])],
            "customFields": [
                {"key": "property_address", "field_value": rec.get("prop_address", "")},
                {"key": "seller_score", "field_value": str(rec.get("score", 0))},
                {"key": "public_records_url", "field_value": rec.get("clerk_url", "")},
            ],
        }
        try:
            resp = session.post(endpoint, json=body, timeout=HTTP_TIMEOUT)
            if resp.status_code < 300:
                pushed += 1
            else:
                failed += 1
                log.debug("GHL rejected %s: %s %s", rec.get("doc_num"),
                          resp.status_code, resp.text[:160])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.debug("GHL push error for %s: %s", rec.get("doc_num"), exc)
        time.sleep(0.25)
    log.info("GHL push complete: %d contacts, %d failures", pushed, failed)


# =============================================================================
# SEEN-HISTORY (so "New this week" means filed recently, not first-seen today)
# =============================================================================

def load_seen() -> Dict[str, str]:
    blob = safe_read_json(SEEN_STATE_PATH, {})
    return blob if isinstance(blob, dict) else {}


def save_seen(records: List[Dict[str, Any]], prior: Dict[str, str]) -> None:
    today = fmt_date(utcnow().replace(tzinfo=None))
    for rec in records:
        key = rec.get("_notice_dedupe") or f"{rec.get('source','')}|{rec.get('doc_num','')}"
        prior.setdefault(key, rec.get("filed") or today)
    # Trim anything older than a year so the state file cannot grow forever.
    cutoff = (utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")
    trimmed = {k: v for k, v in prior.items() if (v or "9999") >= cutoff}
    safe_write_json(SEEN_STATE_PATH, trimmed)


# =============================================================================
# MAIN
# =============================================================================

async def run_all() -> int:
    t0 = time.time()
    end = utcnow().replace(tzinfo=None)
    start = end - timedelta(days=LOOKBACK_DAYS)

    log.info("=" * 74)
    log.info("%s County %s motivated seller scraper", COUNTY, STATE_ABBR)
    log.info("Started %s UTC | window %s .. %s (%d days)",
             end.strftime("%Y-%m-%d %H:%M"), fmt_date(start), fmt_date(end), LOOKBACK_DAYS)
    log.info("Headless=%s  Discovery=%s", HEADLESS, LANDMARK_DISCOVERY)
    log.info("=" * 74)

    session = build_session()

    # --- 1. Parcels first: everything else enriches against this ------------
    parcels = ParcelIndex()
    try:
        parcels.load(session)
    except Exception as exc:  # noqa: BLE001
        log.error("Parcel load failed entirely: %s", exc)
        record_source_result("arcgis_parcels", False, 0, str(exc))

    # --- 2. Record sources (independent; one failing never stops the rest) ---
    all_records: List[Dict[str, Any]] = []

    clerk_records = await scrape_landmark(start, end)
    log.info("Clerk records found: %d", len(clerk_records))
    all_records.extend(clerk_records)

    notice_records = await scrape_legal_notices(start, end, session)
    log.info("Foreclosure/legal notices found: %d", len(notice_records))
    all_records.extend(notice_records)

    tax_records = await scrape_tax_sales(session)
    log.info("Tax delinquent/tax sale records found: %d", len(tax_records))
    all_records.extend(tax_records)

    if not all_records:
        log.warning("No records collected from any source this run. "
                    "Existing output files are left untouched.")

    # --- 3. Dedupe ----------------------------------------------------------
    all_records, dupes = dedupe_records(all_records)
    log.info("Duplicates collapsed: %d  |  unique documents: %d", dupes, len(all_records))

    # --- 4. Enrich ----------------------------------------------------------
    matched, unmatched = enrich_with_parcels(all_records, parcels)
    log.info("Parcel matches: %d matched, %d unmatched", matched, unmatched)

    # --- 5. Releases, consolidation, flags, score ---------------------------
    released = apply_release_handling(all_records)
    if released:
        log.info("Distress records downgraded by a matching release: %d", released)

    context = consolidate_flags(all_records)
    log.info("Distinct properties represented: %d", len(context))

    for rec in all_records:
        try:
            ctx = context.get(property_key(rec), {})
            rec["flags"] = build_flags(rec, ctx, start, end)
            rec["score"] = score_record(rec, ctx, rec["flags"])
        except Exception as exc:  # noqa: BLE001
            log.debug("Scoring failed for %s: %s", rec.get("doc_num"), exc)
            rec.setdefault("flags", [])
            rec.setdefault("score", 30)

    # --- 6. Output ----------------------------------------------------------
    payload = write_outputs(all_records, start, end)
    export_ghl_csv([shape_record(r) for r in all_records])
    push_to_gohighlevel([shape_record(r) for r in all_records])

    save_seen(all_records, load_seen())
    if UNMAPPED_DOC_TYPES:
        prior = set(safe_read_json(UNKNOWN_DOCTYPES_PATH, []) or [])
        combined = sorted(prior | UNMAPPED_DOC_TYPES)
        safe_write_json(UNKNOWN_DOCTYPES_PATH, combined)
        log.info("Unmapped document types logged: %d (see %s)",
                 len(UNMAPPED_DOC_TYPES), UNKNOWN_DOCTYPES_PATH.name)

    # --- 7. Report ----------------------------------------------------------
    scored = [r for r in payload["records"] if r.get("score", 0) >= 60]
    log.info("=" * 74)
    log.info("FINAL: %d leads | %d with property address | %d scoring 60+",
             payload["total"], payload["with_address"], len(scored))
    for name, info in SOURCE_REPORT.items():
        status = "OK  " if info["ok"] else "FAIL"
        log.info("  [%s] %-18s %5d records %s", status, name, info["count"],
                 f"-- {info['error']}" if info["error"] else "")
    log.info("Execution time: %.1fs", time.time() - t0)
    log.info("=" * 74)

    hard_failures = [n for n, i in SOURCE_REPORT.items() if not i["ok"]]
    if len(hard_failures) == len(SOURCE_REPORT) and SOURCE_REPORT:
        log.error("Every source failed. Exiting non-zero so the Action surfaces it.")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DeKalb County GA motivated seller scraper")
    ap.add_argument("--lookback", type=int, help="override LOOKBACK_DAYS")
    ap.add_argument("--headful", action="store_true", help="run browsers visibly")
    ap.add_argument("--discover", action="store_true", help="dump LandmarkWeb selector recon")
    ap.add_argument("--skip", default="", help="comma list: LANDMARK,NOTICES,TAX")
    args = ap.parse_args()

    global LOOKBACK_DAYS, HEADLESS, LANDMARK_DISCOVERY, SKIP_SOURCES
    if args.lookback:
        LOOKBACK_DAYS = args.lookback
    if args.headful:
        HEADLESS = False
    if args.discover:
        LANDMARK_DISCOVERY = True
    if args.skip:
        SKIP_SOURCES |= {s.strip().upper() for s in args.skip.split(",") if s.strip()}

    try:
        return asyncio.run(run_all())
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
