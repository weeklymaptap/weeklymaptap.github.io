#!/usr/bin/env python3
"""
MapTap weekly leaderboard builder.

Reads the local iMessage database (~/Library/Messages/chat.db), finds MapTap
score posts in a group chat, aggregates the current week (Mon-Sun) and writes
data.json next to this script.

Standard library only.

Usage:
    python3 maptap.py --list-chats        # find your group chat, then set CHAT_ID
    python3 maptap.py                     # build data.json for the current week
    python3 maptap.py --week 2026-07-13   # rebuild a specific week
    python3 maptap.py --show              # print what was parsed, write nothing
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, date, timedelta

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
#
# The chat ID and the phone-number-to-name map live in config.json, which is
# gitignored, so no personal data ends up in this file or on GitHub. Copy
# config.example.json to config.json and fill it in. Populated by load_config().

CHAT_ID = None
NORMALIZED_NAMES = {}

# Friendly labels for the emoji sub-scores. Unknown emoji still work,
# they just show up without a label.
EMOJI_LABELS = {
    "\U0001F601": "Streak",     # grinning
    "\U0001F3AF": "Accuracy",   # dart
    "\U0001F3C6": "Trophy",     # trophy
    "\U0001F3C5": "Medal",      # medal
    "\U0001F62D": "Misses",     # sob
}

# ---------------------------------------------------------------------------
# Nothing below here needs editing.
# ---------------------------------------------------------------------------

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# Apple's epoch is 2001-01-01 UTC.
APPLE_EPOCH_OFFSET = 978307200

MONTHS = {}
for _i, _m in enumerate(
    [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ],
    start=1,
):
    MONTHS[_m] = _i
    MONTHS[_m[:3]] = _i

RE_HEADER = re.compile(r"maptap\.gg\s+([A-Za-z]{3,9})\.?\s+(\d{1,2})", re.I)
RE_FINAL = re.compile(r"final\s*score\s*[:\-]?\s*([\d,]+)", re.I)
RE_SUB = re.compile(r"(\d{1,4})\s*([^\s\d]+)")


# ---------------------------------------------------------------------------
# attributedBody decoding
# ---------------------------------------------------------------------------

def decode_attributed_body(blob):
    """Pull plain text out of a message's attributedBody blob.

    Modern macOS leaves message.text NULL and stores the body as an NSAttributed
    String in Apple's binary "typedstream" format. There is no typedstream reader
    in the stdlib, so this walks to the NSString payload by hand: after the class
    name comes a marker byte, then a length, then UTF-8 bytes.

    Lengths are encoded as: a single byte under 128, or 0x81 followed by a
    uint16, or 0x82 followed by a uint32 (little endian).
    """
    if not blob:
        return None
    if isinstance(blob, str):
        return blob
    if isinstance(blob, memoryview):
        blob = blob.tobytes()

    idx = blob.find(b"NSString")
    if idx == -1:
        return _scavenge_text(blob)

    pos = idx + len(b"NSString")
    # The length is preceded by a '+' (0x2B) marker a few bytes along. Scan for
    # it rather than hard-coding an offset, since the preamble varies by OS.
    plus = blob.find(b"+", pos, pos + 16)
    pos = plus + 1 if plus != -1 else pos + 5

    if pos >= len(blob):
        return _scavenge_text(blob)

    first = blob[pos]
    if first == 0x81 and pos + 3 <= len(blob):
        length = int.from_bytes(blob[pos + 1:pos + 3], "little")
        pos += 3
    elif first == 0x82 and pos + 5 <= len(blob):
        length = int.from_bytes(blob[pos + 1:pos + 5], "little")
        pos += 5
    elif first < 0x80:
        length = first
        pos += 1
    else:
        return _scavenge_text(blob)

    if length <= 0 or pos + length > len(blob):
        return _scavenge_text(blob)

    text = blob[pos:pos + length].decode("utf-8", errors="replace").strip()
    return text or _scavenge_text(blob)


def _scavenge_text(blob):
    """Last resort: return the longest decodable run that looks like a post.

    Only used when the typedstream layout is unfamiliar. We look for the chunk
    containing "Final score", which is all this tool cares about anyway.
    """
    try:
        loose = blob.decode("utf-8", errors="ignore")
    except Exception:
        return None
    hit = RE_FINAL.search(loose)
    if not hit:
        return None
    start = max(0, hit.start() - 200)
    return loose[start:hit.end()].strip()


def message_text(text_col, body_col):
    if text_col and text_col.strip():
        return text_col
    return decode_attributed_body(body_col)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def apple_to_datetime(raw):
    """Convert a message.date value to a local datetime.

    Ventura-era rows store nanoseconds since the Apple epoch; older rows store
    seconds. Anything above ~1e11 must be nanoseconds.
    """
    if raw is None:
        return None
    seconds = raw / 1e9 if raw > 1e11 else float(raw)
    return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET)


def datetime_to_apple_seconds(dt):
    return int(dt.timestamp()) - APPLE_EPOCH_OFFSET


def week_bounds(day):
    """Monday..Sunday containing `day`."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def resolve_post_date(header_month, header_day, sent_at):
    """Work out which puzzle day a post refers to.

    Prefer the date printed in the post, inferring the year from when it was
    sent (so a January 1 post sent on December 31 lands correctly). Fall back to
    the send date if the header is missing or implausible.
    """
    sent_day = sent_at.date()
    if not header_month or not header_day:
        return sent_day

    month = MONTHS.get(header_month.lower())
    if not month:
        return sent_day

    best = None
    for year in (sent_at.year - 1, sent_at.year, sent_at.year + 1):
        try:
            candidate = date(year, month, header_day)
        except ValueError:
            continue
        gap = abs((candidate - sent_day).days)
        if best is None or gap < best[0]:
            best = (gap, candidate)

    # A post should be about today or the last few days. If the header says
    # something wildly different, trust the timestamp instead.
    if best is None or best[0] > 14:
        return sent_day
    return best[1]


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

def normalize_handle(handle):
    if not handle:
        return ""
    handle = handle.strip().lower()
    if "@" in handle:
        return handle
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 10 else digits


def load_config(path=None):
    """Read chat_id and the handle-to-name map out of config.json.

    Kept out of this file (and out of git) so phone numbers never reach GitHub.
    """
    global CHAT_ID, NORMALIZED_NAMES

    path = path or CONFIG_PATH
    if not os.path.exists(path):
        die(
            "No config file at %s\n\n"
            "Copy the template and fill in your details:\n"
            "    cp config.example.json config.json\n\n"
            "Run `%s --list-chats` to find your chat_id."
            % (path, os.path.basename(sys.argv[0]))
        )

    try:
        with open(path) as handle:
            raw = json.load(handle)
    except ValueError as exc:
        die("%s is not valid JSON (%s)" % (path, exc))

    CHAT_ID = raw.get("chat_id")
    if not CHAT_ID:
        die('%s is missing "chat_id". Run --list-chats to find it.' % path)

    names = raw.get("names") or {}
    if not names:
        die('%s has no "names" entries, so every post would be unattributed.' % path)

    NORMALIZED_NAMES = {}
    for key, name in names.items():
        NORMALIZED_NAMES["me" if key == "me" else normalize_handle(key)] = name

    if "me" not in NORMALIZED_NAMES:
        warn('config.json has no "me" entry - your own posts will show as "Me".')


def sender_name(is_from_me, handle):
    if is_from_me:
        return NORMALIZED_NAMES.get("me", "Me")
    return NORMALIZED_NAMES.get(normalize_handle(handle))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_post(text, sent_at):
    """Return a parsed post dict, or None if this message isn't a MapTap score."""
    if not text:
        return None

    final = RE_FINAL.search(text)
    if not final:
        return None

    try:
        score = int(final.group(1).replace(",", ""))
    except ValueError:
        return None

    header = RE_HEADER.search(text)
    if not header and "maptap" not in text.lower():
        return None

    month = header.group(1) if header else None
    day = int(header.group(2)) if header else None
    post_day = resolve_post_date(month, day, sent_at)

    # Sub-scores live on their own line: "83<emoji> 98<emoji> ...". Skip the
    # "Final score: 670" line so its number never counts as a sub-score.
    subs = []
    for line in text.splitlines():
        if RE_FINAL.search(line) or RE_HEADER.search(line):
            continue
        found = RE_SUB.findall(line)
        if len(found) >= 2:
            for value, emoji in found:
                emoji = emoji.strip()
                subs.append({
                    "emoji": emoji,
                    "value": int(value),
                    "label": EMOJI_LABELS.get(emoji, ""),
                })
            break

    return {"date": post_day, "score": score, "subs": subs}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect():
    if not os.path.exists(DB_PATH):
        die("No message database at %s" % DB_PATH)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        # connect() is lazy, and a Full Disk Access denial shows up as
        # "unable to open database file" here or as "authorization denied" on
        # the first read, depending on the Python build. Probe now so either
        # way the user gets the explanation instead of a stack trace.
        con.execute("SELECT COUNT(*) FROM chat").fetchone()
        return con
    except sqlite3.Error as exc:
        die(
            "Could not read %s (%s).\n\n"
            "This is almost always macOS privacy protection rather than a missing\n"
            "or corrupt file. Whatever runs this script needs Full Disk Access -\n"
            "see the \"Full Disk Access\" section of README.md." % (DB_PATH, exc)
        )


def list_chats(con):
    rows = con.execute(
        """
        SELECT c.ROWID,
               c.chat_identifier,
               COALESCE(c.display_name, ''),
               c.style,
               COUNT(m.ROWID),
               MAX(m.date)
        FROM chat c
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        LEFT JOIN message m ON m.ROWID = cmj.message_id
        GROUP BY c.ROWID
        HAVING COUNT(m.ROWID) > 0
        ORDER BY MAX(m.date) DESC
        """
    ).fetchall()

    print("\n%-6s %-13s %-9s %-38s %s" % ("ROWID", "LAST MSG", "MESSAGES", "CHAT ID", "NAME / PARTICIPANTS"))
    print("-" * 116)

    for rowid, identifier, display, style, count, last in rows[:40]:
        people = [
            r[0] for r in con.execute(
                """
                SELECT h.id FROM chat_handle_join chj
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE chj.chat_id = ?
                """,
                (rowid,),
            ).fetchall()
        ]
        when = apple_to_datetime(last)
        label = display or ("group of %d" % len(people) if style == 43 else "1:1")
        if people:
            label += "  [%s]" % ", ".join(people[:5])
            if len(people) > 5:
                label += " +%d" % (len(people) - 5)
        print("%-6s %-13s %-9s %-38s %s" % (
            rowid,
            when.strftime("%Y-%m-%d") if when else "?",
            count,
            (identifier or "")[:38],
            label[:60],
        ))

    print(
        "\nGroup chats are style 43 and usually the ones with several participants."
        "\nCopy a CHAT ID (or the ROWID) into CHAT_ID at the top of maptap.py.\n"
    )


def fetch_messages(con, since_day):
    """All messages in the configured chat sent on/after `since_day`."""
    lower_bound = datetime_to_apple_seconds(
        datetime.combine(since_day, datetime.min.time())
    )

    rows = con.execute(
        """
        SELECT m.date, m.is_from_me, h.id, m.text, m.attributedBody
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE (c.chat_identifier = :chat
               OR c.display_name = :chat
               OR CAST(c.ROWID AS TEXT) = :chat)
          AND m.date >= :since
        ORDER BY m.date ASC
        """,
        # Seconds-era rows are numerically far below nanosecond-era rows, so a
        # seconds lower bound is safe for both encodings (it just lets a few
        # extra modern rows through, which the date filter below catches).
        {"chat": str(CHAT_ID), "since": lower_bound},
    ).fetchall()

    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def build(con, target_day, verbose=False):
    start, end = week_bounds(target_day)

    rows = fetch_messages(con, start - timedelta(days=2))
    if not rows:
        warn(
            "No messages found for CHAT_ID=%r. Run --list-chats to confirm the ID."
            % CHAT_ID
        )

    # name -> date -> post. First post of a day wins, so an edit or a repost
    # can't inflate a total.
    by_player = {}
    unknown_senders = {}
    parsed_count = 0

    for raw_date, is_from_me, handle, text_col, body_col in rows:
        sent_at = apple_to_datetime(raw_date)
        if sent_at is None:
            continue

        text = message_text(text_col, body_col)
        post = parse_post(text, sent_at)
        if not post:
            continue

        if not (start <= post["date"] <= end):
            continue

        name = sender_name(is_from_me, handle)
        if not name:
            key = handle or "(unknown)"
            unknown_senders[key] = unknown_senders.get(key, 0) + 1
            continue

        parsed_count += 1
        days = by_player.setdefault(name, {})
        if post["date"] not in days:
            days[post["date"]] = post
            if verbose:
                print("  %-8s %s  %4d  %s" % (
                    name,
                    post["date"].isoformat(),
                    post["score"],
                    " ".join("%d%s" % (s["value"], s["emoji"]) for s in post["subs"]),
                ))

    for handle, count in sorted(unknown_senders.items(), key=lambda kv: -kv[1]):
        warn("%d MapTap post(s) from unmapped sender %s - add it to NAME_MAP."
             % (count, handle))

    players = []
    for name, days in by_player.items():
        posts = sorted(days.values(), key=lambda p: p["date"])
        scores = [p["score"] for p in posts]
        players.append({
            "name": name,
            "total": sum(scores),
            "days_played": len(scores),
            "best": max(scores),
            "average": round(sum(scores) / len(scores)),
            "days": [
                {
                    "date": p["date"].isoformat(),
                    "score": p["score"],
                    "subs": p["subs"],
                }
                for p in posts
            ],
        })

    # Total first; then reward consistency, then a strong single day.
    players.sort(key=lambda p: (-p["total"], -p["days_played"], -p["best"], p["name"]))

    rank = 0
    previous = None
    for i, player in enumerate(players):
        signature = (player["total"], player["days_played"], player["best"])
        if signature != previous:
            rank = i + 1
            previous = signature
        player["rank"] = rank

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "week": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": format_week(start, end),
        },
        "days": [(start + timedelta(days=i)).isoformat() for i in range(7)],
        "players": players,
        "post_count": parsed_count,
    }


def format_week(start, end):
    if start.month == end.month:
        return "%s %d–%d, %d" % (
            start.strftime("%B"), start.day, end.day, end.year
        )
    if start.year == end.year:
        return "%s %d – %s %d, %d" % (
            start.strftime("%b"), start.day, end.strftime("%b"), end.day, end.year
        )
    return "%s %d, %d – %s %d, %d" % (
        start.strftime("%b"), start.day, start.year,
        end.strftime("%b"), end.day, end.year,
    )


# ---------------------------------------------------------------------------

def warn(message):
    sys.stderr.write("warning: %s\n" % message)


def die(message):
    sys.stderr.write("error: %s\n" % message)
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="Build the MapTap weekly leaderboard.")
    parser.add_argument("--list-chats", action="store_true",
                        help="list chats so you can find CHAT_ID, then exit")
    parser.add_argument("--week", metavar="YYYY-MM-DD",
                        help="build the week containing this date (default: today)")
    parser.add_argument("--out", default=os.path.join(HERE, "data.json"),
                        help="output path (default: data.json beside this script)")
    parser.add_argument("--show", action="store_true",
                        help="print parsed posts and the resulting JSON, write nothing")
    parser.add_argument("--config", default=None,
                        help="path to config.json (default: beside this script)")
    args = parser.parse_args()

    con = connect()

    # --list-chats deliberately runs before the config is loaded, so a fresh
    # checkout can find its chat_id before there is a config.json to read.
    if args.list_chats:
        list_chats(con)
        return

    load_config(args.config)

    if args.week:
        try:
            target = datetime.strptime(args.week, "%Y-%m-%d").date()
        except ValueError:
            die("--week expects YYYY-MM-DD, got %r" % args.week)
    else:
        target = date.today()

    if args.show:
        print("Parsed posts:")
    payload = build(con, target, verbose=args.show)

    if args.show:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    with open(args.out, "w") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("%s: %d players, %d posts, week of %s" % (
        os.path.basename(args.out),
        len(payload["players"]),
        payload["post_count"],
        payload["week"]["label"],
    ))


if __name__ == "__main__":
    main()
