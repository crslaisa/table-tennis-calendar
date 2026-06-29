"""
weibo_scraper.py

Fetches posts from the confirmed primary source account
@草莓牛奶特别甜 (uid 7360795486) via the public, no-login m.weibo.cn
mobile-web JSON API.

IMPORTANT — network notes from development and first real deployment:

1. (build-sandbox note) This module could not be exercised end-to-end inside
   the original build sandbox, because that sandbox's outbound network is
   allowlisted and does not include weibo.cn. The JSON shape and endpoints
   below were verified by manually loading the same URLs in a real browser
   session during development.

2. (first GitHub-hosted-runner deployment) The very first live run, from a
   GitHub-hosted runner (datacenter IP), got an empty response body instead
   of JSON. Fix: m.weibo.cn's API silently rejects requests with no Referer
   (or the wrong UA/Pwa headers) -- see the headers in _get_json below.

3. (first self-hosted-runner deployment) The first live run from the user's
   own machine got back valid JSON, but with {"ok": -100, "url":
   "https://passport.weibo.com/sso/signin?..."} instead of post data. This
   is Weibo's anonymous "visitor system" login wall. The obvious fix --
   replaying the genvisitor/incarnate handshake a real browser does
   invisibly on first page load, to get an anonymous SUB/SUBP cookie -- was
   tried and confirmed NOT sufficient by itself (diagnose_weibo.py showed
   the handshake completes and returns real SUB/SUBP cookies, but
   getIndex still returns ok=-100 even with them attached: those cookies
   are scoped to .weibo.com, and m.weibo.cn's API apparently validates
   against a separately-synced .weibo.cn-domain session that the
   weibo.com-only handshake doesn't establish). What DID work in testing:
   a real browser tab on the same machine/network loaded the same account
   successfully with no login wall at all -- so the browser's cookie jar
   (built up from ordinary past browsing, not anything this script can
   reconstruct from scratch) is what makes the difference, not the IP.

   Rather than have this script try to extract or store a real browser
   session cookie -- that's a credential-like value tied to one person's
   browsing session, not something that belongs in code or in a public
   repo -- the fix here is a small manual opt-in: if a file named
   weibo_cookie.txt exists next to this script (see _LOCAL_COOKIE_FILENAME
   below), its contents are sent as the Cookie header. That file is
   listed in .gitignore and must never be committed. See README "Weibo
   login wall" for how to create it. The genvisitor/incarnate handshake is
   kept as an automatic fallback (_get_visitor_cookie below) since it's
   free to try and might work for other accounts/networks even though it
   didn't help here.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

UID = "7360795486"  # @草莓牛奶特别甜
CONTAINERID = f"107603{UID}"  # standard "this user's posts" container id pattern

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

INDEX_URL = "https://m.weibo.cn/api/container/getIndex"
EXTEND_URL = "https://m.weibo.cn/statuses/extend"
GENVISITOR_URL = "https://passport.weibo.com/visitor/genvisitor"
INCARNATE_URL = "https://passport.weibo.com/visitor/visitor"

REQUEST_TIMEOUT_SECONDS = 15
# Be polite: this account is polled on a 30-60 minute cadence per the design
# doc, so there is no need for tight retry loops or high request volume.
MIN_SECONDS_BETWEEN_REQUESTS = 2.0


@dataclass
class WeiboPost:
    mid: str  # stable post id, used to derive event UIDs downstream
    created_at_raw: str  # e.g. "Mon Jun 29 09:00:00 +0800 2026"
    text: str  # HTML-stripped, fully-expanded post text
    is_long_text: bool
    pic_count: int
    raw: dict  # original card dict, kept for debugging / re-parsing


class WeiboFetchError(RuntimeError):
    pass


# Optional manual override: a Cookie header value copied from a real,
# already-logged-in-or-not browser tab that successfully loads
# https://m.weibo.cn/u/<UID>. See module docstring note 3 and README "Weibo
# login wall". Never committed -- see .gitignore.
_LOCAL_COOKIE_FILENAME = "weibo_cookie.txt"

# Cached anonymous "visitor" cookie (see note 3 above). Acquired lazily, at
# most once per process, the first time the API demands it.
_VISITOR_COOKIE: Optional[str] = None

_LOCAL_COOKIE_LOADED = False
_LOCAL_COOKIE: Optional[str] = None


def _local_cookie() -> Optional[str]:
    """Read _LOCAL_COOKIE_FILENAME next to this script, once per process."""
    global _LOCAL_COOKIE_LOADED, _LOCAL_COOKIE
    if not _LOCAL_COOKIE_LOADED:
        _LOCAL_COOKIE_LOADED = True
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _LOCAL_COOKIE_FILENAME)
        try:
            with open(path, "r", encoding="utf-8") as f:
                _LOCAL_COOKIE = f.read().strip() or None
        except OSError:
            _LOCAL_COOKIE = None
    return _LOCAL_COOKIE


def _request_headers(cookie: Optional[str]) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://m.weibo.cn/u/{UID}",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _get_visitor_cookie() -> str:
    """Perform Weibo's anonymous "visitor system" handshake (genvisitor ->
    incarnate) and cache the resulting cookie for the rest of this process.
    Raises WeiboFetchError with a specific, distinguishable message at
    whichever step fails, since this can't be tested from a network-
    restricted environment and may need remote debugging from real error
    text alone."""
    global _VISITOR_COOKIE

    common_headers = {"User-Agent": USER_AGENT, "Referer": "https://weibo.com/"}

    # Step 1: genvisitor -- ask for a "tid" identifying this anonymous client.
    gen_req = urllib.request.Request(
        GENVISITOR_URL,
        data=urllib.parse.urlencode({"cb": "gen_callback"}).encode("utf-8"),
        headers=common_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(gen_req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            gen_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise WeiboFetchError(f"visitor handshake step 1 (genvisitor) network error: {e}") from e

    m = re.search(r"gen_callback\((.*)\)\s*;?\s*$", gen_body.strip())
    if not m:
        raise WeiboFetchError(
            f"visitor handshake step 1 (genvisitor): unexpected response shape: {gen_body[:300]!r}"
        )
    try:
        gen_data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise WeiboFetchError(
            f"visitor handshake step 1 (genvisitor): non-JSON payload: {m.group(1)[:300]!r}"
        ) from e

    tid = (gen_data.get("data") or {}).get("tid")
    if not tid:
        raise WeiboFetchError(f"visitor handshake step 1 (genvisitor): no tid in response: {gen_data}")

    # Step 2: incarnate -- exchange the tid for real SUB/SUBP cookies.
    incarnate_params = {
        "a": "incarnate",
        "t": tid,
        "w": "2",
        "c": "095",
        "gc": "",
        "cb": "cross_domain",
        "from": "weibo",
        "_rand": str(random.random()),
    }
    incarnate_url = f"{INCARNATE_URL}?{urllib.parse.urlencode(incarnate_params)}"
    incarnate_req = urllib.request.Request(incarnate_url, headers=common_headers)
    try:
        with urllib.request.urlopen(incarnate_req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            cookie_headers = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.URLError as e:
        raise WeiboFetchError(f"visitor handshake step 2 (incarnate) network error: {e}") from e

    if not cookie_headers:
        raise WeiboFetchError("visitor handshake step 2 (incarnate): server returned no Set-Cookie header")

    _VISITOR_COOKIE = "; ".join(c.split(";", 1)[0] for c in cookie_headers)
    return _VISITOR_COOKIE


def _fetch_json_once(url: str, cookie: Optional[str]) -> dict:
    req = urllib.request.Request(url, headers=_request_headers(cookie))
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except urllib.error.URLError as e:
        raise WeiboFetchError(f"network error fetching {url}: {e}") from e

    if not body or not body.strip():
        raise WeiboFetchError(
            f"empty response body from {url} -- likely blocked/rate-limited "
            f"by the server rather than a genuine malformed response"
        )
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise WeiboFetchError(f"non-JSON response from {url}: {e}") from e


def _get_json(url: str) -> dict:
    """Fetch url as JSON, working around Weibo's anonymous-visitor login
    wall (ok == -100 with a passport.weibo.com/sso/signin url -- see note 3
    in the module docstring) by trying, in order: (1) the local
    weibo_cookie.txt override if present, (2) a cached visitor-handshake
    cookie from earlier this run, (3) no cookie at all, (4) a fresh
    visitor-handshake attempt. Raises with a clear, actionable message if
    every option is exhausted."""
    data: dict = {}
    tried_visitor_handshake = False

    for cookie in (_local_cookie(), _VISITOR_COOKIE, None):
        data = _fetch_json_once(url, cookie)
        if data.get("ok") != -100:
            return data

    try:
        fresh_cookie = _get_visitor_cookie()
        tried_visitor_handshake = True
    except WeiboFetchError:
        pass
    else:
        data = _fetch_json_once(url, fresh_cookie)
        if data.get("ok") != -100:
            return data

    raise WeiboFetchError(
        f"unexpected response (ok={data.get('ok')}): {data} -- this looks like "
        f"Weibo's anonymous-visitor login wall. Tried: local {_LOCAL_COOKIE_FILENAME} "
        f"({'present' if _local_cookie() else 'absent'}), a cached/fresh anonymous "
        f"visitor-handshake cookie ({'attempted' if tried_visitor_handshake else 'failed before use'}), "
        f"and no cookie. Fix: refresh weibo_cookie.txt with a Cookie value copied from "
        f"a real browser tab that successfully loads https://m.weibo.cn/u/{UID} -- "
        f"see README 'Weibo login wall'."
    )


def _strip_html(text: str) -> str:
    """Weibo post text comes with <br/> and <a> tags embedded. Strip down to
    plain text good enough for the schedule parser. Deliberately simple
    (no external HTML parser dependency) since the input is a small,
    predictable subset of HTML, not arbitrary markup."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


def fetch_full_text(mid: str) -> Optional[str]:
    """Fetch the un-truncated text for a post whose feed snippet was cut off
    with '...展开' / '...全文'. Returns None if unavailable."""
    url = f"{EXTEND_URL}?id={mid}"
    try:
        data = _get_json(url)
    except WeiboFetchError:
        return None
    long_text = (data.get("data") or {}).get("longTextContent")
    if not long_text:
        return None
    return _strip_html(long_text)


def fetch_recent_posts(max_pages: int = 1, page_delay_seconds: float = MIN_SECONDS_BETWEEN_REQUESTS) -> List[WeiboPost]:
    """Fetch the most recent posts from the account's timeline.

    max_pages=1 is enough for the steady-state 30-60 min poll (you only need
    to see posts since the last run). Use a higher value only for backfill /
    initial-load scenarios.

    Note: this account pins its schedule-summary post (typically titled
    "X月X日中国队赛程") to the top of its timeline, and getIndex's first page
    already includes the pinned card first -- so max_pages=1 naturally
    catches it without needing to scroll/paginate further. Confirmed by the
    user, who has watched this account's posting pattern directly.

    Pagination mechanism (confirmed by direct API inspection, see
    tools/dump_posts.py debug output): this container -- a user's own post
    timeline, containerid "107603"+uid -- does NOT return a usable
    cardlistInfo.since_id. A real response's cardlistInfo looks like
    {'containerid': ..., 'v_p': 42, 'show_style': 1, 'total': 55015,
    'autoLoadMoreIndex': 10, 'page': 2} -- there is no since_id key at all.
    The original since_id-based loop here was therefore a silent no-op:
    since_id was always None/falsy, so the loop broke after page 1 every
    time regardless of max_pages, capping every fetch at whatever a single
    getIndex call returns (~11-12 posts). since_id pagination is a
    different Weibo container type (e.g. the combined "microblog" home
    feed); this "this user's posts" container instead pages via a plain
    "&page=N" query parameter (1-indexed; omitted/implicit for page 1).
    Deduplicate by mid while paging since a page can legitimately repeat a
    card already seen (e.g. the pinned post resurfacing) -- treat an
    all-duplicates page as the end of available history.
    """
    posts: List[WeiboPost] = []
    seen_mids: set = set()

    for page_num in range(1, max_pages + 1):
        url = f"{INDEX_URL}?type=uid&value={UID}&containerid={CONTAINERID}"
        if page_num > 1:
            url += f"&page={page_num}"

        data = _get_json(url)
        if data.get("ok") != 1:
            raise WeiboFetchError(f"unexpected response (ok={data.get('ok')}): {data}")

        cards = (data.get("data") or {}).get("cards") or []
        if not cards:
            break  # no more pages of history available

        added_this_page = 0
        for card in cards:
            if card.get("card_type") != 9:  # 9 == a normal post card
                continue
            mblog = card.get("mblog") or {}
            mid = str(mblog.get("mid") or mblog.get("id") or "")
            if not mid or mid in seen_mids:
                continue
            seen_mids.add(mid)
            added_this_page += 1

            text = _strip_html(mblog.get("text", ""))
            is_long = bool(mblog.get("isLongText"))

            if is_long or text.rstrip().endswith(("...展开", "...全文")):
                full = fetch_full_text(mid)
                if full:
                    text = full
                # else: fall back to the truncated snippet rather than
                # dropping the post; the parser will just see less text
                # and should naturally produce lower-confidence/partial
                # results rather than silently losing the post entirely.

            posts.append(
                WeiboPost(
                    mid=mid,
                    created_at_raw=mblog.get("created_at", ""),
                    text=text,
                    is_long_text=is_long,
                    pic_count=int(mblog.get("pic_num", 0) or 0),
                    raw=card,
                )
            )

        if added_this_page == 0:
            # Whole page was already-seen posts -- further pages are very
            # unlikely to add anything new, so stop instead of burning more
            # requests.
            break

        if page_num < max_pages:
            time.sleep(page_delay_seconds)

    return posts


if __name__ == "__main__":
    # Manual smoke test entry point. Run this from an environment with
    # normal internet access to verify the live API still matches the shape
    # assumed above before deploying.
    fetched = fetch_recent_posts(max_pages=1)
    print(f"Fetched {len(fetched)} posts")
    for p in fetched[:5]:
        print("---")
        print(p.mid, p.created_at_raw)
        print(p.text[:200])
