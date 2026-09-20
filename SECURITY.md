# Security policy

## What must never be committed to this repository

This project connects to a real brokerage account. The following must never
appear in a commit, a branch, an issue, a pull request, a test fixture, a
documentation example, or a log file checked into version control:

- **Credentials of any kind** — API keys, OAuth tokens, refresh tokens, access
  tokens, session cookies, MCP server credentials, passwords, or `Authorization`
  headers.
- **Broker account identifiers** — full account numbers, and any mask tied to a
  real account. The masks in this repository (`••••0001`, `••••0002`,
  `••••6789`) are invented.
- **Mailbox, brokerage or account exports** of any kind.
- **Real portfolio data** — holdings, balances, buying power, cost basis,
  unrealized or realized P&L, tax lots, or order history.
- **Real broker snapshots and responses** — `broker.json`, `orders.json`,
  `resp.json`, `state/broker_snapshot.json`, `state/promotion_snapshot.json`.
- **Real approval records** (`state/approvals.json`), **execution ledgers**
  (`state/executions.json`), submission tickets, `ref_id` values, and
  `state/pending_submission.json`.
- **Real decision and audit logs** — `logs/decisions.jsonl`,
  `logs/execution_audit.jsonl`, and scheduler run logs, which embed local
  filesystem paths.
- **Reports and research notes derived from a real portfolio** — everything
  under `reports/` and `research/` that a real run produced.
- **Personal identifiers** — names, email addresses, home or machine paths,
  hostnames, or user IDs.

`.gitignore` is configured to keep these out, but **`.gitignore` is a
convenience, not a control.** Verify before you commit.

## Why the shipped `reports/` and `research/` files are safe

This repository ships exactly one digest, one audit record and four research
notes. **All of them are synthetic**, written to exercise the reporting and
research-note contracts in the test suite. Every account, balance, holding,
price, date and identifier in them is invented. They describe no real portfolio.

The same is true of everything under `examples/`.

## Configuration

`config.json` and `.claude/settings.json` are **gitignored**. The repository
ships `config.example.json` and `.claude/settings.example.json`; copy them to
create your local instances. Your live configuration is yours and stays local.

This project reads no `.env` file and stores no credentials itself. Broker
access is provided by a separately configured MCP server whose credentials live
outside this repository. If you add a `.env`, it is already gitignored — keep it
that way.

## Before you commit

```bash
git diff --cached | grep -iE "api[_-]?key|secret|token|password|bearer|cookie"
git diff --cached --stat
```

Check that nothing from `state/`, `logs/`, `reports/` or `research/` is staged
unless you are certain it is synthetic.

## If a secret is committed

Treat it as disclosed the moment it exists in a commit, even if the commit is
never pushed and even if a later commit removes it — git history retains it.

1. **Revoke and rotate the credential first.** Removing it from history does not
   un-disclose it.
2. Then rewrite history (`git filter-repo`, or a fresh repository with no shared
   history) and force-push.
3. If it was pushed to a fork or a public mirror, assume it is permanently
   compromised.

## Reporting a vulnerability

Open a private security advisory through GitHub's "Report a vulnerability"
workflow rather than a public issue. Please do not include real account data,
credentials or portfolio information in the report.

## Scope note

This is experimental research software with no warranty (see `LICENSE`). It is
not a security-audited trading system. Anyone running it against a real
brokerage account does so at their own risk and is responsible for their own
investment decisions.
