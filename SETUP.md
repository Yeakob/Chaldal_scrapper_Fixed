# Chaldal daily price scrape

Runs the Chaldal scrape once a day on GitHub Actions and maintains a single
wide panel: one row per SKU, one column per date.

Sister repo to the Othoba scraper. Keep them separate — different sites,
different failure modes, and a break in one should not stop the other.

## Why this differs from the Othoba repo

Chaldal is a React SPA with infinite scroll, so a browser is mandatory here;
there is no static-HTML fast path. Three failure modes are handled explicitly,
all of which have caused trouble before:

**The location gate.** Chaldal can interrupt rendering with a "use my current
location / change city" modal. Nothing renders behind it, so the old selectors
returned zero products and it looked like a selector break. It was not — when
no products render, no product classes exist in the DOM regardless of what they
are called. The scraper detects the gate by its visible text and dismisses it
before parsing.

**504 gateway timeouts.** Server-side, not a selector issue. Retried with
backoff and counted separately, so the log distinguishes "Chaldal was down"
from "our parsing broke".

**Premature scroll termination.** Scrolling until `document.body.scrollHeight`
stops growing ends early, because the height can plateau while items are still
being appended. This counts rendered `.textWrapper` elements instead and stops
when the *product count* stabilises.

## Setup

1. New repository (private is fine).

2. Copy these in, keeping the paths:

   ```
   .github/workflows/daily-scrape.yml
   chaldal_daily.py
   rebuild_panel.py
   merge_history.py
   categories.txt
   requirements.txt
   data/raw/.gitkeep
   ```

3. Settings → Actions → General → Workflow permissions →
   **Read and write permissions**.

4. **Splice in your existing history first**, before any scheduled run:

   ```
   python merge_history.py path/to/your_existing_chaldal_data.csv
   git add chaldal_prices.csv && git commit -m "seed panel with 2024-2026 history"
   ```

   Your current file is already in this shape (Name, Amount, date columns), so
   it merges on the same SKU key. Skipping this starts a fresh panel and leaves
   a gap between the old series and the new one.

5. Actions tab → *Daily Chaldal scrape* → **Run workflow**. Do one manual run
   and read the log before trusting the schedule.

Runs at 03:30 Asia/Dhaka, half an hour after the Othoba job so the two never
compete for runner minutes. `cron` is UTC, so the file says `30 21 * * *`.

## Categories

`categories.txt` holds the URLs, one per line, `#` for comments. **All 214 are
active**, matching the Colab script exactly, so the SKU count lines up with the
existing dataset. A food index only needs the first twelve sections; comment
out the rest with `#` to shorten the run, but expect the SKU count to drop by
roughly half if you do.

## If a run comes back short

Compare against a known-good Colab run. If this repo yields fewer SKUs, trace a
single category:

```
python debug_category.py https://chaldal.com/fresh-vegetable
```

It prints the product count after every scroll round. If the count is still
climbing when the loop ends, the stall threshold is too low for your network —
raise `SCROLL_STALLS` in the workflow. If it plateaus and stays flat, that is
the true size of the category.

Four things have already been corrected after a short first run:

- **Category list.** Only the 78 food URLs were active; all 214 now are.
- **Scroll.** It jumped straight to the page bottom, which can skip the lazy
  loader's trigger points, and gave up after 80 rounds. It now scrolls in
  increments, stalls on the product count, and allows 600 rounds.
- **Stall tolerance.** Thresholds tuned on Colab under-wait on a GitHub runner,
  which sits much further from Chaldal. Defaults raised, and tunable via
  `SCROLL_STALLS` / `SCROLL_PAUSE`.
- **Out-of-stock items.** They were dropped entirely. They are now kept as rows
  with a blank price, so the SKU stays in the panel and the day records that it
  was listed but unavailable.

## Output

**`chaldal_prices.csv`** — the panel:

| Name | Amount | 2026-09-08 | 2026-09-09 | ... |
|---|---|---|---|---|
| Miniket Rice Premium | 25 kg | 1850 | 1875 |  |
| Soyabean Oil | 5 ltr | 890 | 890 |  |

One column added per run. New SKUs join with blanks before their first sighting;
delisted SKUs keep their history and go blank forward. Re-running a date
overwrites that column, so a retry after a partial failure is safe.

SKU identity is **Name + Amount**, matching the existing dataset. Note that the
same product in two pack sizes is correctly two rows.

**`data/raw/chaldal_YYYY-MM-DD.csv`** — the day's rows before merging. Never
rewritten, so `rebuild_panel.py` can regenerate the panel from them if the
incremental file is ever damaged.

**`data/dead_slugs.json`** — consecutive failure counts per URL. A slug is only
parked after 5 consecutive failures, and the counter resets the moment it
returns products again. Retiring a URL after one bad day would quietly shrink
the basket every time Chaldal has a wobble.

## Failing on purpose

The run exits non-zero when it collects fewer than `MIN_ROWS` SKUs, when the
location gate blocks more than a quarter of categories, or when more than a
third return 504s. GitHub emails you on a failed run.

This is deliberate. A silent partial harvest does not look like an error, it
looks like a quiet day — and a missing day can never be back-filled, because the
site only ever shows today's prices. That is how a multi-month hole appears in a
panel. Set `MIN_ROWS` to roughly 60% of a healthy run once you know your normal
count.

## Worth trying: JSON in page source

Chaldal very likely embeds product data as JSON in the page source. If so,
reading that would be more reliable than parsing a rendered grid and would
remove the scroll loop entirely. To check, load a category in a browser,
View Source, and search for a product name — if it appears inside a JSON blob
rather than only in HTML, the payload is there to be parsed.
