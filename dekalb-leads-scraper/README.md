# DeKalb County Motivated Seller Scraper

Finds distressed property owners in DeKalb County, Georgia every morning, matches
them to county parcel records for a mailing address, scores how motivated they
are likely to be, and hands you a call list and a CRM import file.

Built for **Revamp Realty Group / Revamp Home Buyers**. Runs itself on GitHub —
there is nothing to install and nothing to remember to do.

---

## What you get each morning

Around **7:15am Eastern**, without you doing anything:

| | |
|---|---|
| **Dashboard** | A web page listing every lead, sorted by motivation, with the top lead shown as a "call first" card. Works on your phone. |
| **CRM file** | `data/ghl_leads.csv`, formatted for GoHighLevel. One row per owner. |
| **Run summary** | A plain-English report on the Actions tab: how many leads, how many are hot, which sources worked. |

Your dashboard address is:

```
https://terrellkday.github.io/Dekalb-Leads-Scraper/
```

---

## First time through

Two settings had to be switched on in GitHub. If you have already done these, skip ahead.

**1. Let the scraper save its results**
Settings → Actions → General → scroll to **Workflow permissions** →
**Read and write permissions** → Save.

**2. Turn on the dashboard**
Settings → Pages → **Source** → **GitHub Actions**. No save button; it applies instantly.

**Then collect your first batch.** Don't wait until tomorrow:

1. Open the **Actions** tab
2. Click **Daily DeKalb Lead Scrape**
3. Click **Run workflow**
4. Set *lookback days* to **30** — the default of 7 only looks back a week, and
   30 gives you a real list to start from
5. Click the green **Run workflow** button

It takes a few minutes. When it finishes, open the run and read the summary.

---

## Using it day to day

**The dashboard** is the fast path. Filter by minimum score, lead type, city,
whether the owner lives at the property, and how recently the document was
filed. Search matches owner names, addresses, parcel numbers and document
numbers. Click any column header to sort.

Scores are color-coded:

| Score | Meaning |
|---|---|
| 80–100 | Very high motivation — call these first |
| 60–79 | High |
| 40–59 | Moderate |
| below 40 | Lower |

**The CRM file** is the button marked *Download CRM file*, or grab
`data/ghl_leads.csv` from the repository. It imports into GoHighLevel directly.

**Absentee owners** are worth noticing. When the owner's mailing address differs
from the property address, they don't live there — usually a landlord, an heir,
or someone who has already moved on. Those convert better than average, so the
dashboard has a filter just for them.

---

## When something needs you

If a file called **`NEEDS_ATTENTION.md`** appears at the top of the repository,
open it. It explains in plain English what broke and what to do — usually just
"send Claude these three files." It deletes itself once the problem clears.

Most problems fix themselves. The scraper relearns the Clerk's website on every
run, so a slow page or a brief outage usually clears by the next morning. If
that file is still there after two or three days, it's real.

**A bad day cannot erase a good list.** If every source fails, the scraper keeps
the leads already published and marks them as older rather than overwriting them
with an empty file.

---

## Where the leads come from

| Source | What it provides | Notes |
|---|---|---|
| **Clerk of Superior Court** — LandmarkWeb | Lis pendens, judgments, liens, tax executions, probate deeds | The county's official land records |
| **Georgia Public Notice** | Foreclosure advertisements, tax sales, probate notices | Georgia Press Association. The Champion, DeKalb's legal organ, publishes here |
| **DeKalb Tax Commissioner** | Tax sale list with parcel IDs and amounts owed | Parcel IDs make these the most reliable matches |
| **DeKalb County GIS** | Owner names, property addresses, mailing addresses | Official parcel data, ~228,000 properties |

**Why foreclosures come from a newspaper rather than the courthouse.** Georgia
uses *non-judicial* foreclosure. There is usually no court case and no recorded
"notice of foreclosure" to find. Instead the lender must advertise a **Notice of
Sale Under Power** in the county's legal organ for four consecutive weeks before
selling on the courthouse steps the first Tuesday of the month. That newspaper
ad is the real signal — which is why this scraper reads legal notices instead of
only searching deed records the way a Florida-built scraper would.

Because the same ad runs four weeks running, the scraper collapses the repeats
into one lead and keeps the earliest publication date.

---

## How leads are scored

Every lead starts at **30** and climbs:

| | |
|---|---|
| Each major distress signal | +10 |
| Lis pendens **and** foreclosure on the same property | +20 |
| Three or more different kinds of distress | +20 |
| Amount owed over $100,000 | +15 |
| Amount owed over $50,000 | +10 |
| Filed within the lookback window | +5 |
| Property address successfully matched | +5 |
| Absentee owner | +5 |

Capped at 100. Two adjustments pull scores down: a released or cancelled lien
drops to 40% of its score, and a notice of commencement caps at 45 — someone
renovating a property is investing in it, not leaving it.

**Signals stack across documents.** If a judgment is filed Monday, a lis pendens
Wednesday, and a foreclosure ad runs Thursday — all on the same house — that is
**one** motivated seller carrying three signals and a stacked score, not three
separate leads. The scraper groups by parcel ID first, then property address,
then owner name.

The CRM file goes further and collapses to **one row per person**. If the same
owner is distressed on several properties, you get a single contact flagged
*"Owns N distressed properties"* rather than duplicate contacts.

---

## What's in the repository

```
scraper/fetch.py                    the scraper
scraper/requirements.txt            what it needs installed
.github/workflows/scrape.yml        the daily schedule
dashboard/index.html                the web dashboard
dashboard/records.json              what the dashboard reads
data/records.json                   your lead list
data/ghl_leads.csv                  your CRM import file
data/seen_documents.json            which documents have been seen before
data/landmark_learned_selectors.json how it learned to drive the Clerk's site
```

The last two look like junk but must not be deleted. Runners are wiped after
every run, so those files are the scraper's only memory. Without them every lead
looks brand new every day and the site has to be relearned from scratch nightly.

---

## Running it on your own computer

Only needed for debugging. The scheduled run needs none of this.

```bash
git clone https://github.com/terrellkday/Dekalb-Leads-Scraper.git
cd Dekalb-Leads-Scraper

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium

python scraper/fetch.py
```

**Watch it work.** To see the browser instead of running it invisibly:

```bash
HEADLESS=false python scraper/fetch.py
```

A Chromium window opens and you can watch it accept the disclaimer, fill in the
date range and page through results. This is the fastest way to understand a
failure.

**Preview the dashboard locally.** Opening `index.html` by double-clicking it
will not work — browsers block local pages from reading files next to them.
Instead:

```bash
cd dashboard
python3 -m http.server 8000
```

Then open `http://localhost:8000`.

---

## Settings you can change

Set these as environment variables, or edit the top of `scraper/fetch.py`.

| Setting | Default | What it does |
|---|---|---|
| `LOOKBACK_DAYS` | `7` | How many days back to search |
| `HEADLESS` | `true` | `false` shows the browser |
| `MAX_RETRIES` | `3` | Attempts before a source is given up on |
| `POLITE_DELAY` | `1.2` | Seconds between page requests |
| `PARCEL_CACHE_HOURS` | `72` | How long parcel data is reused before re-downloading |
| `SKIP_SOURCES` | *(none)* | Skip sources: `LANDMARK`, `NOTICES`, `TAX` |
| `LOG_LEVEL` | `INFO` | `DEBUG` for much more detail |

To change the daily schedule, edit the `cron` line in
`.github/workflows/scrape.yml`. **GitHub cron runs on UTC time, not Eastern.**
`17 11 * * *` is 7:17am Eastern in summer, 6:17am in winter — GitHub does not
adjust for daylight saving.

---

## Document types

The Clerk indexes documents under names like `NOTICE OF SALE UNDER POWER` or
`WRIT OF FIERI FACIAS`. `DOCUMENT_TYPE_MAP` near the top of `fetch.py` sorts
those into twelve lead categories: `LP`, `FC`, `TAX`, `TAXLIEN`, `JUD`, `LIEN`,
`MECH`, `HOA`, `MED`, `PRO`, `NOC`, `RELLP`.

Matching is case-insensitive and works on partial text, so `CLAIM OF LIEN -
MATERIALMAN` matches `MATERIALMAN` and files as a mechanic's lien. Order
matters: specific categories are checked before general ones, so a release of
lis pendens is never mistaken for a lis pendens.

**Anything unrecognized is written to `data/unmapped_doc_types.json`** rather
than silently dropped. After a few weeks, look at that file — it shows the real
DeKalb document names the scraper is seeing but ignoring. Adding a useful one is
a single line:

```python
"HOA": [
    "HOA LIEN",
    "HOMEOWNERS ASSOCIATION LIEN",
    "PROPERTY OWNERS ASSOCIATION",
    "YOUR NEW TERM HERE",
],
```

---

## Troubleshooting

**No leads at all.** Read the run summary on the Actions tab — it names which
sources failed and why. If the Clerk's site was the only failure, the other
sources still produced leads.

**The dashboard shows an error.** You are probably opening the file directly
instead of through the web address. Use the GitHub Pages link at the top of this
file.

**Leads have no property address.** Owner name matching is deliberately
conservative — if a name is ambiguous it leaves the address blank rather than
guessing, because attaching a foreclosure to the wrong house is worse than a
blank field. Records from the tax sale list always match, because they carry
parcel IDs.

**The Clerk's website changed.** This is the one thing that genuinely needs a
human eventually, but not immediately. The scraper finds the date boxes by
typing a date into every field and seeing which ones hold it, tries each search
button until results appear, and saves whatever worked. If the site changes, the
saved configuration is thrown out and it starts fresh. Two search methods are
built in — filing date first, then document search.

If all of that fails, it writes `NEEDS_ATTENTION.md` plus a screenshot, a page
dump, and a list of every control on the page. Send those three files to Claude
and the mapping can be updated. You don't need to interpret them.

---

## Data format

`records.json`:

```json
{
  "fetched_at": "2026-08-26T11:17:00+00:00",
  "source": "DeKalb County GA Public Records",
  "date_range": { "start": "2026-08-19", "end": "2026-08-26" },
  "total": 146,
  "with_address": 138,
  "sources_report": { "landmarkweb": { "ok": true, "count": 98, "error": "" } },
  "records": [
    {
      "doc_num": "2026-40001",
      "doc_type": "NOTICE OF SALE UNDER POWER",
      "filed": "2026-08-22",
      "cat": "FC",
      "cat_label": "Pre-Foreclosure",
      "owner": "TATE TYRONE",
      "grantee": "Acme Servicing LLC",
      "amount": 184500.0,
      "legal": "LOT 4 BLOCK C",
      "parcel_id": "15 186 03 002",
      "prop_address": "3990 GLENWOOD RD",
      "prop_city": "DECATUR",
      "prop_state": "GA",
      "prop_zip": "30032",
      "mail_address": "PO BOX 88",
      "mail_city": "ATLANTA",
      "mail_state": "GA",
      "mail_zip": "30303",
      "owner_occupied": false,
      "clerk_url": "https://deeds.dekalbcountyga.gov/LandmarkWeb",
      "source": "Georgia Public Notice (legal organ advertisement)",
      "foreclosure_sale_date": "2026-10-06",
      "status": "active",
      "match_confidence": 0.92,
      "flags": ["Pre-foreclosure", "Absentee owner", "New this week"],
      "score": 100
    }
  ]
}
```

`match_confidence` runs 0 to 1 and records how the address was found: `1.0` for
a parcel ID match, `0.92` for a property address, `0.85` for an exact owner
name, lower for alternate name orderings.

**CRM file columns:** First Name, Last Name, Mailing Address, Mailing City,
Mailing State, Mailing Zip, Property Address, Property City, Property State,
Property Zip, Lead Type, Document Type, Date Filed, Document Number,
Amount/Debt Owed, Seller Score, Motivated Seller Flags, Source, Public Records URL.

Company names are never split. `WESLEY CHAPEL VENTURES LLC` goes into First Name
whole with Last Name blank, rather than being mangled into "Wesley Chapel".

---

## Pushing leads straight to GoHighLevel

Optional. The CSV works without it. To enable, add two repository secrets under
Settings → Secrets and variables → Actions:

```
GHL_API_KEY
GHL_LOCATION_ID
```

Only leads scoring 60 or above are pushed by default (`GHL_MIN_SCORE`).

---

## Limitations — read this part

**Public records are messy.** Names are inconsistent, addresses are abbreviated
differently across sources, and some documents have no address at all. Not every
lead will match a parcel.

**Probate coverage is partial.** This reads probate *deeds* filed in land
records. Newly opened estates live at DeKalb Probate Court, which is a separate
system. A future module could add it — the code is structured for that — but no
unofficial probate API has been invented here to paper over the gap.

**Legal notices are extracted from prose.** Foreclosure ads are paragraphs of
legal text, not database fields. Borrower names, amounts and sale dates are
pulled with pattern matching and will occasionally be wrong or missing.

**Not every source works every day.** County websites go down. The design
assumption is partial success, not perfection — one failure never stops the rest.

**Verify before you act.** This is a research tool. Everything here is indexed
public record information that can be out of date, superseded, or simply wrong.
Confirm ownership, liens and payoff amounts before making an offer or spending
money. Nothing here is legal or financial advice.

**Use it politely.** The scraper rate-limits itself, retries with backoff, and
does not bypass CAPTCHAs, logins, or paywalls. Please keep it that way — these
are public services, and hammering them is how access gets restricted for
everyone.
