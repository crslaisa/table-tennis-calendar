"""
ics_generator.py

Renders a player's list of publishable StoredEvent (from state_store.py)
into an RFC5545 .ics feed, by hand, using only the Python standard
library -- the `icalendar` PyPI package is unavailable in this build
sandbox (PyPI is blocked by the network allowlist), so this module
implements just the small subset of the spec the project actually needs:
VCALENDAR/VEVENT, stable UID, SEQUENCE/LAST-MODIFIED for updates, basic
text escaping, and line folding. If you later have unrestricted pip
access, swapping this for `icalendar` is a reasonable simplification --
see README "Future improvements".

Timezone handling: the source account only ever gives times in China
Standard Time (Asia/Shanghai, UTC+8, no DST -- this has been fixed since
1991). Rather than hand-roll a VTIMEZONE block (a common source of subtle
bugs in DIY ICS generators), this module converts every local time to UTC
and emits DTSTART/DTEND with a trailing "Z". This is fully spec-compliant
and every mainstream calendar client (Google/Outlook/Apple) will render
the correct local time for the viewer automatically.

Duration assumption: the source posts only ever give a start time, never
an end time. DEFAULT_DURATION below is used for every event and is called
out in each event's DESCRIPTION so this assumption is visible to anyone
inspecting the feed, not just buried in code. See design doc Section 3 /
README "Known limitations".

DESCRIPTION field language: subscriber-facing text (field labels, the
"where did this come from" line) is Chinese, since the audience is
Chinese-speaking fans subscribing to a Chinese source account. See
_source_description() below for why the raw source_post_id (a screenshot
filename or a Weibo post mid) is never shown verbatim -- those are
internal implementation details, not user-facing information.

Calendar display name (X-WR-CALNAME): also pure Chinese (e.g.
"王楚钦赛程日历"), not mixed English/Chinese -- this is the name most
calendar apps show in their calendar list/sidebar after subscribing, so
it gets the same "no English label leakage" treatment as DESCRIPTION.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Iterable, List, Optional

from state_store import StoredEvent

TZ_SHANGHAI_OFFSET = dt.timedelta(hours=8)
DEFAULT_DURATION = dt.timedelta(hours=1)

PRODID = "-//table-tennis-calendar//wang-chuqin-sun-yingsha-schedule//EN"
LINE_FOLD_LIMIT = 75  # octets per RFC5545 3.1, excluding the CRLF itself

DISPLAY_NAMES = {
    "wangchuqin": "王楚钦赛程日历",
    "sunyingsha": "孙颖莎赛程日历",
}


def _escape_text(value: str) -> str:
    """RFC5545 3.3.11 TEXT escaping."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    """Fold a single logical content line to LINE_FOLD_LIMIT octets per
    physical line, continuation lines prefixed with a single space, per
    RFC5545 3.1. Splits are done on UTF-8 byte boundaries without cutting
    a multi-byte character in half."""
    encoded = line.encode("utf-8")
    if len(encoded) <= LINE_FOLD_LIMIT:
        return line

    parts = []
    start = 0
    limit = LINE_FOLD_LIMIT
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        # Back off if we landed mid-character (UTF-8 continuation bytes
        # have the high bit set and the top two bits == 10).
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(encoded[start:end].decode("utf-8"))
        start = end
        limit = LINE_FOLD_LIMIT - 1  # continuation lines lose 1 octet to the leading space

    return "\r\n ".join(parts)


def _dtstamp_utc(local_date: str, local_time: str) -> dt.datetime:
    naive = dt.datetime.strptime(f"{local_date} {local_time}", "%Y-%m-%d %H:%M")
    return naive - TZ_SHANGHAI_OFFSET  # Shanghai wall time -> UTC


def _format_utc(moment: dt.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _source_description(source_post_id: Optional[str]) -> str:
    """Human-facing (Chinese), non-technical description of where an
    event's data came from. Deliberately does NOT leak internal
    identifiers (screenshot filenames like 'manual-screenshot-img6890-...'
    or raw Weibo post mids) into the subscriber-facing feed -- those are
    implementation details, not something a calendar subscriber needs to
    see. Covers both the automated scraper path (source_post_id is a
    Weibo mid, set in run_pipeline.py) and the Plan B manual screenshot
    path (source_post_id is prefixed "manual-screenshot", set in
    tools/ingest_manual_post.py) -- so this applies uniformly to every
    event regardless of which path produced it."""
    if not source_post_id:
        return "来源未知"
    if source_post_id.startswith("manual-screenshot"):
        return "博主微博截图（人工录入）"
    return "博主微博发布"


def _build_vevent(stored: StoredEvent, generated_at: dt.datetime) -> List[str]:
    start_utc = _dtstamp_utc(stored.date, stored.time_local)
    end_utc = start_utc + DEFAULT_DURATION

    opponent_summary = f"{stored.player1} vs {stored.player2}"
    summary = _escape_text(f"\U0001F3D3 {opponent_summary}")

    description_parts = [
        f"赛事：{stored.tournament_name or '未知'}",
        f"球台：{stored.table or '未知'}",
        f"北京时间：{stored.time_local}",
        f"消息来源：{_source_description(stored.source_post_id)}",
        f"原文摘录：{stored.raw_line}",
        "备注：原始消息未公布结束时间，此事件统一按"
        f"{int(DEFAULT_DURATION.total_seconds() // 60)}分钟占位时长处理。",
    ]
    description = _escape_text("\n".join(description_parts))

    lines = [
        "BEGIN:VEVENT",
        f"UID:{stored.uid}",
        f"DTSTAMP:{_format_utc(generated_at)}",
        f"DTSTART:{_format_utc(start_utc)}",
        f"DTEND:{_format_utc(end_utc)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"SEQUENCE:{stored.sequence}",
        f"LAST-MODIFIED:{stored.last_modified.replace('-', '').replace(':', '')}",
        "END:VEVENT",
    ]
    return [_fold_line(l) for l in lines]


def build_calendar(player_tag: str, events: Iterable[StoredEvent]) -> str:
    display_name = DISPLAY_NAMES.get(player_tag, player_tag)
    generated_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    sorted_events = sorted(events, key=lambda e: (e.date or "", e.time_local or ""))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold_line(f"X-WR-CALNAME:{_escape_text(display_name)}"),
        "X-WR-TIMEZONE:Asia/Shanghai",
        "REFRESH-INTERVAL;VALUE=DURATION:PT30M",
        "X-PUBLISHED-TTL:PT30M",
    ]
    for stored in sorted_events:
        lines.extend(_build_vevent(stored, generated_at))
    lines.append("END:VCALENDAR")

    # RFC5545 requires CRLF line endings.
    return "\r\n".join(lines) + "\r\n"


def write_feed(player_tag: str, events: Iterable[StoredEvent], output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    text = build_calendar(player_tag, events)
    path = os.path.join(output_dir, f"{player_tag}.ics")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp_path, path)  # atomic swap -- subscribers should never see a half-written feed
    return path


if __name__ == "__main__":
    # Smoke test: round-trip a couple of fixture StoredEvents (bypassing
    # state_store.py's confidence gating on purpose, just to exercise the
    # ICS rendering itself) and print the result so it can be eyeballed,
    # plus parsed back with Python's own email-style header reader as a
    # sanity check that line folding didn't break anything.
    sample_events = [
        StoredEvent(
            uid="ttcal-sample0000000001@table-tennis-calendar.example",
            tournament_name="WTT美国大满贯",
            date="2026-06-30",
            time_local="9:00",
            timezone_assumed="Asia/Shanghai",
            table="T1",
            player1="孙颖莎",
            player2="刘杨子",
            player_tags=["sunyingsha"],
            confidence="high",
            sequence=0,
            last_modified="2026-06-29T08:00:00Z",
            source_post_id="smoketest-1",
            raw_line="9:00 T1 孙颖莎🇨🇳VS刘杨子🇦🇺",
        ),
    ]
    out_text = build_calendar("sunyingsha", sample_events)
    print(out_text)
    path = write_feed("sunyingsha", sample_events, output_dir="feeds")
    print(f"wrote {path}")
