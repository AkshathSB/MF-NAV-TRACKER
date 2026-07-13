import streamlit as st
import requests
import pandas as pd
import time
import json
import io
import zipfile
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

# ── CONFIG ───────────────────────────────────────────────────────────────────

AMFI_URL   = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_BASE = "https://api.mfapi.in/mf"
CACHE_DIR  = Path("/tmp/mf_cache")
SLEEP_SEC  = 0.2

# Default selected categories on first load
DEFAULT_CATEGORIES = [
    "Equity Scheme - Large & Mid Cap Fund",
    "Equity Scheme - Large Cap Fund",
    "Equity Scheme - Flexi Cap Fund",
    "Equity Scheme - Mid Cap Fund",
    "Equity Scheme - Small Cap Fund",
    "Equity Scheme - Multi Cap Fund",
    "Equity Scheme - Value Fund",
    "Equity Scheme - Contra Fund",
    "Equity Scheme - Focused Fund",
    "Equity Scheme - Dividend Yield Fund",
    "Equity Scheme - Sectoral/ Thematic",
    "Equity Scheme - ELSS",
    "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
    "Hybrid Scheme - Aggressive Hybrid Fund",
    "Hybrid Scheme - Conservative Hybrid Fund",
]

# ── CORE FUNCTIONS ────────────────────────────────────────────────────────────

def setup():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_amfi_data():
    """
    Fetches AMFI master file once per hour.
    Returns (all_categories, all_schemes).
    """
    resp = requests.get(AMFI_URL, timeout=30)
    resp.raise_for_status()

    schemes = []
    categories = set()
    current_category = ""

    for raw_line in resp.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "Schemes(" in line and line.endswith(")"):
            start = line.index("(") + 1
            current_category = line[start:-1].strip()
            categories.add(current_category)
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

    return sorted(categories), schemes

def filter_schemes(schemes, selected_cats):
    """Exact category match — no substring ambiguity."""
    result = defaultdict(list)
    selected_set = set(selected_cats)
    for s in schemes:
        if s["category"] in selected_set:
            result[s["category"]].append(s)
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
            sheet = cat_name.replace("/", "-").replace(":", "-").strip()[:31]
            df.to_excel(writer, sheet_name=sheet)
    return buffer.getvalue()

def create_cache_zip():
    """Zip all cached JSON files for download."""
    buffer = io.BytesIO()
    files  = list(CACHE_DIR.glob("*.json"))
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return buffer.getvalue(), len(files)

def load_cache_zip(uploaded_zip):
    """Extract uploaded ZIP into cache directory."""
    with zipfile.ZipFile(io.BytesIO(uploaded_zip.read())) as zf:
        zf.extractall(CACHE_DIR)
    return len(list(CACHE_DIR.glob("*.json")))

# ── STREAMLIT UI ──────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="MF NAV Downloader",
        page_icon="📈",
        layout="wide",
    )

    st.title("📈 MF NAV Historical Downloader")
    st.caption("Data: AMFI (scheme list + categories) · mfapi.in (historical NAVs) · Free, no API key needed")
    st.divider()

    setup()

    # ── Cache management ──────────────────────────────────────────────────────
    with st.expander("⚡ Speed up with cached data (optional but recommended)"):
        st.write(
            "Upload a previously downloaded cache ZIP to skip re-downloading "
            "funds that haven't changed. After each run you can download the "
            "updated cache to save for next time."
        )
        uploaded = st.file_uploader("Upload cache ZIP", type="zip", label_visibility="collapsed")
        if uploaded:
            count = load_cache_zip(uploaded)
            st.success(f"✅ Cache loaded — {count:,} funds ready, no re-download needed for these.")

    st.divider()

    # ── Load categories from AMFI ─────────────────────────────────────────────
    with st.spinner("Loading category list from AMFI..."):
        all_categories, all_schemes = fetch_amfi_data()

    valid_defaults = [c for c in DEFAULT_CATEGORIES if c in all_categories]

    col1, col2 = st.columns([3, 1])

    with col1:
        selected_cats = st.multiselect(
            f"Select categories ({len(all_categories)} available from AMFI)",
            options=all_categories,
            default=valid_defaults,
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

    by_category = filter_schemes(all_schemes, selected_cats)
    total       = sum(len(v) for v in by_category.values())
    cached      = sum(1 for s in all_schemes
                      if s["category"] in set(selected_cats)
                      and (CACHE_DIR / f"{s['code']}.json").exists())

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Categories selected", len(by_category))
    col_b.metric("Total schemes", total)
    col_c.metric("Already cached", f"{cached} / {total}")

    if st.button("🚀 Run Download", type="primary", use_container_width=True):

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

        status.caption("Building Excel file...")
        excel_bytes = build_excel_bytes(collected)

        progress_bar.empty()
        status.empty()
        cat_status.empty()

        st.success(f"✅ Done! {len(collected)} sheets ready.")
        st.divider()

        date_tag = datetime.today().strftime("%Y%m%d")

        col_dl1, col_dl2 = st.columns(2)

        with col_dl1:
            st.download_button(
                label="📥 Download Excel",
                data=excel_bytes,
                file_name=f"MF_NAV_History_{date_tag}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                type="primary",
            )

        with col_dl2:
            cache_zip, cache_count = create_cache_zip()
            st.download_button(
                label=f"💾 Download Cache ({cache_count:,} funds)",
                data=cache_zip,
                file_name=f"mf_cache_{date_tag}.zip",
                mime="application/zip",
                use_container_width=True,
            )

        st.info(
            "💡 Save the cache ZIP — upload it next time to skip re-downloading "
            f"{cache_count:,} funds and go straight to the Excel."
        )

if __name__ == "__main__":
    main()