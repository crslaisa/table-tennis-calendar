# Wang Chuqin / Sun Yingsha Schedule Calendar

Two subscribable calendar feeds (ICS/webcal), one per player, generated
automatically from the schedule posts of Weibo account
[@草莓牛奶特别甜](https://weibo.com/u/7360795486) (uid `7360795486`). See
`table_tennis_calendar_design_doc.md` (or the `_zh` version) for the full
design rationale; this README is about running and deploying what's built.

## How it fits together

```
weibo_scraper.py   -> fetches recent posts from the source account
schedule_parser.py -> classifies each post and extracts match events
state_store.py      -> diffs events against on-disk state, assigns stable
                        UIDs, gates publishing on confidence
ics_generator.py    -> renders each player's publishable events to a
                        hand-rolled, spec-compliant .ics file
run_pipeline.py     -> wires the four above into one script for cron
tools/promote.py    -> the manual side of the review queue (approve/reject)
subscribe/index.html -> the one-click-subscribe landing page
```

Each module also has a `if __name__ == "__main__":` smoke test you can run
standalone, e.g. `python3 schedule_parser.py`.

## Quick start (local)

No third-party dependencies — everything is Python 3 standard library only
(see "Why no `icalendar`/`requests`" below).

```bash
cd table_tennis_calendar
python3 run_pipeline.py
```

This fetches recent posts, updates `data/state_*.json`, and writes
`feeds/wangchuqin.ics` / `feeds/sunyingsha.ics`. Run it again at any time —
every step is idempotent; a post that produced no new information is a
no-op.

To check what's waiting on manual review:

```bash
python3 tools/promote.py --list
python3 tools/promote.py --approve <uid>   # publish it
python3 tools/promote.py --reject <uid>    # discard it, don't publish
```

## Deploying

The design doc recommends GitHub Pages (static hosting for the feeds + the
subscribe page) plus GitHub Actions on a cron schedule (running
`run_pipeline.py` every 30–60 minutes). Either works without any server to
maintain.

Before going live, replace two placeholder values with your real domain:

1. `state_store.py` — `UID_DOMAIN = "table-tennis-calendar.example"` (used
   to build each event's RFC5545 `UID`; doesn't need to be a real domain,
   but should be stable — changing it later will make every event look
   "new" to subscribers' calendar apps).
2. `subscribe/index.html` — `const FEED_BASE_URL = "https://table-tennis-calendar.example";`
   in the `<script>` block (must be the real HTTPS URL the feeds are served
   from, or the subscribe buttons will point nowhere).

A minimal GitHub Actions workflow:

```yaml
name: update-calendar
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch: {}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 table_tennis_calendar/run_pipeline.py
      - run: |
          git config user.name "calendar-bot"
          git config user.email "calendar-bot@users.noreply.github.com"
          git add table_tennis_calendar/feeds table_tennis_calendar/data
          git diff --cached --quiet || git commit -m "Update schedule feeds"
          git push
```

Serve `feeds/` and `subscribe/index.html` via GitHub Pages (or any static
host) at the domain you put in the two placeholders above.

## Testing notes / sandbox limitations

This was built in a sandbox whose outbound network is allowlisted and does
**not** include `weibo.cn`/`m.weibo.cn`, nor PyPI. Two consequences:

- `weibo_scraper.py` could not be run end-to-end here. Its request/response
  handling was verified by manually inspecting the same API URLs in a real
  browser session, and `schedule_parser.py`/`state_store.py`/
  `ics_generator.py` were all separately verified against real post text
  captured by browsing the live account directly (see
  `tests/live_capture_2026_06_29.py`). But **run `weibo_scraper.py`'s own
  smoke test from a normal, unrestricted environment** (your machine, any
  normal CI runner) before the first real deployment, to confirm the live
  API still matches the assumed JSON shape.
- No `pip install` was possible, which is why `ics_generator.py` hand-rolls
  the small subset of RFC5545 actually needed instead of depending on the
  `icalendar` package. If you have normal pip access in your deployment
  environment, switching to `icalendar` is a reasonable simplification, but
  not required — the hand-rolled version is fully spec-compliant and is
  exercised by `tests/`.

`tests/live_capture_2026_06_29.py` is a regression test built from real
text captured live on 2026-06-29, including a genuine Wang Chuqin match.
It's also what caught a real bug: the account's "links out to a separate
page" posts used the anchor text "微博正文" on that date, not "网页链接" as
in the earlier fixture — `schedule_parser._LINK_MARKER_RE` now matches
both. Worth re-confirming once `weibo_scraper.py` is run for real, since
that live observation was via the desktop web renderer, which may format
link anchor text slightly differently than the raw API JSON.

`schedule_parser_fix.py`, `schedule_parser_v2.py`, and
`schedule_parser_synced.py` in this directory are leftover (now emptied)
intermediate files from development (the canonical file is
`schedule_parser.py`) — safe to delete, kept only because this build
sandbox couldn't delete files. Same for `feeds/wangchuqin.ics` /
`feeds/sunyingsha.ics`: example output from a smoke test, not real data —
they'll be overwritten the first time `run_pipeline.py` runs for real.

## Weibo login wall (`ok: -100`)

The first real run from a residential/home network hit Weibo's anonymous
"visitor system" login wall: `weibo_scraper.py` got back `{"ok": -100, "url":
"https://passport.weibo.com/sso/signin?..."}` instead of post data. An
automatic fix (replaying the same genvisitor/incarnate handshake a browser
does on first page load) is built in, but in testing it was **not enough by
itself** — a real browser on the same network loaded the account fine, with
no login wall, while the from-scratch handshake still got walled.

If you hit this (the pipeline's error message will say "anonymous-visitor
login wall"), the fix is a one-time, manual cookie capture — about 2
minutes, no coding:

1. In Chrome/Edge, open `https://m.weibo.cn/u/7360795486` in a normal tab
   and confirm it loads real posts (not a login page).
2. Open DevTools (F12 or Ctrl+Shift+I) → **Network** tab → reload the page.
3. Click the request named `getIndex?...` in the list → **Headers** tab →
   under **Request Headers**, find `Cookie:` → copy its entire value.
4. Paste that value into a new plain-text file named exactly
   `weibo_cookie.txt`, saved directly inside this `table_tennis_calendar/`
   folder (next to `weibo_scraper.py`). No quotes, no extra text — just the
   cookie value.
5. Run the pipeline again (or re-trigger the GitHub Actions workflow).

`weibo_cookie.txt` is in `.gitignore` and must never be committed — it's a
real session-derived value, not a secret you "set" once and forget; if it
stops working again later (cookies expire), repeat steps 1–4 to refresh it.
`weibo_scraper.py` tries it first, then falls back to the automatic
handshake, then a no-cookie attempt, so removing the file just reverts to
the old (currently insufficient, but free to keep retrying) behavior rather
than breaking anything.

## Known limitations (by design, for the MVP)

- **No auto-removal/cancellation.** The source posts incremental
  announcements, never a canonical full-schedule list to diff against, so
  there's no reliable signal that a previously-announced match was
  cancelled. If this becomes a real problem in practice, the manual review
  workflow (`tools/promote.py`) is the place a "remove" command would go.
- **One hour placeholder duration** for every event, since the source never
  announces an end time — called out in each event's `DESCRIPTION` field
  so it's visible to anyone who looks, not just in code.
- **Single source account.** Xiaohongshu was dropped from scope entirely;
  this is intentionally dependent on `@草莓牛奶特别甜` continuing to post in
  a parseable format.
- **Calendar app refresh intervals are not configurable by this tool** —
  Google (~12–24h) and Outlook (~3–24h) refresh on their own schedule
  regardless of `REFRESH-INTERVAL`/`X-PUBLISHED-TTL` hints in the feed;
  Apple Calendar respects a user-adjustable interval, default weekly. This
  is documented 