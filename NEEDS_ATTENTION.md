# The clerk's website changed

**When:** August 26, 2026 at 13:28 UTC

**What happened:** The search form could not be operated.

Everything else still ran. Your foreclosure notices, tax-sale records and
parcel data were collected normally, so today's lead list is still usable --
it's just missing the deed/lien records from the Clerk's portal.

## What to do

Nothing technical. Send these three files to Claude and say
"the clerk portal broke, here are the files":

1. `data/landmark_discovery.json`
2. `data/landmark_failure.png`
3. `data/landmark_page_dump.html`

They contain a picture of the page and a list of every button and box on
it, which is everything needed to update the scraper.

## Will it fix itself?

Often, yes. The scraper re-learns the page from scratch on every run, so a
temporary outage or a slow-loading page usually clears by the next morning.
If this file is still here after two or three days, send the files along.
