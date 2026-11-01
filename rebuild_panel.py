#!/usr/bin/env python3
"""Rebuild chaldal_prices.csv from the daily raw snapshots.

The panel is updated incrementally by chaldal_daily.py. The raw files in
data/raw/ are never rewritten, so they stay the source of truth and the panel
can be regenerated from them at any time:

    python rebuild_panel.py
"""
import glob
import os
import sys

import pandas as pd

from chaldal_daily import clean_amount

KEY_COLS = ["Name", "Amount"]
RAW_DIR = os.environ.get("RAW_DIR", "data/raw")
PANEL = os.environ.get("PANEL", "chaldal_prices.csv")

files = sorted(glob.glob(os.path.join(RAW_DIR, "chaldal_*.csv")))
if not files:
    sys.exit(f"No raw snapshots in {RAW_DIR}/")

panel = None
for f in files:
    day = pd.read_csv(f)
    date = day["Date"].iloc[0]
    # Snapshots up to 2026-10-19 carry the delivery estimate in Amount
    # ("1 ltr3 hrs"). Strip it and keep the priced listing per SKU.
    day["Amount"] = day["Amount"].map(clean_amount)
    day = day.sort_values(KEY_COLS + ["Price (BDT)"], kind="stable",
                          na_position="last")
    part = (day[KEY_COLS + ["Price (BDT)"]]
            .drop_duplicates(subset=KEY_COLS, keep="first")
            .rename(columns={"Price (BDT)": date}))
    panel = part if panel is None else panel.merge(part, on=KEY_COLS, how="outer")

dates = sorted(c for c in panel.columns if c not in KEY_COLS)
panel = panel[KEY_COLS + dates].sort_values(KEY_COLS, kind="stable")
panel.to_csv(PANEL, index=False, encoding="utf-8-sig")

print(f"{len(files)} daily files -> {len(panel)} SKUs x {len(dates)} dates")
print(f"dates  : {dates[0]} to {dates[-1]}")
print(f"filled : {panel[dates].notna().mean().mean():.1%} of cells have a price")
print(f"wrote  : {PANEL}")
