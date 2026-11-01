#!/usr/bin/env python3
"""Scrape one category with verbose scroll tracing, to diagnose short harvests.

    python debug_category.py https://chaldal.com/fresh-vegetable

Prints the product count after every scroll round. If the count is still
climbing when the loop stops, the stall threshold is too low for your network
and SCROLL_STALLS should go up. If it plateaus early and stays flat, that is
the real size of the category.
"""
import os
import sys
import time

os.environ.setdefault("SCROLL_STALLS", "8")

import chaldal_daily as cd   # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "https://chaldal.com/fresh-vegetable"

b = cd.Browser()
try:
    print(f"loading {url}")
    b.driver.get(url)
    time.sleep(3)

    if b._dismiss_gate():
        print("  location gate dismissed")
        time.sleep(2)

    n = b.driver.execute_script(cd.COUNT_JS)
    print(f"  initial render: {n} products")

    last, stalls, rounds = n, 0, 0
    while stalls < cd.SCROLL_STALLS and rounds < cd.SCROLL_MAX_ROUNDS:
        b.driver.execute_script("window.scrollBy(0, 1200);")
        time.sleep(cd.SCROLL_PAUSE)
        n = b.driver.execute_script(cd.COUNT_JS)
        rounds += 1
        if n <= last:
            stalls += 1
            print(f"  round {rounds:3d}: {n:5d}  (stall {stalls}/{cd.SCROLL_STALLS})")
        else:
            print(f"  round {rounds:3d}: {n:5d}  (+{n - last})")
            stalls, last = 0, n

    html = b.driver.page_source
    rows = cd.parse_page(html)
    priced = sum(1 for r in rows if r["Price (BDT)"] is not None)

    print()
    print(f"  rendered in DOM : {last}")
    print(f"  parsed rows     : {len(rows)}")
    print(f"  with a price    : {priced}")
    print(f"  out of stock    : {len(rows) - priced}")
    print(f"  scroll rounds   : {rounds}")
    if len(rows) < last:
        print(f"  [!] {last - len(rows)} rendered items did not parse — "
              f"check the pvName / subText / price selectors")
    if stalls < cd.SCROLL_STALLS:
        print("  [!] stopped on the round cap, not on a stall: still growing")
finally:
    b.close()
