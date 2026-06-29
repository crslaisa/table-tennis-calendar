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
   is Weibo's anonymous "visitor system" login wall -- m.weibo.cn's
   container API requires a short-lived anonymous "visitor" cookie (SUB/
   SUBP, obtained via the same genvisitor/incarnate handshake a real
   browser does invisibly on first page load) before it will serve guest
   API responses, and apparently enforces this more aggressively for some
   IPs than others. Fix: _get_visitor_cookie() below performs that
   handshake once per process and _get_json transparently retries with it
   if it sees ok == -100. This is still anonymous/no-login in the sense
   that no Weibo account or password is involved -- it's the same
   anonymous cookie any browser gets for free just by loading the page.
"""

from __future__ import annotations

import json
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


# Cached anonymous "visitor" cookie (see note 3 above). Acquired lazily, at
# most once per process, the first time the API demands it.
_VISITOR_COOKIE: Optional[str] = None


def _request_headers() -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://m.weibo.cn/u/{UID}",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
    }
    if _VISITOR_COOKIE:
        headers["Cookie"] = _VISITOR_COOKIE
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


def _get_json(url: str, *, _retried_after_login_wall: bool = False) -> dict:
    req = urllib.request.Request(url, headers=_request_headers())
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
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise WeiboFetchError(f"non-JSON response from {url}: {e}") from e

    # ok == -100 with a passport.weibo.com sso/signin url means the request
    # hit the anonymous-visitor login wall (see note 3 in the module
    # docstring). Do the visitor handshake once and retry this same request
    # with the resulting cookie before giving up.
    if data.get("ok") == -100 and not _retried_after_login_wall:
        _get_visitor_cookie()
        return _get_json(url, _retried_after_login_wall=True)

    return data


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
    """
    posts: List[WeiboPost] = []
    since_id: Optional[str] = None

    for page_num in range(max_pages):
        url = f"{INDEX_URL}?type=uid&value={UID}&containerid={CONTAINERID}"
        if since_id:
            url += f"&since_id={since_id}"

        data = _get_json(url)
        if data.get("ok") != 1:
            raise WeiboFetchError(f"unexpected response (ok={data.get('ok')}): {data}")

        cards = (data.get("data") or {}).get("cards") or []
        for card in cards:
            if card.get("card_type") != 9:  # 9 == a normal post card
                continue
            mblog = card.get("mblog") or {}
            text = _strip_html(mblog.get("text", ""))
            is_long = bool(mblog.get("isLongText"))

            if is_long or text.rstrip().endswith(("...展开", "...全文")):
                full = fetch_full_text(mblog.get("mid") or mblog.get("id"))
                if full:
                    text = full
                # else: fall back to the truncated snippet rather than
                # dropping the post; the parser will just see less text
                # and should naturally produce lower-confidence/partial
                # results rather than silently losing the post entirely.

            posts.append(
                WeiboPost(
                    mid=str(mblog.get("mid") or mblog.get("id") or ""),
                    created_at_raw=mblog.get("created_at", ""),
                    text=text,
                    is_long_text=is_long,
                    pic_count=int(mblog.get("pic_num", 0) or 0),
                    raw=card,
                )
            )

        since_id = ((data.get("data") or {}).get("cardlistInfo") or {}).get("since_id")
        if not since_id or page_num + 1 >= max_pages:
            break
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
