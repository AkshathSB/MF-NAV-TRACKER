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
from rapidfuzz import fuzz, process

# ── CONFIG ───────────────────────────────────────────────────────────────────

AMFI_URL   = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_BASE = "https://api.mfapi.in/mf"
CACHE_DIR  = Path("/tmp/mf_cache")
SLEEP_SEC  = 0.2

CATEGORY_MAP = {
    # Equity
    "Equity - Large Cap":         ("Equity",  "direct",  ["Equity Scheme - Large Cap Fund"]),
    "Equity - Mid Cap":           ("Equity",  "direct",  ["Equity Scheme - Mid Cap Fund"]),
    "Equity - Small Cap":         ("Equity",  "direct",  ["Equity Scheme - Small Cap Fund"]),
    "Equity - Multi Cap":         ("Equity",  "direct",  ["Equity Scheme - Multi Cap Fund"]),
    "Equity - Flexi Cap":         ("Equity",  "direct",  ["Equity Scheme - Flexi Cap Fund"]),
    "Equity - Large & Mid Cap":   ("Equity",  "direct",  ["Equity Scheme - Large & Mid Cap Fund"]),
    "Equity - Value & Contra":    ("Equity",  "direct",  ["Equity Scheme - Value Fund", "Equity Scheme - Contra Fund"]),
    "Equity - Dividend Yield":    ("Equity",  "direct",  ["Equity Scheme - Dividend Yield Fund"]),
    "Equity - Focused":           ("Equity",  "direct",  ["Equity Scheme - Focused Fund"]),
    "Equity - ELSS":              ("Equity",  "direct",  ["Equity Scheme - ELSS", "ELSS"]),
    "Equity - Quality":           ("Equity",  "keyword", ("Equity Scheme - Sectoral/ Thematic", ["quality"])),
    "Equity - Quant":             ("Equity",  "keyword", ("Equity Scheme - Sectoral/ Thematic", ["quant"])),
    # Gold
    "Gold - ETF":                 ("Gold",    "direct",  ["Other Scheme - Gold ETF"]),
    "Gold - Fund/ ETF FoF":       ("Gold",    "keyword", ("Other Scheme - FoF Domestic", ["gold"])),
    # Hybrid
    "Hybrid - Balanced Advantage / DAA": ("Hybrid", "direct", [
        "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
        "Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation",
    ]),
    "Hybrid - Aggressive Hybrid": ("Hybrid",  "direct",  [
        "Hybrid Scheme - Aggressive Hybrid Fund",
        "Hybrid Schemes - Aggressive Hybrid Fund",
    ]),
    "Hybrid - Arbitrage":         ("Hybrid",  "direct",  ["Hybrid Scheme - Arbitrage Fund"]),
    "Hybrid - Equity Savings":    ("Hybrid",  "direct",  ["Hybrid Scheme - Equity Savings"]),
    "Hybrid - Conservative Hybrid": ("Hybrid", "direct", ["Hybrid Scheme - Conservative Hybrid Fund"]),
    "Hybrid - Balanced Hybrid":   ("Hybrid",  "direct",  ["Hybrid Scheme - Balanced Hybrid Fund"]),
    "Hybrid - Multi Asset Allocation": ("Hybrid", "direct", ["Hybrid Scheme - Multi Asset Allocation"]),
    # Debt
    "Debt - Overnight":           ("Debt", "direct", ["Debt Scheme - Overnight Fund"]),
    "Debt - Liquid":              ("Debt", "direct", ["Debt Scheme - Liquid Fund"]),
    "Debt - Money Market":        ("Debt", "direct", ["Debt Scheme - Money Market Fund"]),
    "Debt - Ultra Short Duration":("Debt", "direct", ["Debt Scheme - Ultra Short Duration Fund"]),
    "Debt - Low Duration":        ("Debt", "direct", ["Debt Scheme - Low Duration Fund"]),
    "Debt - Floating Rate":       ("Debt", "direct", ["Debt Scheme - Floater Fund"]),
    "Debt - Banking and PSU":     ("Debt", "direct", ["Debt Scheme - Banking and PSU Fund"]),
    "Debt - Corporate Bond":      ("Debt", "direct", ["Debt Scheme - Corporate Bond Fund"]),
    "Debt - Short Duration":      ("Debt", "direct", ["Debt Scheme - Short Duration Fund"]),
    "Debt - Medium to Long Duration": ("Debt","direct", ["Debt Scheme - Medium to Long Duration Fund"]),
    "Debt - Medium Duration":     ("Debt", "direct", ["Debt Scheme - Medium Duration Fund"]),
    "Debt - Gilt":                ("Debt", "direct", ["Debt Scheme - Gilt Fund", "Debt Scheme - Gilt Fund with 10 year constant duration"]),
    "Debt - Long Duration":       ("Debt", "direct", ["Debt Scheme - Long Duration Fund"]),
    "Debt - Dynamic Bond":        ("Debt", "direct", ["Debt Scheme - Dynamic Bond"]),
    "Debt - Credit Risk":         ("Debt", "direct", ["Debt Scheme - Credit Risk Fund"]),
    "Debt - Target Maturity":     ("Debt", "keyword", ("Other Scheme - Index Funds", ["target maturity", "bharat bond"])),
}

# ── CORE FUNCTIONS ────────────────────────────────────────────────────────────

def setup():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_amfi_data():
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

def apply_plan_filter(schemes, plan_mode):
    """Filter schemes by plan type."""
    if plan_mode == "Both":
        return schemes
    if plan_mode == "Direct only":
        return [s for s in schemes if "direct" in s["name"].lower()]
    if plan_mode == "Regular only":
        return [s for s in schemes if "regular" in s["name"].lower() or "direct" not in s["name"].lower()]
    return schemes

def build_display_map(schemes, selected_displays, extra_amfi_cats, extra_schemes):
    """
    Maps each selected display to its schemes.
    - selected_displays → predefined subcategories (from CATEGORY_MAP)
    - extra_amfi_cats   → full AMFI categories added via search
    - extra_schemes     → individual schemes added via search
    """
    result = defaultdict(list)

    for display in selected_displays:
        _, mode, config = CATEGORY_MAP[display]
        if mode == "direct":
            amfi_cats = set(config)
            for s in schemes:
                if s["category"] in amfi_cats:
                    result[display].append(s)
        elif mode == "keyword":
            parent_cat, keywords = config
            for s in schemes:
                if s["category"] == parent_cat:
                    name_lower = s["name"].lower()
                    if any(kw.lower() in name_lower for kw in keywords):
                        result[display].append(s)

    # Extra AMFI categories (added as-is)
    for amfi_cat in extra_amfi_cats:
        display = f"Custom - {amfi_cat}"
        for s in schemes:
            if s["category"] == amfi_cat:
                result[display].append(s)

    # Extra individual schemes (grouped into one sheet)
    if extra_schemes:
        sel_codes = {s["code"] for s in extra_schemes}
        for s in schemes:
            if s["code"] in sel_codes:
                result["Custom - Selected Schemes"].append(s)

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
    used_names = set()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for cat_name, df in sorted(collected.items()):
            sheet = cat_name.replace("/", "-").replace(":", "-").strip()[:31]
            # Ensure uniqueness if truncation causes collisions
            base = sheet
            i = 1
            while sheet in used_names:
                suffix = f"_{i}"
                sheet = base[:31 - len(suffix)] + suffix
                i += 1
            used_names.add(sheet)
            df.to_excel(writer, sheet_name=sheet)
    return buffer.getvalue()

def create_cache_zip():
    buffer = io.BytesIO()
    files  = list(CACHE_DIR.glob("*.json"))
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return buffer.getvalue(), len(files)

def load_cache_zip(uploaded_zip):
    with zipfile.ZipFile(io.BytesIO(uploaded_zip.read())) as zf:
        zf.extractall(CACHE_DIR)
    return len(list(CACHE_DIR.glob("*.json")))

def fuzzy_search(query, choices, limit=10, cutoff=50):
    """Return top matches above cutoff score."""
    if not query.strip():
        return []
    results = process.extract(query, choices, scorer=fuzz.WRatio, limit=limit)
    return [(name, score) for name, score, _ in results if score >= cutoff]

# ── STREAMLIT UI ──────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="MF NAV Downloader", page_icon="📈", layout="wide")
    st.title("📈 MF NAV Historical Downloader")
    st.caption("Data: AMFI · mfapi.in · Free, no API key needed")
    st.divider()

    setup()

    # ── Cache upload ──
    with st.expander("⚡ Speed up with cached data (optional but recommended)"):
        st.write("Upload a previously downloaded cache ZIP to skip re-downloading.")
        uploaded = st.file_uploader("Upload cache ZIP", type="zip", label_visibility="collapsed")
        if uploaded:
            count = load_cache_zip(uploaded)
            st.success(f"✅ Cache loaded — {count:,} funds ready.")

    st.divider()

    # ── Load AMFI ──
    with st.spinner("Loading data from AMFI..."):
        schemes = fetch_amfi_data()

    all_amfi_categories = sorted(set(s["category"] for s in schemes))
    all_scheme_names    = sorted(set(s["name"] for s in schemes))

    # ── Plan filter ──
    st.subheader("Plan filter")
    plan_mode = st.radio(
        "Which plan variants to include?",
        ["Both", "Direct only", "Regular only"],
        horizontal=True,
        label_visibility="collapsed",
    )
    st.caption("Filter applies to ALL downloads including custom categories added via search.")

    st.divider()

    # ── Predefined subcategory checkboxes ──
    st.subheader("Predefined categories")

    equity_cats = [k for k, v in CATEGORY_MAP.items() if v[0] == "Equity"]
    gold_cats   = [k for k, v in CATEGORY_MAP.items() if v[0] == "Gold"]
    hybrid_cats = [k for k, v in CATEGORY_MAP.items() if v[0] == "Hybrid"]
    debt_cats   = [k for k, v in CATEGORY_MAP.items() if v[0] == "Debt"]

    col_e, col_g, col_h, col_d = st.columns(4)
    with col_e:
        st.markdown("**Equity**")
        eq_selected = [c for c in equity_cats if st.checkbox(c.replace("Equity - ", ""), value=True, key=f"eq_{c}")]
    with col_g:
        st.markdown("**Gold**")
        gd_selected = [c for c in gold_cats if st.checkbox(c.replace("Gold - ", ""), value=True, key=f"gd_{c}")]
    with col_h:
        st.markdown("**Hybrid**")
        hy_selected = [c for c in hybrid_cats if st.checkbox(c.replace("Hybrid - ", ""), value=True, key=f"hy_{c}")]
    with col_d:
        st.markdown("**Debt**")
        dt_selected = [c for c in debt_cats if st.checkbox(c.replace("Debt - ", ""), value=True, key=f"dt_{c}")]

    st.divider()

    # ── Smart search ──
    st.subheader("🔍 Search for more categories or specific funds")
    st.caption("Type anything — AMFI category name, fund keyword. We'll suggest matches.")

    if "extra_amfi_cats" not in st.session_state:
        st.session_state.extra_amfi_cats = []
    if "extra_schemes" not in st.session_state:
        st.session_state.extra_schemes = []

    search_query = st.text_input("Search", label_visibility="collapsed", placeholder="e.g. 'large and mid', 'nippon quality', 'bharat bond'")

    if search_query:
        col_s1, col_s2 = st.columns(2)

        with col_s1:
            st.markdown("**Matching AMFI categories**")
            cat_matches = fuzzy_search(search_query, all_amfi_categories, limit=5, cutoff=40)
            if cat_matches:
                for cat, score in cat_matches:
                    already_added = cat in st.session_state.extra_amfi_cats
                    label = f"{cat} ({score:.0f}% match)"
                    if st.button(("✓ " if already_added else "➕ ") + label, key=f"add_cat_{cat}", disabled=already_added):
                        st.session_state.extra_amfi_cats.append(cat)
                        st.rerun()
            else:
                st.caption("_No category matches_")

        with col_s2:
            st.markdown("**Matching scheme names**")
            name_matches = fuzzy_search(search_query, all_scheme_names, limit=8, cutoff=60)
            if name_matches:
                for name, score in name_matches:
                    scheme = next((s for s in schemes if s["name"] == name), None)
                    if not scheme:
                        continue
                    already_added = any(x["code"] == scheme["code"] for x in st.session_state.extra_schemes)
                    label = f"{name[:70]} ({score:.0f}%)"
                    if st.button(("✓ " if already_added else "➕ ") + label, key=f"add_sch_{scheme['code']}", disabled=already_added):
                        st.session_state.extra_schemes.append(scheme)
                        st.rerun()
            else:
                st.caption("_No scheme matches_")

    # ── Show current custom selections ──
    if st.session_state.extra_amfi_cats or st.session_state.extra_schemes:
        st.markdown("**Custom additions:**")
        for cat in st.session_state.extra_amfi_cats:
            c1, c2 = st.columns([10, 1])
            c1.caption(f"📁 {cat}")
            if c2.button("✕", key=f"rm_cat_{cat}"):
                st.session_state.extra_amfi_cats.remove(cat)
                st.rerun()
        for sch in st.session_state.extra_schemes:
            c1, c2 = st.columns([10, 1])
            c1.caption(f"📄 {sch['name'][:80]}")
            if c2.button("✕", key=f"rm_sch_{sch['code']}"):
                st.session_state.extra_schemes = [x for x in st.session_state.extra_schemes if x["code"] != sch["code"]]
                st.rerun()

    st.divider()

    # ── Date ranges ──
    st.subheader("Date ranges")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        equity_start = st.date_input(
            "Equity / Gold / Hybrid — start date",
            value=date(2016, 6, 30),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
        )
    with col_d2:
        debt_start = st.date_input(
            "Debt — start date",
            value=date(2021, 6, 30),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
        )

    st.divider()

    # ── Build map and metrics ──
    all_selected = eq_selected + gd_selected + hy_selected + dt_selected
    by_display   = build_display_map(schemes, all_selected, st.session_state.extra_amfi_cats, st.session_state.extra_schemes)

    # Apply plan filter to every category's scheme list
    by_display   = {cat: apply_plan_filter(scheme_list, plan_mode) for cat, scheme_list in by_display.items()}
    by_display   = {cat: sl for cat, sl in by_display.items() if sl}  # drop empty

    if not by_display:
        st.warning("Select at least one category or add a custom one via search.")
        return

    total  = sum(len(v) for v in by_display.values())
    cached = sum(1 for cat_schemes in by_display.values() for s in cat_schemes if (CACHE_DIR / f"{s['code']}.json").exists())

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Sheets to generate", len(by_display))
    col_b.metric("Total schemes",       total)
    col_c.metric("Already cached",      f"{cached} / {total}")

    if st.button("🚀 Run Download", type="primary", use_container_width=True):
        st.write("#### Downloading NAV history...")
        progress_bar = st.progress(0)
        status       = st.empty()
        cat_status   = st.empty()

        collected = {}
        done      = 0
        debt_set  = set(dt_selected)

        for cat_name in sorted(by_display):
            cat_schemes = by_display[cat_name]
            start_date  = str(debt_start if cat_name in debt_set else equity_start)
            all_series  = []

            cat_status.markdown(f"**Category:** {cat_name} ({len(cat_schemes)} schemes)")

            for scheme in cat_schemes:
                code = scheme["code"]
                name = scheme["name"]
                status.caption(f"⬇ {name[:80]}")

                raw = fetch_nav_cached(code)
                time.sleep(SLEEP_SEC)

                if raw:
                    series = nav_to_series(raw, name, start_date)
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

        st.info(f"💡 Save the cache ZIP for next time to skip re-downloading {cache_count:,} funds.")

if __name__ == "__main__":
    main()