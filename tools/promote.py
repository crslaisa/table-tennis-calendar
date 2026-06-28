"""
tools/promote.py

The manual-review side of the confidence-gated pipeline (design doc
Section 3): apply_events() in state_store.py automatically publishes
only "high"-confidence events and parks everything else in
data/review_queue.json. This script is the human's other half of that
workflow -- after eyeballing a queued item against the real source post,
either approve it onto the live feeds or drop it as wrong/not-our-players.

Usage:
    python3 tools/promote.py --list
    python3 tools/promote.py --approve <uid>
    python3 tools/promote.py --reject <uid>

--approve marks the event "high" confidence in the relevant player
state file(s) and removes it from the review queue. It does NOT
regenerate the .ics feeds itself -- run run_pipeline.py (or just the
ics_generator step) afterwards, or simply wait for the next scheduled
run, since it reads state fresh each time.

--reject just removes the item from the queue without publishing it
(e.g. it turned out to be a typo'd name, not a real match).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_store import load_review_queue, save_review_queue, load_state, save_state, StoredEvent


def cmd_list():
    queue = load_review_queue()
    if not queue:
        print("Review queue is empty.")
        return
    for item in queue:
        ev = item.event
        print(f"uid: {item.uid}")
        print(f"  reason: {item.reason}")
        print(f"  queued_at: {item.queued_at}")
        print(f"  {ev.get('date')} {ev.get('time_local')} {ev.get('table')} "
              f"{ev.get('player1')} vs {ev.get('player2')} tags={ev.get('player_tags')}")
        print(f"  raw_line: {ev.get('raw_line')!r}")
        print()


def cmd_approve(uid: str):
    queue = load_review_queue()
    match = next((item for item in queue if item.uid == uid), None)
    if match is None:
        print(f"No queued item with uid {uid!r}. Use --list to see what's queued.", file=sys.stderr)
        sys.exit(1)

    ev = match.event
    tags = ev.get("player_tags", [])
    if not tags:
        print(f"Queued item {uid!r} has no player_tags -- can't tell which feed(s) to publish to.", file=sys.stderr)
        sys.exit(1)

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for tag in tags:
        state = load_state(tag)
        existing = state.get(uid)
        sequence = existing.sequence + 1 if existing else 0
        state[uid] = StoredEvent(
            uid=uid,
            tournament_name=ev.get("tournament_name"),
            date=ev.get("date"),
            time_local=ev.get("time_local"),
            timezone_assumed=ev.get("timezone_assumed", "Asia/Shanghai"),
            table=ev.get("table"),
            player1=ev.get("player1"),
            player2=ev.get("player2"),
            player_tags=tags,
            confidence="high",
            sequence=sequence,
            last_modified=now_iso,
            source_post_id=ev.get("source_post_id"),
            raw_line=ev.get("raw_line", ""),
        )
        save_state(tag, state)
        print(f"[{tag}] approved and marked high-confidence (will publish on next feed regen): {uid}")

    remaining = [item for item in queue if item.uid != uid]
    save_review_queue(remaining)
    print("Removed from review queue. Run run_pipeline.py (or restart the scheduled job) to regenerate feeds.")


def cmd_reject(uid: str):
    queue = load_review_queue()
    remaining = [item for item in queue if item.uid != uid]
    if len(remaining) == len(queue):
        print(f"No queued item with uid {uid!r}.", file=sys.stderr)
        sys.exit(1)
    save_review_queue(remaining)
    print(f"Removed {uid} from review queue without publishing.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--approve", metavar="UID")
    group.add_argument("--reject", metavar="UID")
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.approve:
        cmd_approve(args.approve)
    elif args.reject:
        cmd_reject(args.reject)


if __name__ == "__main__":
    main()
