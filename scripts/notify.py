#!/usr/bin/env python3
"""Deliver one notification to macOS Notification Center, with suppression.

    python3 scripts/notify.py --kind ACTIONABLE_BUY --title "..." --body "..."
    python3 scripts/notify.py --from-json note.json
    python3 scripts/notify.py --from-json note.json --dry-run

Delivery is `osascript -e 'display notification ...'`, which is why this lives
in ``scripts/`` and not ``src/``: ``src`` may not import ``subprocess``, and
that rule is worth more than the convenience of putting it there.

Suppression is decided by :mod:`src.notifications` against
``state/notifications.json`` before anything is displayed, so a condition that
has not materially changed stays quiet until its reminder interval elapses.

Exit codes: 0 delivered or deliberately suppressed, 1 delivery failed,
2 bad input. A suppressed notification is a success — silence is the feature.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.notifications import (  # noqa: E402
    NOTIFICATION_KINDS,
    NOTIFICATION_STATE_PATH,
    Notification,
    NotificationError,
    load_state,
    record_sent,
    save_state,
    should_send,
)


def _escape(text: str) -> str:
    """Quote a string for embedding in an AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def deliver(notification: Notification) -> bool:
    """Show the notification. Returns False if macOS refused it.

    Notification Center shows a single line, so the body is flattened and the
    most important line — the one naming the action — is kept first.
    """
    body = " · ".join(line.strip() for line in notification.body.splitlines()
                      if line.strip())
    script = 'display notification "%s" with title "%s"' % (
        _escape(body[:240]), _escape(notification.title[:120]))
    if notification.urgent:
        script += ' sound name "Ping"'
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        print("could not deliver notification: %s" % exc, file=sys.stderr)
        return False
    if completed.returncode != 0:
        print("osascript failed: %s" % completed.stderr.decode().strip(),
              file=sys.stderr)
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-json", help="a JSON file holding one notification")
    parser.add_argument("--kind", choices=NOTIFICATION_KINDS)
    parser.add_argument("--title")
    parser.add_argument("--body", default="")
    parser.add_argument("--urgent", action="store_true")
    parser.add_argument("--state", default=NOTIFICATION_STATE_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and report, display nothing, record nothing")
    args = parser.parse_args(argv)

    if args.from_json:
        try:
            with open(args.from_json, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            print("could not read %s: %s" % (args.from_json, exc), file=sys.stderr)
            return 2
        if not isinstance(payload, dict) or payload.get("kind") not in NOTIFICATION_KINDS:
            print("notification file must carry a known 'kind'", file=sys.stderr)
            return 2
        notification = Notification(
            kind=payload["kind"],
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            fingerprint=str(payload.get("fingerprint") or ""),
            urgent=bool(payload.get("urgent")),
            details=payload.get("details") or {},
        )
    elif args.kind and args.title:
        notification = Notification(
            kind=args.kind, title=args.title, body=args.body or args.title,
            fingerprint="", urgent=args.urgent)
    else:
        print("give either --from-json or both --kind and --title", file=sys.stderr)
        return 2

    if not notification.fingerprint:
        from src.notifications import _fingerprint

        notification.fingerprint = _fingerprint(
            notification.kind, {"title": notification.title, "body": notification.body})

    try:
        sent = load_state(args.state)
    except NotificationError as exc:
        # A corrupt ledger must never silence a real alert: notify anyway and
        # start a fresh ledger. This is the one place in the repository that
        # deliberately fails *open*, because the cost of a duplicate is noise
        # and the cost of silence is a missed purchase or an unreconciled order.
        print("notification ledger unusable (%s); notifying anyway" % exc,
              file=sys.stderr)
        sent = {}

    if not should_send(notification, sent):
        print("suppressed: %s unchanged since the last notification" % notification.kind)
        return 0

    if args.dry_run:
        print("--dry-run: would notify [%s] %s" % (notification.kind, notification.title))
        return 0

    if not deliver(notification):
        return 1

    try:
        save_state(record_sent(notification, sent), args.state)
    except OSError as exc:
        print("notified, but could not record it: %s" % exc, file=sys.stderr)
    print("notified: [%s] %s" % (notification.kind, notification.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
