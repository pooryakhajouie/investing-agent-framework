#!/usr/bin/env python3
"""Refresh data/crypto_universe.json from a get_currency_pairs response.

This script does not talk to Robinhood. The agent calls the READ-ONLY MCP tool
``get_currency_pairs`` (with a high ``limit`` so the full catalog comes back),
saves the raw JSON, and pipes it here. That keeps the network call in the
agent's audited tool layer and the parsing in reviewable code.

    python3 scripts/update_crypto_universe.py raw_pairs.json
    cat raw_pairs.json | python3 scripts/update_crypto_universe.py -

Accepts either the full MCP envelope ({"data": {"results": [...]}}), a bare
{"results": [...]}, or a plain list of pair objects. Writes nothing that could
identify an account.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.state import (  # noqa: E402
    DEFAULT_CRYPTO_UNIVERSE_PATH,
    CryptoUniverseError,
    atomic_write_json,
    iso_now,
    load_crypto_universe,
)

# Fields copied from each pair. Anything not on this list is dropped, which is
# how account-scoped data is kept out of the snapshot by construction.
_KEEP = ("name", "tradability", "halted", "min_order_size")


def extract_pairs(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return payload["results"]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
    raise SystemExit(
        "could not find a list of currency pairs in the input; expected "
        '{"data": {"results": [...]}}, {"results": [...]}, or a bare list'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the crypto pair snapshot")
    parser.add_argument("source", help="path to the raw get_currency_pairs JSON, or '-' for stdin")
    parser.add_argument("--out", default=DEFAULT_CRYPTO_UNIVERSE_PATH)
    args = parser.parse_args()

    text = sys.stdin.read() if args.source == "-" else open(args.source, encoding="utf-8").read()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        print("input is not valid JSON: %s" % exc, file=sys.stderr)
        return 2

    raw_pairs = extract_pairs(payload)
    if not raw_pairs:
        print("input contained zero currency pairs; refusing to write an empty snapshot",
              file=sys.stderr)
        return 2

    pairs = {}
    for entry in raw_pairs:
        if not isinstance(entry, dict) or not entry.get("symbol"):
            continue
        symbol = str(entry["symbol"]).upper()
        by_type = entry.get("tradability_by_account_type") or {}
        pairs[symbol] = {
            "name": entry.get("name"),
            "code": (entry.get("asset_currency") or {}).get("code"),
            "tradability": entry.get("tradability"),
            "individual_tradability": by_type.get("individual"),
            "halted": bool(entry.get("halted")),
            "halted_regions": list(entry.get("halted_regions") or []),
            "display_only": bool(entry.get("display_only")),
            "min_order_size": entry.get("min_order_size"),
        }

    snapshot = {
        "schema_version": 1,
        "source": "robinhood-trading MCP get_currency_pairs",
        "fetched_at": iso_now(),
        "note": "Snapshot of Robinhood-supported crypto pairs. Contains no account or "
                "credential data. Refresh with scripts/update_crypto_universe.py.",
        "pairs": dict(sorted(pairs.items())),
    }
    atomic_write_json(args.out, snapshot)

    try:
        universe = load_crypto_universe(args.out)
    except CryptoUniverseError as exc:
        print("wrote %s but it does not load cleanly: %s" % (args.out, exc), file=sys.stderr)
        return 3

    print("Wrote %s" % args.out)
    print("  pairs listed      : %d" % len(universe.pairs))
    print("  purchasable now   : %d" % len(universe.purchasable_symbols))
    print("  fetched_at        : %s" % universe.fetched_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
