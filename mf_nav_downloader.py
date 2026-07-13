"""
MF NAV Historical Downloader
==============================
Downloads historical NAV data for ~600 mutual funds across 15 SEBI categories.

Data sources:
  - AMFI (https://www.amfiindia.com) -> scheme list with categories (1 request)
  - mfapi.in (https://api.mfapi.in)  -> full NAV history per scheme

Output:
  One Excel file with one sheet per category.
  Each sheet: Date (rows) × Fund Name (columns).

Usage:
  pip install requests pandas openpyxl tqdm
  python mf_nav_downloader.py

Config:
  Edit the CONFIG block below to change categories, date range, or output path.
"""

import requests
import pandas as pd
import time
import json
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from tqdm import tqdm

# ============================================================
# CONFIG — edit this block to suit your needs
# ============================================================

START_DATE = "2015-01-01"          # Pull NAVs from this date onwards (YYYY-MM-DD)
OUTPUT_DIR = Path("mf_nav_output") # All output goes here
SLEEP_SEC  = 0.4                   # Pause between API calls (be polite to mfapi.in)
MAX_RETRIES = 3                    # How many times to retry a timed-out request

# These strings are matched against AMFI's category labels.
# Adjust if you need different categories. Matching is case-insensitive substring.
TARGET_CATEGORIES = [
    "Large & Mid Cap Fund",   # must come before "Mid Cap Fund" — substring collision
    "Large Cap Fund",
    "Flexi Cap Fund",
    "Mid Cap Fund",
    "Small Cap Fund",
    "Multi Cap Fund",
    "Value Fund",
    "Contra Fund",
    "Focused Fund",
    "Dividend Yield Fund",
    "Sectoral/ Thematic",     # AMFI label — no "Fund" at end
    "ELSS",
    "Balanced Advantage",
    "Aggressive Hybrid Fund",
    "Conservative Hybrid Fund",
]

# ============================================================
# INTERNALS — no need to edit below
# ============================================================

AMFI_URL   = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_BASE = "https://api.mfapi.in/mf"
CACHE_DIR  = OUTPUT_DIR / "_cache"


def setup():
    OUTPUT_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(exist_ok=True)


# ── Step 1: Parse AMFI master file ─────────────────────────────────────────

def fetch_amfi_schemes() -> list[dict]:
    """
    Downloads AMFI's daily NAV file (one HTTP request) and parses it to extract
    scheme codes and their SEBI categories.
    """
    print("Fetching AMFI master scheme list (1 request)...")
    resp = requests.get(AMFI_URL, timeout=30)
    resp.raise_for_status()

    schemes = []
    current_category = ""

    for raw_line in resp.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if "Schemes(" in line and line.endswith(")"):
            start = line.index("(") + 1
            current_category = line[start:-1].strip()
            continue

        if line.lower().startswith("scheme code"):
            continue

        parts = line.split(";")
        if len(parts) < 4 or not current_category:
            continue

        try:
            code = int(parts[0].strip())
        except ValueError:
            continue

        name = parts[3].strip()
        if code and name:
            schemes.append({
                "code":     code,
                "name":     name,
                "category": current_category,
            })

    print(f"  → {len(schemes):,} total schemes found across all categories")
    return schemes


# ── Step 2: Filter to target categories ─────────────────────────────────────

def filter_schemes(schemes: list[dict]) -> dict[str, list[dict]]:
    """
    Returns a dict keyed by matched TARGET_CATEGORY with the list of schemes.
    Uses case-insensitive substring matching.
    """
    result: dict[str, list] = defaultdict(list)

    for s in schemes:
        cat = s["category"].lower()
        for target in TARGET_CATEGORIES:
            if target.lower() in cat:
                result[target].append(s)
                break

    total = sum(len(v) for v in result.values())
    print(f"\nFiltered to {total} schemes across {len(result)} categories:")
    for cat in sorted(result):
        print(f"  [{len(result[cat]):3d}]  {cat}")

    return dict(result)


# ── Step 3: Fetch historical NAVs from mfapi.in ─────────────────────────────

def fetch_nav_cached(code: int) -> dict | None:
    """
    Returns mfapi.in JSON for a scheme code. Caches to disk so re-runs are instant.
    Retries up to MAX_RETRIES times on timeout, with increasing wait between attempts.
    """
    cache_file = CACHE_DIR / f"{code}.json"

    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = 20 + (attempt - 1) * 15  # 20s → 35s → 50s
            resp = requests.get(f"{MFAPI_BASE}/{code}", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "SUCCESS" and data.get("data"):
                with open(cache_file, "w") as f:
                    json.dump(data, f)
                return data

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES:
                wait = 3 * attempt
                tqdm.write(f"    ⏱ Timeout (attempt {attempt}/{MAX_RETRIES}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                tqdm.write(f"    ✗ Timed out after {MAX_RETRIES} attempts: code {code}")

        except Exception as e:
            tqdm.write(f"    ✗ Error fetching code {code}: {e}")
            break

    return None


def nav_to_series(raw: dict, scheme_name: str, start_date: str) -> pd.Series | None:
    """
    Converts raw mfapi response to a pandas Series indexed by date,
    filtered to start_date onwards.
    """
    cutoff = pd.Timestamp(start_date)
    records = []

    for entry in raw.get("data", []):
        try:
            dt  = pd.Timestamp(datetime.strptime(entry["date"], "%d-%m-%Y"))
            nav = float(entry["nav"])
            if dt >= cutoff:
                records.append((dt, nav))
        except (ValueError, KeyError):
            continue

    if not records:
        return None

    series = pd.Series(dict(records), name=scheme_name, dtype=float)
    return series.sort_index()


# ── Step 4: Collect data, then write Excel ───────────────────────────────────

def collect_data(by_category: dict[str, list[dict]]) -> dict[str, pd.DataFrame]:
    """
    Phase 1: Download and process all NAV data into DataFrames.
    Safe to interrupt with Ctrl+C — returns whatever was collected so far.
    """
    collected = {}

    try:
        for cat_name in sorted(by_category):
            schemes    = by_category[cat_name]
            all_series = []

            print(f"\n  [{cat_name}]  {len(schemes)} schemes")

            for scheme in tqdm(schemes, leave=False, unit="fund"):
                code = scheme["code"]
                name = scheme["name"]

                raw = fetch_nav_cached(code)
                time.sleep(SLEEP_SEC)

                if raw is None:
                    tqdm.write(f"    ✗ No data: {name[:70]}")
                    continue

                series = nav_to_series(raw, name, START_DATE)
                if series is None or series.empty:
                    tqdm.write(f"    ✗ Empty after {START_DATE}: {name[:70]}")
                    continue

                all_series.append(series)

            if not all_series:
                print(f"    ⚠ No usable data — skipping {cat_name}")
                continue

            df = pd.concat(all_series, axis=1)
            df.index.name = "Date"
            df.index = df.index.strftime("%Y-%m-%d")
            df = df.sort_index()

            collected[cat_name] = df
            print(f"    ✓ {len(df)} dates × {len(df.columns)} funds")

    except KeyboardInterrupt:
        print(f"\n\n⚠ Interrupted by user.")
        if collected:
            print(f"  Will save {len(collected)} categories collected so far.")
        else:
            print("  No complete categories collected. Nothing to save.")
            sys.exit(0)

    return collected


def write_excel(collected: dict[str, pd.DataFrame], output_path: Path):
    """
    Phase 2: Write collected DataFrames to Excel.
    Only called once all (or partial) data is ready — no empty workbook risk.
    """
    if not collected:
        print("No data to write.")
        return

    print(f"\nWriting Excel file → {output_path}")

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for cat_name, df in sorted(collected.items()):
            sheet = cat_name.replace("/", "-").strip()[:31]
            df.to_excel(writer, sheet_name=sheet)
            print(f"  ✓ Sheet '{sheet}'")

    print(f"\n✅ Done! File saved: {output_path}")
    print(f"   {len(collected)} sheets, open from: {output_path.resolve()}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    setup()

    all_schemes  = fetch_amfi_schemes()
    by_category  = filter_schemes(all_schemes)

    if not by_category:
        print("\n⚠ No schemes matched target categories. Check TARGET_CATEGORIES config.")
        sys.exit(1)

    total       = sum(len(v) for v in by_category.values())
    est_minutes = round(total * SLEEP_SEC / 60, 1)
    print(f"\nReady to download NAV history for {total} schemes.")
    print(f"Estimated time (fresh run): ~{est_minutes} min. Cached runs are instant.")
    print(f"Tip: if you interrupt with Ctrl+C, completed categories will still be saved.\n")
    ans = input("Proceed? [Y/n]: ").strip().lower()
    if ans and ans != "y":
        print("Aborted.")
        sys.exit(0)

    date_tag    = datetime.today().strftime("%Y%m%d")
    output_path = OUTPUT_DIR / f"MF_NAV_History_{date_tag}.xlsx"

    # Phase 1: collect (safe to interrupt)
    collected = collect_data(by_category)

    # Phase 2: write Excel (only runs if we have something)
    write_excel(collected, output_path)


if __name__ == "__main__":
    main()