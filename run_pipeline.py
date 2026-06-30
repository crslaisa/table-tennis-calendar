"""
run_pipeline.py

The single entrypoint a scheduled job (cron / GitHub Actions) should
call. Wires the four building-block modules together end to end:

  weibo_scraper.fetch_recent_posts()
      -> schedule_parser.parse_post() per post
      -> state_store.apply_events() per player, diffed against on-disk state
      -> ics_generator.write_feed() per player

Designed to be safe to run on a tight, unattended schedule (every
30-60 minutes per the design doc): every step is idempotent, and a
post that's already fully reflected in state produces no changes.

Daily update threshold: if the total number of new or changed events
across all players in a single UTC day reaches DAILY_UPDATE_THRESHOLD,
the pipeline writes feeds and state as normal but exits with code 99
and sends an alert email instead of exiting 0. The GitHub Actions
workflow interprets exit code 99 as "skip the git commit step", so
calendar subscribers won't see the high-update-rate changes until the
counter resets at UTC midnight (or a human investigates and re-triggers
manually). This guards against runaway anomalies (e.g. a scraper bug
re-creating dozens of events) without blocking normal operation.

The threshold is intentionally high (default: 5). In a typical day
Wang Chuqin and Sun Yingsha together play at most 4 matches across
singles + mixed doubles, so 5 genuine new/changed events per day is
already unusual and warrants a human look.

Usage:
    python3 run_pipeline.py [--feeds-dir feeds] [--max-pages 1]

Exit codes:
    0   -- normal (zero or more events updated, below daily threshold)
    1   -- unexpected error (scraper failure, etc.)
    99  -- daily update threshold reached; commit skipped, alert sent
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import traceback
import urllib.error
import urllib.request

from schedule_parser import parse_post, TARGET_PLAYERS
from state_store import apply_events
from ics_generator import write_feed
from weibo_scraper import fetch_recent_posts, WeiboFetchError

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DAILY_STATS_PATH = os.path.join(DATA_DIR, "daily_stats.json")

GITHUB_REPO = "clarissally/table-tennis-calendar"
DAILY_UPDATE_THRESHOLD = 5  # new + changed events across all players per UTC day


def _today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def load_daily_stats() -> dict:
    today = _today_utc()
    if os.path.exists(DAILY_STATS_PATH):
        try:
            with open(DAILY_STATS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except Exception:
            pass  # corrupt file -- start fresh
    return {"date": today, "updates_today": 0}


def save_daily_stats(stats: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DAILY_STATS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DAILY_STATS_PATH)


def create_alert_issue(updates_today: int) -> None:
    """Open a GitHub Issue so the repo owner gets a notification email from
    GitHub. Uses the automatically-injected GITHUB_TOKEN secret (no extra
    credentials needed) and Python's standard-library urllib -- no pip
    install required."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "WARNING: GITHUB_TOKEN env var not set -- skipping alert issue.",
            file=sys.stderr,
        )
        return
    title = f"[乒乓赛程] 今日更新 {updates_today} 次，已自动暂停提交"
    body = (
        f"日历管道今日赛程更新已达 **{updates_today}** 次"
        f"（阈值：{DAILY_UPDATE_THRESHOLD}），已自动暂停提交更新。\n\n"
        f"请前往 [Actions 日志](https://github.com/{GITHUB_REPO}/actions) 查看详情。\n\n"
        "如确认数据无误，日计数会在 UTC 0 点后自动重置，下次定时运行时恢复正常提交。"
        "也可以手动触发一次工作流（Run workflow）提前恢复。"
    )
    payload = json.dumps({"title": title, "body": body}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            issue = json.loads(resp.read())
        print(f"Alert issue created: {issue['html_url']}")
    except urllib.error.HTTPError as e:
        print(
            f"WARNING: failed to create alert issue: {e} -- {e.read().decode()}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"WARNING: failed to create alert issue: {e}", file=sys.stderr)


def run(feeds_dir: str, max_pages: int) -> int:
    try:
        posts = fetch_recent_posts(max_pages=max_pages)
    except WeiboFetchError as e:
        print(f"ERROR: failed to fetch posts: {e}", file=sys.stderr)
        return 1

    print(f"Fetched {len(posts)} post(s).")

    events_by_player = {tag: [] for tag in TARGET_PLAYERS.values()}

    for post in posts:
        result = parse_post(post.text, source_post_id=post.mid)
        if result.notes:
            for note in result.notes:
                print(f"  [{post.mid}] NOTE: {note}")
        for event in result.events:
            for tag in event.player_tags:
                events_by_player[tag].append(event)

    this_run_updates = 0
    for tag, events in events_by_player.items():
        update = apply_events(tag, events)
        print(
            f"[{tag}] new={len(update.new_uids)} changed={len(update.changed_uids)} "
            f"unchanged={len(update.unchanged_uids)} held_for_review={len(update.held_for_review)}"
        )
        this_run_updates += len(update.new_uids) + len(update.changed_uids)
        path = write_feed(tag, update.publishable, output_dir=feeds_dir)
        print(f"[{tag}] wrote {path} ({len(update.publishable)} event(s))")

    if this_run_updates > 0:
        stats = load_daily_stats()
        stats["updates_today"] += this_run_updates
        save_daily_stats(stats)
        print(
            f"Daily update count: {stats['updates_today']} / {DAILY_UPDATE_THRESHOLD}"
        )
        if stats["updates_today"] >= DAILY_UPDATE_THRESHOLD:
            print(
                f"ALERT: daily update threshold ({DAILY_UPDATE_THRESHOLD}) reached -- "
                "feeds written but committing paused; sending alert email."
            )
            create_alert_issue(stats["updates_today"])
            return 99  # workflow sees non-zero → skips git commit step

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feeds-dir", default=os.path.join(PROJECT_ROOT, "feeds"))
    parser.add_argument("--max-pages", type=int, default=1)
    args = parser.parse_args()

    try:
        sys.exit(run(args.feeds_dir, args.max_pages))
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
