import streamlit as st
import requests
import pandas as pd
import time
import json
import io
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

# ── CONFIG ───────────────────────────────────────────────────────────────────

AMFI_URL   = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_BASE = "https://api.mfapi.in/mf"
CACHE_DIR  = Path("/tmp/mf_cache")  # Ephemeral but persists within a session
SLEEP_SEC  = 0.2

TARGET_CATEGORIES = [
    "Large & Mid Cap Fund",
    "Large Cap Fund",
    "Flexi Cap Fund",
    "Mid Cap Fund",
    "Small Cap Fund",
    "Multi Cap Fund",
    "Value Fund",
    "Contra Fund",
    "Focused Fund",
    "Dividend Yield Fund",
    "Sectoral/ Thematic",
    "ELSS",
    "Balanced Advantage",
    "Aggressive Hybrid Fund",
    "Conservative Hybrid Fund",
]

# ── CORE FUNCTIONS ────────────────────────────────────────────────────────────

def setup():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

def fetch_amfi_schemes():
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
            schemes.append({"code": code, "name": name, "category": current_category})
    return schemes

def filter_schemes(schemes, selected_cats):
    result = defaultdict(list)
    for s in schemes:
        cat = s["category"].lower()
        for target in TARGET_CATEGORIES:
            if target.lower() in cat and target in selected_cats:
                result[target].append(s)
                break
    return dict(result)

def fetch_nav_cached(code):
    cache_file = CACHE_DIR / f"{code}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    for attempt in range(1, 4):
        try:
            timeout = 20 + (attempt - 1) * 15
            resp = requests.get(f"{MFAPI_BASE}/{code}", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "SUCCESS" and data.get("data"):
                with open(cache_file, "w") as f:
                    json.dump(data, f)
                return data
        except requests.exceptions.Timeout:
            if attempt < 3:
                time.sleep(3 * attempt)
        except Exception:
            break
    return None

def nav_to_series(raw, scheme_name, start_date):
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

def build_excel_bytes(collected):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for cat_name, df in sorted(collected.items()):
            sheet = cat_name.replace("/", "-").strip()[:31]
            df.to_excel(writer, sheet_name=sheet)
    return buffer.getvalue()

# ── STREAMLIT UI ──────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="MF NAV Downloader",
        page_icon="📈",
        layout="wide"
    )

    st.title("📈 MF NAV Historical Downloader")
    st.caption("Data: AMFI (scheme list + categories) · mfapi.in (historical NAVs) · Free, no API key needed")
    st.divider()

    col1, col2 = st.columns([3, 1])

    with col1:
        selected_cats = st.multiselect(
            "Select categories",
            options=TARGET_CATEGORIES,
            default=TARGET_CATEGORIES,
        )

    with col2:
        start_date = st.date_input(
            "Start date",
            value=date(2015, 1, 1),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
        )

    st.divider()

    if not selected_cats:
        st.warning("Select at least one category to continue.")
        return

    if st.button("🚀 Run Download", type="primary", use_container_width=True):
        setup()

        # Step 1: Fetch scheme list from AMFI
        with st.spinner("Fetching AMFI scheme list..."):
            schemes     = fetch_amfi_schemes()
            by_category = filter_schemes(schemes, selected_cats)

        total = sum(len(v) for v in by_category.values())
        st.info(f"**{total} schemes** found across **{len(by_category)} categories**")

        # Step 2: Download NAV history
        st.write("#### Downloading NAV history...")
        progress_bar = st.progress(0)
        status       = st.empty()
        cat_status   = st.empty()

        collected = {}
        done      = 0

        for cat_name in sorted(by_category):
            cat_schemes = by_category[cat_name]
            all_series  = []

            cat_status.markdown(f"**Category:** {cat_name} ({len(cat_schemes)} schemes)")

            for scheme in cat_schemes:
                code = scheme["code"]
                name = scheme["name"]

                status.caption(f"⬇ {name[:80]}")

                raw = fetch_nav_cached(code)
                time.sleep(SLEEP_SEC)

                if raw:
                    series = nav_to_series(raw, name, str(start_date))
                    if series is not None and not series.empty:
                        all_series.append(series)

                done += 1
                progress_bar.progress(done / total)

            if all_series:
                df = pd.concat(all_series, axis=1)
                df.index.name = "Date"
                df.index = df.index.strftime("%Y-%m-%d")
                df = df.sort_index()
                collected[cat_name] = df

        # Step 3: Build Excel
        status.caption("Building Excel file...")
        excel_bytes = build_excel_bytes(collected)

        # Clear progress UI
        progress_bar.empty()
        status.empty()
        cat_status.empty()

        st.success(f"✅ Done! {len(collected)} sheets ready.")

        date_tag = datetime.today().strftime("%Y%m%d")
        st.download_button(
            label="📥 Download Excel",
            data=excel_bytes,
            file_name=f"MF_NAV_History_{date_tag}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            type="primary",
        )

if __name__ == "__main__":
    main()
