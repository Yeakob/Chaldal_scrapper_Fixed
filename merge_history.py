#!/usr/bin/env python3
"""Splice an existing Chaldal wide CSV onto the panel this repo maintains.

You already have a panel covering Jan 2024 - May 2026 in the same shape
(Name, Amount, then one column per date). Starting a fresh file would throw
that away and leave a gap between the old series and the new one. This merges
them on Name + Amount, the same SKU identity both use.

    python merge_history.py old_chaldal_data.csv

Writes chaldal_prices.csv and leaves the input untouched. Run it once, before
the first scheduled scrape, then commit the result.
"""
import os
import shutil
import sys

import pandas as pd

KEY_COLS = ["Name", "Amount"]
PANEL = os.environ.get("PANEL", "chaldal_prices.csv")

if len(sys.argv) < 2:
    sys.exit(__doc__)
src = sys.argv[1]

hist = pd.read_csv(src)
missing = [c for c in KEY_COLS if c not in hist.columns]
if missing:
    sys.exit(f"{src} is missing column(s): {missing}. Expected {KEY_COLS} "
             f"followed by one column per date.")

hist_dates = [c for c in hist.columns if c not in KEY_COLS]
hist = hist.drop_duplicates(subset=KEY_COLS, keep="first")

if os.path.exists(PANEL):
    shutil.copy(PANEL, PANEL + ".bak")
    cur = pd.read_csv(PANEL)
    cur_dates = [c for c in cur.columns if c not in KEY_COLS]

    overlap = sorted(set(hist_dates) & set(cur_dates))
    if overlap:
        # Prefer the freshly scraped values on any shared date
        hist = hist.drop(columns=overlap)
        print(f"{len(overlap)} overlapping date(s) kept from the current panel: "
              f"{overlap[0]} .. {overlap[-1]}")

    merged = cur.merge(hist, on=KEY_COLS, how="outer")
else:
    merged, cur_dates = hist, []

dates = sorted(c for c in merged.columns if c not in KEY_COLS)
merged = merged[KEY_COLS + dates].sort_values(KEY_COLS, kind="stable")
merged.to_csv(PANEL, index=False, encoding="utf-8-sig")

filled = merged[dates].notna().mean().mean()
print(f"history : {len(hist)} SKUs x {len(hist_dates)} dates from {src}")
print(f"panel   : {len(merged)} SKUs x {len(dates)} dates")
print(f"range   : {dates[0]} to {dates[-1]}")
print(f"filled  : {filled:.1%} of cells have a price")
print(f"wrote   : {PANEL}" + ("  (previous saved as .bak)" if cur_dates else ""))
