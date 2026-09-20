#!/usr/bin/env python3
"""Ingest broker history into state/portfolio_history.json — local only.

    # first time: full history
    python3 scripts/ingest_history.py --staging /tmp/raw.json --full

    # later: incremental, validated against loss and rewriting
    python3 scripts/ingest_history.py --staging /tmp/raw.json

    # look at what is stored, without changing it
    python3 scripts/ingest_history.py --summary

## How the data gets here

The agent gathers raw read-only MCP responses and writes them to a staging
file; this script normalizes and validates them. That is the same split as
``src/execution.py``'s ``BrokerSnapshot``: the model does the I/O, Python does
the arithmetic, and ``src/`` imports no network module. Nothing here calls a
broker tool, and no write tool exists for it to call.

The staging file is a JSON object::

    {
      "accounts": [{"account_number": "...", "role": "individual",
                    "agentic": false}],
      "equity_orders": [{"account_number": "...", "orders": [ ...raw... ]}],
      "crypto_orders": [{"account_number": "...", "orders": [ ...raw... ]}],
      "tax_lots":      [{"account_number": "...", "symbol": "MU",
                         "lots": [ ...raw... ]}],
      "agent_crypto_order_ids": ["..."],
      "anomalies": ["..."]
    }

## Privacy

``state/portfolio_history.json`` holds personal financial history. It is
gitignored, account identifiers are stored masked (``••••0001``), and this
script **refuses to write** a document in which any unmasked account-shaped
identifier survives.

## Safety on incremental update

A later ingest may add records. It may not drop one, and it may not rewrite a
historical fact. Both are refused, the prior file is kept, and a ``.bak`` copy
is written before any replacement. The stored digest is re-checked on load so a
file corrupted outside this script fails closed instead of being merged into.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(REPO_ROOT, "state", "portfolio_history.json")

from src.history import (  # noqa: E402
    ASSET_CRYPTO,
    ASSET_EQUITY,
    SCHEMA_VERSION,
    coverage_summary,
    mask_account,
    merge_records,
    normalize_crypto_order,
    normalize_equity_order,
    normalize_tax_lot,
    unmasked_account_numbers,
    validate_history,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def body_digest(document: dict) -> str:
    """Digest of everything except the digest field itself."""
    payload = {k: v for k, v in document.items() if k != "digest"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def empty_history() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "last_ingest_at": None,
        "ingest_count": 0,
        "accounts": [],
        "orders": [],
        "lots": [],
        "coverage": {},
        "anomalies": [],
        "notes": [],
        "digest": "",
    }


def load_history(path: str, problems: list) -> dict:
    if not os.path.exists(path):
        return empty_history()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        problems.append(
            "could not read %s: %s. Refusing to overwrite a file that may hold "
            "history — move it aside deliberately if it is genuinely corrupt."
            % (path, exc)
        )
        return {}

    if not isinstance(document, dict):
        problems.append("%s is not a JSON object; refusing to merge into it" % path)
        return {}

    stored = document.get("digest") or ""
    if stored:
        actual = body_digest(document)
        if stored != actual:
            problems.append(
                "%s fails its own integrity digest (stored %s..., computed "
                "%s...). It was modified outside this script. Refusing to merge "
                "into a file whose contents cannot be trusted."
                % (path, stored[:12], actual[:12])
            )
            return {}
    return document


def normalize_staging(staging: dict, problems: list) -> dict:
    """Raw MCP payloads -> normalized records."""
    orders = []
    lots = []
    agent_crypto_ids = staging.get("agent_crypto_order_ids") or []

    for block in staging.get("equity_orders") or []:
        account = block.get("account_number")
        for raw in block.get("orders") or []:
            orders.append(normalize_equity_order(raw, account).to_dict())

    for block in staging.get("crypto_orders") or []:
        account = block.get("account_number")
        for raw in block.get("orders") or []:
            orders.append(
                normalize_crypto_order(raw, account, agent_crypto_ids).to_dict()
            )

    for block in staging.get("tax_lots") or []:
        account = block.get("account_number")
        symbol = block.get("symbol") or ""
        if not symbol:
            problems.append("a tax_lots block has no symbol; skipped")
            continue
        for raw in block.get("lots") or []:
            lots.append(normalize_tax_lot(raw, account, symbol).to_dict())

    accounts = []
    for entry in staging.get("accounts") or []:
        accounts.append({
            "masked": mask_account(entry.get("account_number")),
            "role": entry.get("role") or "unknown",
            "agentic": bool(entry.get("agentic")),
        })

    return {"orders": orders, "lots": lots, "accounts": accounts}


def merge_accounts(existing: list, incoming: list) -> list:
    by_mask = {a.get("masked"): a for a in existing if isinstance(a, dict)}
    for account in incoming:
        by_mask.setdefault(account.get("masked"), account)
    return sorted(by_mask.values(), key=lambda a: str(a.get("masked")))


def render_summary(document: dict) -> str:
    coverage = document.get("coverage") or coverage_summary(document)
    lines = [
        "=" * 68,
        "PORTFOLIO HISTORY — %s" % HISTORY_PATH.replace(REPO_ROOT + "/", ""),
        "=" * 68,
        "  schema        %s" % document.get("schema_version"),
        "  ingests       %s (last %s)" % (
            document.get("ingest_count"), document.get("last_ingest_at") or "never"),
        "  accounts      %s" % ", ".join(
            "%s (%s%s)" % (a.get("masked"), a.get("role"),
                           ", agentic" if a.get("agentic") else "")
            for a in document.get("accounts") or []) or "none",
        "  records       %d orders, %d lots" % (
            len(document.get("orders") or []), len(document.get("lots") or [])),
    ]
    for section, data in sorted(coverage.items()):
        if not isinstance(data, dict):
            continue
        bits = ", ".join("%s=%s" % (k, v) for k, v in sorted(data.items())
                         if not isinstance(v, dict))
        lines.append("  %-13s %s" % (section, bits))
        if isinstance(data.get("provenance"), dict) and data["provenance"]:
            lines.append("  %-13s provenance %s" % (
                "", ", ".join("%s=%d" % kv for kv in sorted(data["provenance"].items()))))
    for anomaly in document.get("anomalies") or []:
        lines.append("  ! %s" % anomaly)
    lines.append("=" * 68)
    lines.append("Local only, gitignored, account identifiers masked.")
    lines.append("Advisory input to an evaluation — never authorization.")
    lines.append("=" * 68)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", help="raw MCP payloads gathered by the agent")
    parser.add_argument("--history", default=HISTORY_PATH)
    parser.add_argument("--full", action="store_true",
                        help="first full ingest; refuses if a history already exists")
    parser.add_argument("--summary", action="store_true", help="print and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and report, write nothing")
    args = parser.parse_args(argv)

    # An unattended run must not write personal financial history. The PreToolUse
    # write guard already denies state/portfolio_history.json, but this script
    # writes through Python rather than a tool call, so it refuses on its own
    # account too. --summary is read-only and stays available.
    if os.environ.get("RH_AGENT_SCHEDULED_RUN") == "1" and not args.summary:
        print(
            "refusing: RH_AGENT_SCHEDULED_RUN=1 means this is an unattended "
            "scheduled run. state/portfolio_history.json is written only by a "
            "human-invoked ingest.",
            file=sys.stderr)
        return 4

    problems: list = []

    if args.summary:
        document = load_history(args.history, problems)
        for problem in problems:
            print("problem: %s" % problem, file=sys.stderr)
        if not document:
            return 1
        print(render_summary(document))
        return 0

    if not args.staging:
        print("--staging is required unless --summary is given", file=sys.stderr)
        return 2

    try:
        with open(args.staging, "r", encoding="utf-8") as handle:
            staging = json.load(handle)
    except (OSError, ValueError) as exc:
        print("could not read staging file: %s" % exc, file=sys.stderr)
        return 2
    if not isinstance(staging, dict):
        print("staging file must be a JSON object", file=sys.stderr)
        return 2

    existing = load_history(args.history, problems)
    if problems:
        for problem in problems:
            print("problem: %s" % problem, file=sys.stderr)
        return 1

    had_records = bool(existing.get("orders") or existing.get("lots"))
    if args.full and had_records:
        print(
            "refusing --full: %s already holds %d orders and %d lots. Drop the "
            "flag to ingest incrementally, which validates against loss."
            % (args.history, len(existing.get("orders") or []),
               len(existing.get("lots") or [])),
            file=sys.stderr,
        )
        return 1

    incoming = normalize_staging(staging, problems)

    merged_orders, order_problems = merge_records(
        existing.get("orders") or [], incoming["orders"])
    merged_lots, lot_problems = merge_records(
        existing.get("lots") or [], incoming["lots"])
    problems.extend(order_problems)
    problems.extend(lot_problems)

    document = dict(existing) or empty_history()
    document.setdefault("created_at", utc_now())
    document["schema_version"] = SCHEMA_VERSION
    document["orders"] = merged_orders
    document["lots"] = merged_lots
    document["accounts"] = merge_accounts(
        existing.get("accounts") or [], incoming["accounts"])
    document["last_ingest_at"] = utc_now()
    document["ingest_count"] = int(existing.get("ingest_count") or 0) + 1
    document["coverage"] = coverage_summary(document)

    anomalies = list(existing.get("anomalies") or [])
    for anomaly in staging.get("anomalies") or []:
        if anomaly not in anomalies:
            anomalies.append(anomaly)
    document["anomalies"] = anomalies
    document["notes"] = staging.get("notes") or existing.get("notes") or []

    problems.extend(validate_history(document))

    # --- no record may be lost by this ingest ---
    if len(merged_orders) < len(existing.get("orders") or []):
        problems.append("order count went down; refusing to write")
    if len(merged_lots) < len(existing.get("lots") or []):
        problems.append("lot count went down; refusing to write")

    document["digest"] = ""
    document["digest"] = body_digest(document)
    # ensure_ascii=False so a masked identifier is stored as the readable
    # "••••0001" rather than an escaped \u2022 sequence. The integrity digest is
    # computed over its own canonical encoding, so this does not affect it.
    serialized = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)

    # --- privacy: no unmasked account identifier may survive ---
    # Scanned WITHOUT the digest field: a SHA-256 hex string can contain nine
    # consecutive decimal digits by chance, which would trip the account-number
    # guard at random. The digest is a hash of the body, not an identifier.
    scan_target = json.dumps(
        {k: v for k, v in document.items() if k != "digest"}, sort_keys=True)
    leaked = unmasked_account_numbers(scan_target)
    if leaked:
        problems.append(
            "refusing to write: %d unmasked account-shaped identifier(s) would "
            "be stored (%s). Account numbers are never written to disk."
            % (len(leaked), ", ".join("..." + v[-4:] for v in leaked[:5]))
        )

    if problems:
        for problem in problems:
            print("problem: %s" % problem, file=sys.stderr)
        print("nothing was written; %s is unchanged" % args.history, file=sys.stderr)
        return 1

    print("orders: %d existing + %d incoming -> %d stored" % (
        len(existing.get("orders") or []), len(incoming["orders"]), len(merged_orders)))
    print("lots:   %d existing + %d incoming -> %d stored" % (
        len(existing.get("lots") or []), len(incoming["lots"]), len(merged_lots)))

    if args.dry_run:
        print("--dry-run: validated, nothing written")
        print(render_summary(document))
        return 0

    try:
        os.makedirs(os.path.dirname(args.history), exist_ok=True)
        if os.path.exists(args.history):
            with open(args.history, "rb") as src:
                previous = src.read()
            with open(args.history + ".bak", "wb") as dst:
                dst.write(previous)
        temporary = args.history + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        os.replace(temporary, args.history)
    except OSError as exc:
        print("could not write history: %s" % exc, file=sys.stderr)
        return 1

    print("wrote %s" % args.history)
    print(render_summary(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
