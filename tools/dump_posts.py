"""
dump_posts.py

One-off diagnostic: fetch recent posts from the live account and print each
post's id, timestamp, classification (schedule/recap/summary_links/other),
and full text -- so a human can see exactly what's in the account's recent
timeline, beyond just whatever schedule_parser/run_pipeline chose to log a
NOTE about. Not part of the regular pipeline; run manually when something
needs eyeballing (e.g. "is there a plain-text schedule post Plan A is
missing, further down than page 1?").

Usage: python tools/dump_posts.py [max_pages]
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows Actions runners default stdout to the cp1252 console codepage,
# which can't encode Chinese characters -- crashes with UnicodeEncodeError
# as soon as we print(p.text). Force UTF-8 on stdout/stderr so the dump
# doesn't blow up partway through (errors="replace" as a last-resort
# safety net for anything still unmappable).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import weibo_scraper
import schedule_parser


def main() -> None:
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    # Debug: call the raw getIndex endpoint directly (bypassing
    # fetch_recent_posts) to see exactly what cardlistInfo/since_id the API
    # returns for page 1 -- fetch_recent_posts stops paginating whenever
    # since_id comes back falsy, so if pagination looks stuck at ~12 posts
    # no matter how high max_pages goes, this tells us whether the API
    # simply isn't giving us a since_id to continue with.
    url = f"{weibo_scraper.INDEX_URL}?type=uid&value={weibo_scraper.UID}&containerid={weibo_scraper.CONTAINERID}"
    raw = weibo_scraper._get_json(url)
    cardlist_info = (raw.get("data") or {}).get("cardlistInfo")
    print(f"DEBUG page-1 cardlistInfo = {cardlist_info!r}\n")

    posts = weibo_scraper.fetch_recent_posts(max_pages=max_pages)
    print(f"Fetched {len(posts)} post(s) across {max_pages} page(s).\n")
    for p in posts:
        result = schedule_parser.parse_post(p.text, source_post_id=p.mid)
        print("=" * 70)
        print(f"mid={p.mid}  created_at={p.created_at_raw}")
        print(f"classification={result.post_classification}  events={len(result.events)}")
        if result.notes:
            print(f"notes={result.notes}")
        print("-" * 70)
        print(p.text)
        print()


if __name__ == "__main__":
    main()
