#!/usr/bin/env python3
"""Resolve the absolute executables a LaunchAgent needs, and render its plist.

`launchd` reads no `.zshrc`, sources no `nvm.sh`, and inherits nothing from an
open Terminal. A scheduled job that runs ``claude`` by name therefore works
when a human tests it and fails at 10:30 the next morning. This script removes
that failure mode by resolving absolute paths at *install* time, verifying them
by actually running the executable under launchd-like conditions, and writing
them into the plist.

    # what would be persisted, as JSON
    python3 scripts/resolve_launchd_runtime.py --json

    # one field, for a shell to capture
    python3 scripts/resolve_launchd_runtime.py --print claude_bin

    # render a plist template with the resolved values
    python3 scripts/resolve_launchd_runtime.py \
        --render scheduler/com.robinhood-agent.evaluation.plist \
        --output ~/Library/LaunchAgents/com.robinhood-agent.evaluation.plist

    # what a rendered plist declares, for testing it under a minimal env
    python3 scripts/resolve_launchd_runtime.py --env-args <plist>
    python3 scripts/resolve_launchd_runtime.py --program-args <plist>

Exit status is 0 only when resolution succeeded. **A non-zero exit must stop an
installation**: a job that cannot be made independent of shell initialisation
should not be scheduled at all.

This script runs ``claude --version`` and nothing else. It places no orders, it
approves nothing, and it changes no repository state — the only file it ever
writes is the plist named by ``--output``.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.launchd import (  # noqa: E402
    LAUNCHD_BASE_PATH,
    ProbeResult,
    RuntimeResolution,
    plist_values,
    render_plist,
    resolve_runtime,
    unresolved_placeholders,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Long enough for a cold start of a large binary, short enough that a hung
# installer is obvious rather than mysterious.
PROBE_TIMEOUT_SECONDS = 60


def make_probe(home: str, verbose: bool = False):
    """A probe that runs ``<claude> --version`` in a launchd-like environment.

    ``env -i``-equivalent: the child gets only HOME, PATH and a TERM, so a
    success here means the executable does not depend on anything the calling
    shell happened to provide.
    """

    def probe(executable: str, path: str) -> ProbeResult:
        env = {"HOME": home, "PATH": path, "TERM": "dumb"}
        try:
            completed = subprocess.run(
                [executable, "--version"],
                env=env,
                cwd=home,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            return ProbeResult(False, "could not execute %s: %s" % (executable, exc))
        except subprocess.TimeoutExpired:
            return ProbeResult(
                False,
                "%s did not respond to --version within %ds"
                % (executable, PROBE_TIMEOUT_SECONDS),
            )
        output = (completed.stdout or b"").decode("utf-8", "replace").strip()
        if verbose and output:
            print("  probe: %s" % output.splitlines()[0], file=sys.stderr)
        if completed.returncode != 0:
            return ProbeResult(
                False,
                "exited %d under PATH=%s: %s"
                % (completed.returncode, path, output.splitlines()[0] if output else "(no output)"),
            )
        return ProbeResult(True, output.splitlines()[0] if output else "")

    return probe


def resolve(verbose: bool = False) -> RuntimeResolution:
    """Resolve the runtime, honouring CLAUDE_BIN / NODE_BIN overrides."""
    home = os.path.expanduser("~")
    extra_claude = []
    extra_node = []

    # An explicit override is a directory hint, not a bypass: the probe still
    # has to succeed before the value is persisted.
    override_claude = os.environ.get("CLAUDE_BIN", "").strip()
    if override_claude:
        extra_claude.append(os.path.dirname(os.path.abspath(override_claude)))
    override_node = os.environ.get("NODE_BIN", "").strip()
    if override_node:
        extra_node.append(os.path.dirname(os.path.abspath(override_node)))

    return resolve_runtime(
        home=home,
        probe=make_probe(home, verbose=verbose),
        path_env=os.environ.get("PATH", ""),
        extra_claude_dirs=extra_claude,
        extra_node_dirs=extra_node,
        base_path=LAUNCHD_BASE_PATH,
    )


def report(resolution: RuntimeResolution) -> str:
    lines = [
        "=" * 68,
        "LAUNCHD RUNTIME RESOLUTION — %s" % ("RESOLVED" if resolution.ok else "FAILED"),
        "=" * 68,
        "  claude_bin     %s" % (resolution.claude_bin or "(unresolved)"),
    ]
    if resolution.claude_target:
        lines.append("    -> target    %s" % resolution.claude_target)
    lines.append("  node_bin       %s" % (resolution.node_bin or "(none)"))
    lines.append("  node_required  %s" % ("yes" if resolution.node_required else "no"))
    lines.append("  PATH           %s" % resolution.path)
    for check in resolution.checks:
        lines.append("  . %s" % check)
    for note in resolution.notes:
        lines.append("  ! %s" % note)
    for problem in resolution.problems:
        lines.append("  x %s" % problem)
    lines.append("=" * 68)
    return "\n".join(lines)


def load_plist(path: str) -> dict:
    with open(os.path.expanduser(path), "rb") as handle:
        return plistlib.loads(handle.read())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the resolution as JSON")
    parser.add_argument("--print", dest="field", help="print one resolved field")
    parser.add_argument("--render", help="a plist template to render")
    parser.add_argument("--output", help="where to write the rendered plist")
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument(
        "--env-args",
        metavar="PLIST",
        help="print KEY=VALUE lines for a rendered plist's EnvironmentVariables",
    )
    parser.add_argument(
        "--program-args",
        metavar="PLIST",
        help="print a rendered plist's ProgramArguments, one per line",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # --- reading back a rendered plist (no resolution needed) ------------
    if args.env_args:
        try:
            data = load_plist(args.env_args)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            print("could not read plist: %s" % exc, file=sys.stderr)
            return 2
        for key, value in sorted((data.get("EnvironmentVariables") or {}).items()):
            if value != "":
                print("%s=%s" % (key, value))
        return 0

    if args.program_args:
        try:
            data = load_plist(args.program_args)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            print("could not read plist: %s" % exc, file=sys.stderr)
            return 2
        for value in data.get("ProgramArguments") or []:
            print(value)
        return 0

    # --- resolution ------------------------------------------------------
    resolution = resolve(verbose=not args.quiet and not args.json and not args.field)

    if args.json:
        print(json.dumps(resolution.to_dict(), indent=2, sort_keys=True))
    elif args.field:
        value = resolution.to_dict().get(args.field)
        if value is None:
            value = ""
        print(value if not isinstance(value, list) else "\n".join(str(v) for v in value))
    elif not args.quiet:
        print(report(resolution), file=sys.stderr)

    if not resolution.ok:
        return 1

    if args.render:
        if not args.output:
            print("--render requires --output", file=sys.stderr)
            return 2
        try:
            with open(args.render, "r", encoding="utf-8") as handle:
                template = handle.read()
        except OSError as exc:
            print("could not read template: %s" % exc, file=sys.stderr)
            return 2

        values = plist_values(
            os.path.abspath(args.repo_root), os.path.expanduser("~"), resolution
        )
        rendered = render_plist(template, values)

        leftover = unresolved_placeholders(rendered)
        if leftover:
            print(
                "refusing to write %s: unresolved placeholders %s. The template and "
                "this script have drifted apart." % (args.output, ", ".join(leftover)),
                file=sys.stderr,
            )
            return 2

        output = os.path.expanduser(args.output)
        try:
            os.makedirs(os.path.dirname(output), exist_ok=True)
            with open(output, "w", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as exc:
            print("could not write %s: %s" % (output, exc), file=sys.stderr)
            return 2

        # Prove the result is a valid plist that still declares what we meant.
        try:
            data = load_plist(output)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            print("rendered plist is not readable: %s" % exc, file=sys.stderr)
            return 2
        env = data.get("EnvironmentVariables") or {}
        if env.get("CLAUDE_BIN") != resolution.claude_bin:
            print("rendered plist does not carry the resolved CLAUDE_BIN", file=sys.stderr)
            return 2
        if not args.quiet:
            print("rendered %s" % output, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
