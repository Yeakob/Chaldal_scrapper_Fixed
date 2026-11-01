#!/usr/bin/env python3
"""
Daily Chaldal.com price scrape for scheduled, unattended runs.

Sister project to the Othoba scraper. Chaldal is a very different target:
a React SPA with infinite scroll, so every category needs a real browser and
Selenium is not a fallback here, it is the only path.

Three failure modes this handles explicitly, all of which have bitten before:

  * **The location gate.** Chaldal can interrupt rendering with a "use my
    current location / change city" modal. Products never appear behind it, so
    the old selectors returned zero and looked like a selector break. It is not
    — if nothing renders, no product classes exist in the DOM regardless of how
    they are named. The gate is detected and dismissed before parsing.

  * **504 gateway timeouts.** Server-side, not a selector problem. Retried with
    backoff and classified separately so the logs stay honest.

  * **Premature scroll termination.** Scrolling until `document.body.scrollHeight`
    stops growing ends early, because the height can plateau while items are
    still being appended. This counts rendered products instead.

Output is a wide panel, one column per date:

    Name | Amount | 2026-09-08 | 2026-09-09 | ...

matching the SKU-by-date layout the analysis pipeline already expects.

Environment variables (all optional):
    WORKERS=4              parallel headless Chrome instances
    MIN_ROWS=2500          fail the run below this many rows
    PANEL=chaldal_prices.csv
    RAW_DIR=data/raw
    KEEP_RAW=1
    CATEGORIES=categories.txt
    DEAD_FILE=data/dead_slugs.json
    MAX_CATEGORIES=0       0 = all (set a small number for a test run)
    DRY_RUN=0              1 = scrape and report, but write nothing
    DEBUG_DIR=data/debug   HTML + screenshot of categories that fail
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# --------------------------------------------------------------------------- config
WORKERS = int(os.environ.get("WORKERS", "4"))
MIN_ROWS = int(os.environ.get("MIN_ROWS", "2500"))
PANEL = os.environ.get("PANEL", "chaldal_prices.csv")
RAW_DIR = os.environ.get("RAW_DIR", "data/raw")
KEEP_RAW = os.environ.get("KEEP_RAW", "1") == "1"
CATEGORIES_FILE = os.environ.get("CATEGORIES", "categories.txt")
DEAD_FILE = os.environ.get("DEAD_FILE", "data/dead_slugs.json")
MAX_CATEGORIES = int(os.environ.get("MAX_CATEGORIES", "0")) or None
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
DEBUG_DIR = os.environ.get("DEBUG_DIR", "data/debug")
DEBUG_MAX = int(os.environ.get("DEBUG_MAX", "15"))

PAGE_WAIT = int(os.environ.get("PAGE_WAIT", "25"))
# Scrolling stops after SCROLL_STALLS consecutive rounds with no new products.
# Higher than the Colab defaults on purpose: a GitHub runner sits much further
# from Chaldal, so the same thresholds stop mid-category.
SCROLL_STALLS = int(os.environ.get("SCROLL_STALLS", "6"))
SCROLL_PAUSE = float(os.environ.get("SCROLL_PAUSE", "0.8"))
SCROLL_MAX_ROUNDS = int(os.environ.get("SCROLL_MAX_ROUNDS", "600"))
RECYCLE_EVERY = 25
DEAD_AFTER = 5          # consecutive failures before a slug is parked

DHAKA = timezone(timedelta(hours=6))

OK, EMPTY, GATE, SERVER_ERR, GONE, UNKNOWN = (
    "ok", "empty", "gate", "server", "gone", "unknown")


def log(msg):
    print(f"[{datetime.now(DHAKA).strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- browser
def make_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_experimental_option(
        "prefs", {"profile.managed_default_content_settings.images": 2})
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.page_load_strategy = "eager"
    # Optional explicit binaries. Unset, Selenium Manager picks a chromedriver
    # that matches the installed Chrome.
    if os.environ.get("CHROME_BIN"):
        opts.binary_location = os.environ["CHROME_BIN"]
    service = (Service(executable_path=os.environ["CHROMEDRIVER"])
               if os.environ.get("CHROMEDRIVER") else Service())
    driver = webdriver.Chrome(options=opts, service=service)
    driver.set_page_load_timeout(90)
    # Headless Chrome announces itself as "HeadlessChrome" in the User-Agent,
    # which bot filters block on sight. Send the ordinary desktop string.
    try:
        ua = driver.execute_script("return navigator.userAgent;")
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": ua.replace("HeadlessChrome", "Chrome"),
            "acceptLanguage": "en-US,en;q=0.9"})
    except Exception:
        pass
    return driver


def driver_info(driver):
    caps = driver.capabilities or {}
    chrome = caps.get("browserVersion", "?")
    drv = (caps.get("chrome") or {}).get("chromedriverVersion", "?").split(" ")[0]
    try:
        ua = driver.execute_script("return navigator.userAgent;")
    except Exception:
        ua = "?"
    return f"Chrome {chrome}, chromedriver {drv}, UA: {ua}"


_debug_lock = threading.Lock()
_debug_count = [0]


def dump_debug(driver, url, verdict, html):
    """Save what Chaldal actually served for a failed category.

    A run that fails on the guard rails only says *that* categories came back
    empty. These files say *why*: a block page, a captcha, a location modal,
    or a redesigned grid with new class names.
    """
    with _debug_lock:
        if _debug_count[0] >= DEBUG_MAX:
            return
        _debug_count[0] += 1
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stem = os.path.join(DEBUG_DIR, f"{verdict}_{url.rstrip('/').split('/')[-1]}")
        with open(stem + ".html", "w", encoding="utf-8") as fh:
            fh.write(html or "")
        driver.save_screenshot(stem + ".png")
    except Exception:
        pass
    try:
        title = driver.title
    except Exception:
        title = "?"
    text = re.sub(r"\s+", " ", BeautifulSoup(html or "", "lxml").get_text(" "))[:200]
    log(f"  [debug] {url} -> {verdict}; title={title!r}; "
        f"html={len(html or '')} bytes; text={text!r}")


# The gate renders as a modal with one of these calls to action. Matching on
# visible text rather than a class name is deliberate: the copy is far more
# stable than Chaldal's generated CSS class names.
GATE_PATTERNS = ("use my current location", "change city",
                 "select your location", "choose your area")

DISMISS_JS = """
var wanted = ['continue', 'skip', 'dhaka', 'use my current location'];
var nodes = document.querySelectorAll('button, a, div[role=button], span');
for (var i = 0; i < nodes.length; i++) {
    var t = (nodes[i].innerText || '').trim().toLowerCase();
    if (!t || t.length > 40) continue;
    for (var j = 0; j < wanted.length; j++) {
        if (t === wanted[j] || t.indexOf(wanted[j]) === 0) {
            nodes[i].click();
            return t;
        }
    }
}
return null;
"""

COUNT_JS = "return document.getElementsByClassName('textWrapper').length;"


def looks_like_gate(html):
    low = html[:20000].lower()
    return any(p in low for p in GATE_PATTERNS)


def looks_like_504(html):
    low = html[:4000].lower()
    return ("504" in low and "gateway" in low) or "gateway time-out" in low


def classify(html):
    if not html:
        return UNKNOWN
    if "textWrapper" in html:
        return OK
    if looks_like_504(html):
        return SERVER_ERR
    if looks_like_gate(html):
        return GATE
    if "not found" in html[:4000].lower():
        return GONE
    return EMPTY


class Browser:
    """One headless Chrome, with gate handling and session recovery."""

    def __init__(self):
        self.driver = make_driver()
        self.pages = 0

    def _restart(self):
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = make_driver()
        self.pages = 0

    def _dismiss_gate(self):
        try:
            clicked = self.driver.execute_script(DISMISS_JS)
            if clicked:
                time.sleep(1.5)
                return True
        except WebDriverException:
            pass
        return False

    def _scroll_all(self):
        """Scroll until the *product count* stops growing.

        Three things here were tuned against real truncation:

        1. Scroll in increments, not a jump to the bottom. Chaldal's lazy
           loader fires on the scroll positions it passes through; jumping
           straight to document.body.scrollHeight can skip those triggers so
           later batches never request.

        2. Stall on the product *count*, not on scrollHeight. The page height
           can plateau while items are still being appended, and a
           height-based loop stops early and silently truncates.

        3. Be patient. These thresholds were originally tuned on Colab, whose
           latency to Chaldal is much lower than a GitHub runner's. The same
           values under-wait in CI and stop mid-category, which is exactly how
           a run comes back short without ever looking like an error.

        SCROLL_STALLS and SCROLL_PAUSE override the defaults.
        """
        last, stalls, rounds = 0, 0, 0
        while stalls < SCROLL_STALLS and rounds < SCROLL_MAX_ROUNDS:
            try:
                self.driver.execute_script("window.scrollBy(0, 1200);")
                time.sleep(SCROLL_PAUSE)
                n = self.driver.execute_script(COUNT_JS)
            except WebDriverException:
                break
            if n <= last:
                stalls += 1
                # Before giving up, jump to the bottom once: it re-triggers the
                # loader if the increments have fallen behind the content.
                if stalls == SCROLL_STALLS - 1:
                    try:
                        self.driver.execute_script(
                            "window.scrollTo(0, document.body.scrollHeight);")
                        time.sleep(SCROLL_PAUSE * 2)
                        n = self.driver.execute_script(COUNT_JS)
                        if n > last:
                            stalls, last = 0, n
                    except WebDriverException:
                        break
            else:
                stalls, last = 0, n
            rounds += 1

        if rounds >= SCROLL_MAX_ROUNDS:
            log(f"  [!] scroll hit the {SCROLL_MAX_ROUNDS}-round cap at "
                f"{last} products — the category may be truncated")
        return last

    def get(self, url, max_attempts=4):
        """Load a category, dismiss any gate, scroll it out. -> (html, verdict)"""
        for attempt in range(1, max_attempts + 1):
            try:
                if self.pages >= RECYCLE_EVERY:
                    self._restart()
                self.driver.get(url)
                self.pages += 1

                try:
                    WebDriverWait(self.driver, PAGE_WAIT).until(
                        EC.presence_of_element_located((By.CLASS_NAME, "textWrapper")))
                except Exception:
                    # Nothing rendered. Before blaming selectors, check the gate.
                    if self._dismiss_gate():
                        try:
                            WebDriverWait(self.driver, PAGE_WAIT).until(
                                EC.presence_of_element_located(
                                    (By.CLASS_NAME, "textWrapper")))
                        except Exception:
                            pass

                html = self.driver.page_source
                verdict = classify(html)

                if verdict == OK:
                    self._scroll_all()
                    return self.driver.page_source, OK
                if verdict == GONE:
                    return html, GONE
                if verdict in (GATE, SERVER_ERR, EMPTY, UNKNOWN):
                    # 504s and gates are both worth another attempt; a genuinely
                    # empty category will simply come back EMPTY again.
                    if attempt == max_attempts:
                        dump_debug(self.driver, url, verdict, html)
                        return html, verdict
            except WebDriverException:
                self._restart()

            time.sleep(2 * attempt + random.uniform(0, 1.5))

        return "", UNKNOWN

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------- parsing
# The pack-size element also carries Chaldal's delivery estimate, and
# get_text() glues the two together: "1 ltr3 hrs", "500 gmNext Day". The
# estimate changes from day to day, so left in place it splits one product
# into a new panel row every time the delivery time moves. Strip it.
ETA_SUFFIX = re.compile(
    r"\s*(?:\d+\s*(?:hrs?|hours?|mins?|minutes?|days?)|next\s*day|tomorrow|today)\s*$",
    re.I)


def clean_amount(amount):
    if not isinstance(amount, str):
        return amount
    prev = None
    while prev != amount:             # in case two estimates are stacked
        prev, amount = amount, ETA_SUFFIX.sub("", amount).strip()
    return amount


def parse_page(html):
    """Extract (Name, Amount, Price) triples from a rendered category page."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for w in soup.find_all("div", class_="textWrapper"):
        name_tag = w.find(class_="pvName")
        if not name_tag:
            continue
        amount_tag = w.find(class_="subText")
        if not amount_tag:
            continue
        amount = clean_amount(amount_tag.get_text(strip=True))
        # A pack size without a digit is not a pack size
        if not any(c.isdigit() for c in amount):
            continue

        # An out-of-stock item renders with no price or a zero. Keep the SKU
        # with a blank price rather than dropping the row: the product still
        # exists in the catalogue that day, and dropping it both understates
        # the SKU count and loses the fact that it was listed but unavailable.
        price = None
        price_tag = w.find(class_="price")
        if price_tag:
            raw = price_tag.get_text(strip=True).replace("৳", "").replace(",", "")
            raw = raw.split("+")[0].strip()       # "120 + 20" -> base price
            m = re.search(r"\d+(?:\.\d+)?", raw)
            if m:
                val = float(m.group(0))
                price = val if val > 0 else None

        rows.append({
            "Name": name_tag.get_text(strip=True),
            "Amount": amount,
            "Price (BDT)": price,
        })
    return rows


# --------------------------------------------------------------------------- panel
KEY_COLS = ["Name", "Amount"]


def update_panel(df_today, panel_path, date_str):
    """Merge one day's prices into the wide panel as a new date column.

    Layout: Name | Amount | 2026-09-08 | 2026-09-09 | ...

    Chaldal exposes no product id, so the SKU key is Name + Amount — the same
    identity the existing dataset uses, which keeps this panel compatible with
    it. Re-running a date overwrites that column rather than duplicating it.

    Returns (panel, n_added, n_matched).
    """
    today = (df_today[["Name", "Amount", "Price (BDT)"]]
             .rename(columns={"Price (BDT)": date_str})
             .drop_duplicates(subset=KEY_COLS, keep="first"))

    if os.path.exists(panel_path):
        panel = pd.read_csv(panel_path)
        if date_str in panel.columns:
            panel = panel.drop(columns=[date_str])
        # Older panels carry the delivery estimate in Amount. Normalise and
        # collapse those split rows so today's clean keys line up with history.
        panel["Amount"] = panel["Amount"].map(clean_amount)
        if panel.duplicated(subset=KEY_COLS).any():
            before = len(panel)
            panel = panel.groupby(KEY_COLS, as_index=False, sort=False).first()
            log(f"panel: merged {before - len(panel)} rows split by the "
                f"delivery-time suffix")

        known = set(zip(panel["Name"], panel["Amount"]))
        pairs = list(zip(today["Name"], today["Amount"]))
        matched = sum(1 for p in pairs if p in known)
        added = len(today) - matched

        merged = panel.merge(today, on=KEY_COLS, how="outer")
    else:
        merged, added, matched = today, len(today), 0

    dates = sorted(c for c in merged.columns if c not in KEY_COLS)
    merged = merged[KEY_COLS + dates]
    merged = merged.sort_values(KEY_COLS, kind="stable").reset_index(drop=True)
    merged.to_csv(panel_path, index=False, encoding="utf-8-sig")
    return merged, added, matched


# --------------------------------------------------------------------------- dead slugs
def load_dead():
    if os.path.exists(DEAD_FILE):
        try:
            return json.load(open(DEAD_FILE))
        except Exception:
            return {}
    return {}


def save_dead(dead):
    os.makedirs(os.path.dirname(DEAD_FILE) or ".", exist_ok=True)
    json.dump(dead, open(DEAD_FILE, "w"), indent=1, sort_keys=True)


# --------------------------------------------------------------------------- main
lock = threading.Lock()


def load_categories():
    if not os.path.exists(CATEGORIES_FILE):
        sys.exit(f"Missing {CATEGORIES_FILE}")
    urls = []
    for line in open(CATEGORIES_FILE):
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def main():
    started = time.time()
    run_date = datetime.now(DHAKA).strftime("%Y-%m-%d")
    log(f"Run for {run_date} (Asia/Dhaka)")

    urls = load_categories()
    dead = load_dead()

    # A slug is only parked after repeated failures. Retiring one on a single
    # bad day would quietly shrink the basket every time Chaldal has a wobble.
    active = [u for u in urls if dead.get(u, 0) < DEAD_AFTER]
    parked = len(urls) - len(active)
    if MAX_CATEGORIES:
        active = active[:MAX_CATEGORIES]
    log(f"{len(active)} categories to scrape ({parked} parked as dead)"
        + ("  [DRY RUN: nothing will be written]" if DRY_RUN else ""))
    probe = make_driver()
    try:
        log(driver_info(probe))
    finally:
        probe.quit()

    all_rows = []
    stats = {OK: 0, EMPTY: 0, GATE: 0, SERVER_ERR: 0, GONE: 0, UNKNOWN: 0}
    gate_hits = [0]

    def worker(chunk, wid):
        browser = Browser()
        local = []
        try:
            for url in chunk:
                t0 = time.time()
                try:
                    html, verdict = browser.get(url)
                except Exception as e:
                    log(f"[W{wid}] ERR   {url}: {type(e).__name__}: {e}")
                    with lock:
                        stats[UNKNOWN] += 1
                        dead[url] = dead.get(url, 0) + 1
                    continue

                rows = parse_page(html) if verdict == OK else []
                local += rows

                with lock:
                    stats[verdict] = stats.get(verdict, 0) + 1
                    if verdict == GATE:
                        gate_hits[0] += 1
                    if rows:
                        dead.pop(url, None)               # healthy again
                    elif verdict in (GONE, EMPTY):
                        dead[url] = dead.get(url, 0) + 1
                log(f"[W{wid}] {verdict.upper():6s} {url.split('/')[-1]:<32s} "
                    f"{len(rows):5d} items  {time.time()-t0:5.1f}s")
                time.sleep(random.uniform(0.3, 0.9))
        finally:
            browser.close()
        with lock:
            all_rows.extend(local)

    chunks = [active[i::WORKERS] for i in range(WORKERS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(worker, ch, i) for i, ch in enumerate(chunks) if ch]
        for f in as_completed(futs):
            f.result()

    if not DRY_RUN:
        save_dead(dead)

    if not all_rows:
        log(f"FATAL: no rows collected  (ok={stats[OK]} empty={stats[EMPTY]} "
            f"gate={stats[GATE]} 504={stats[SERVER_ERR]} gone={stats[GONE]} "
            f"err={stats[UNKNOWN]}). See the chaldal-debug artifact.")
        return 1

    # The same SKU can be listed in two categories. Sort priced rows ahead of
    # blank ones so a real price always wins over an out-of-stock listing.
    df = (pd.DataFrame(all_rows)
          .sort_values(KEY_COLS + ["Price (BDT)"], kind="stable",
                       na_position="last")
          .drop_duplicates(subset=KEY_COLS, keep="first")
          .reset_index(drop=True))
    df.insert(0, "Date", run_date)

    if DRY_RUN:
        priced = df["Price (BDT)"].notna().sum()
        log(f"DRY RUN: {len(df)} SKUs ({priced} priced) from {stats[OK]} ok "
            f"categories; sample: {df[['Name', 'Amount', 'Price (BDT)']].head(3).values.tolist()}")
        return 0 if len(df) >= MIN_ROWS and stats[OK] else 1

    if KEEP_RAW:
        os.makedirs(RAW_DIR, exist_ok=True)
        df.to_csv(os.path.join(RAW_DIR, f"chaldal_{run_date}.csv"),
                  index=False, encoding="utf-8-sig")

    panel, added, matched = update_panel(df, PANEL, run_date)

    mins = (time.time() - started) / 60
    log("-" * 60)
    priced = df["Price (BDT)"].notna().sum()
    log(f"SKUs today    : {len(df)} listed, {priced} with a price "
        f"({len(df) - priced} out of stock)")
    log(f"categories    : ok={stats[OK]} empty={stats[EMPTY]} gate={stats[GATE]} "
        f"504={stats[SERVER_ERR]} gone={stats[GONE]} err={stats[UNKNOWN]}")
    log(f"panel         : {len(panel)} SKUs x "
        f"{sum(c not in KEY_COLS for c in panel.columns)} dates")
    log(f"  matched     : {matched} already in the panel")
    log(f"  new         : {added} added as new rows")
    log(f"median price  : {df['Price (BDT)'].median():.0f} BDT")
    log(f"elapsed       : {mins:.1f} min")
    log(f"written       : {PANEL}")
    log("-" * 60)

    # Guard rails. A silent partial harvest is worse than a loud failure: a
    # missing day cannot be back-filled, because the site only shows today.
    if len(df) < MIN_ROWS:
        log(f"FAIL: only {len(df)} SKUs, expected at least {MIN_ROWS}. "
            f"See the chaldal-debug artifact.")
        return 1
    if gate_hits[0] > len(active) / 4:
        log(f"FAIL: location gate blocked {gate_hits[0]} categories")
        return 1
    if stats[SERVER_ERR] > len(active) / 3:
        log(f"FAIL: {stats[SERVER_ERR]} categories returned 504s")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
