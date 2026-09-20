"""Resolving the executables a launchd job needs, before the job is installed.

## The problem this solves

`launchd` does not read `.zshrc`. It does not source `nvm.sh`. It does not
inherit an open Terminal's environment. A LaunchAgent starts with a minimal
`PATH` — roughly ``/usr/bin:/bin:/usr/sbin:/sbin`` — and nothing else.

So a scheduled job that runs ``claude`` by name works when a human tests it
from a shell that already has Claude Code and Node on `PATH`, and then fails at
10:30 the next morning with ``claude: not found``. That is exactly the failure
this module removes: **resolve the absolute paths at install time, persist them
into the plist, and never rely on shell initialisation at run time.**

## What "resolved" means here

1. An absolute path to the ``claude`` executable that exists and is executable.
2. Whether that executable needs Node — a native binary does not, a
   ``#!/usr/bin/env node`` script does — and if so, an absolute path to `node`.
3. A `PATH` string built only from absolute directories, which is written into
   the plist and is sufficient on its own.

Resolution is not taken on trust. The caller supplies a ``probe`` that actually
*runs* the candidate executable under the minimal environment being proposed. A
candidate that cannot start under that environment is not resolved, it is a
failure with a reason attached — installation must stop rather than schedule a
job that will fail unattended.

## Why the probe is injected

`src/` may not import `subprocess` or any network module; a test walks the AST
of every module in the package and fails on either. That rule is what makes
"this repository cannot reach the broker" structural rather than a promise, and
it is not worth weakening for a convenience here. So this module is pure: it
decides *what* to run and *what environment to run it in*, and
``scripts/resolve_launchd_runtime.py`` does the running.

Nothing here schedules, enables or installs anything.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# The environment a LaunchAgent actually starts with. Anything a scheduled job
# needs beyond this has to be named explicitly in the plist.
LAUNCHD_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Where Claude Code installs itself, in the order worth trying. Entries
# beginning with "~" are expanded against the resolved home directory.
CLAUDE_SEARCH_DIRS = (
    "~/.local/bin",
    "~/.claude/local",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/bin",
    "~/.npm-global/bin",
)

# Where a Node runtime tends to live, including the nvm layout that is only on
# PATH because a shell rc file put it there.
NODE_SEARCH_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.volta/bin",
    "~/.asdf/shims",
    "/usr/bin",
)
NVM_VERSIONS_DIR = "~/.nvm/versions/node"

# Directories worth appending to the job's PATH when they exist, so ordinary
# tooling (git, python3, homebrew utilities) is available to the run.
OPTIONAL_PATH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

# A placeholder in a plist template.
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")

# Interpreters that mean "this is a wrapper script, look inside it".
_WRAPPER_INTERPRETERS = ("sh", "bash", "zsh", "dash", "env")

# --------------------------------------------------------------------------
# macOS TCC-protected folders
# --------------------------------------------------------------------------
#
# A repository under ~/Desktop, ~/Documents or ~/Downloads cannot be executed by
# a LaunchAgent. macOS Transparency, Consent & Control grants access to these
# folders per *application*, and launchd does not inherit the grant your
# Terminal holds — so the job fails before its first line with:
#
#     shell-init: error retrieving current directory: getcwd: cannot access
#       parent directories: Operation not permitted
#     /bin/bash: .../scheduled_evaluation.sh: Operation not permitted
#
# and exit code 126. This is invisible from an interactive shell, where
# everything works, which is exactly why it has to be checked rather than
# discovered on the first scheduled morning. It cost this project one missed
# weekday evaluation and one missed weekly discovery run.
#
# The fix is to move the repository, not to grant Full Disk Access to /bin/bash:
# a blanket grant to a shell interpreter is a far larger permission than the job
# needs, and it silently applies to everything else that shell ever runs.
TCC_PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")


def tcc_protected_ancestor(repo_root: str, home: str) -> Optional[str]:
    """The protected folder containing ``repo_root``, or None if it is clear.

    Compares resolved absolute paths, so a symlink pointing into Desktop is
    caught too — the kernel enforces on the real path, not the link.
    """
    try:
        resolved = os.path.realpath(repo_root)
        resolved_home = os.path.realpath(_expand(home, home))
    except OSError:  # pragma: no cover - realpath on an unreadable path
        return None
    for name in TCC_PROTECTED_DIRS:
        protected = os.path.join(resolved_home, name)
        if resolved == protected or resolved.startswith(protected + os.sep):
            return protected
    return None


@dataclass
class ProbeResult:
    """The outcome of actually running a candidate executable."""

    ok: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"ok": self.ok, "detail": self.detail}


@dataclass
class RuntimeResolution:
    """Absolute paths a launchd job needs, and how they were established."""

    claude_bin: Optional[str] = None
    claude_target: Optional[str] = None
    node_bin: Optional[str] = None
    node_required: bool = False
    path: str = LAUNCHD_BASE_PATH
    checks: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.claude_bin)

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "claude_bin": self.claude_bin,
            "claude_target": self.claude_target,
            "node_bin": self.node_bin,
            "node_required": self.node_required,
            "path": self.path,
            "checks": list(self.checks),
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# Locating executables
# --------------------------------------------------------------------------


def _expand(path: str, home: str) -> str:
    if path.startswith("~"):
        return os.path.normpath(os.path.join(home, path[1:].lstrip("/")))
    return os.path.normpath(path)


def is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def dedupe(items: Sequence[str]) -> List[str]:
    """Order-preserving de-duplication; empty entries dropped."""
    seen = set()
    out = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def nvm_node_dirs(home: str, lister: Callable[[str], List[str]] = None) -> List[str]:
    """`bin` directories of every installed nvm Node, newest version first.

    nvm exists only as shell functions and a `PATH` entry written by `.zshrc`,
    so a LaunchAgent never sees it. Reading the directory layout directly is
    what makes the resulting path independent of shell initialisation.
    """
    root = _expand(NVM_VERSIONS_DIR, home)
    try:
        entries = lister(root) if lister else os.listdir(root)
    except OSError:
        return []

    def version_key(name: str) -> Tuple:
        parts = name.lstrip("v").split(".")
        numeric = []
        for part in parts:
            try:
                numeric.append(int(part))
            except ValueError:
                numeric.append(-1)
        return tuple(numeric)

    return [
        os.path.join(root, name, "bin")
        for name in sorted(entries, key=version_key, reverse=True)
    ]


def search_dirs_for(
    name: str,
    home: str,
    path_env: str = "",
    extra: Sequence[str] = (),
) -> List[str]:
    """Directories to look in for ``name``, best candidate first.

    The caller's own `PATH` is consulted first — when a human runs the
    installer from a working Terminal, that shell already knows the answer, and
    using it means the installed job points at the same executable the human
    just tested. Everything after it is a shell-independent fallback.
    """
    dirs = list(extra)
    dirs.extend(d for d in (path_env or "").split(os.pathsep) if d)
    template = CLAUDE_SEARCH_DIRS if name == "claude" else NODE_SEARCH_DIRS
    dirs.extend(_expand(d, home) for d in template)
    if name == "node":
        dirs.extend(nvm_node_dirs(home))
    return dedupe([_expand(d, home) for d in dirs])


def find_executable(
    name: str,
    dirs: Sequence[str],
    is_exec: Callable[[str], bool] = is_executable_file,
) -> Optional[str]:
    """The first executable ``name`` in ``dirs``, as an absolute path."""
    for directory in dirs:
        candidate = os.path.join(directory, name)
        if is_exec(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------
# Does this claude need Node?
# --------------------------------------------------------------------------


def read_shebang(path: str) -> Optional[str]:
    """The interpreter line of a script, or None for a binary/unreadable file."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    line = head.split(b"\n", 1)[0]
    try:
        return line[2:].decode("utf-8", "replace").strip()
    except Exception:  # pragma: no cover - decode with 'replace' cannot raise
        return None


def script_body(path: str, limit: int = 8192) -> str:
    """The first ``limit`` bytes of a text script, or '' for a binary."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(limit)
    except OSError:
        return ""
    if not head.startswith(b"#!"):
        return ""
    return head.decode("utf-8", "replace")


def looks_node_based(
    path: str,
    shebang_reader: Callable[[str], Optional[str]] = read_shebang,
    body_reader: Callable[[str], str] = script_body,
) -> bool:
    """Whether this ``claude`` is a Node program rather than a native binary.

    A first-guess only. The authoritative answer comes from the probe: if the
    executable runs under a Node-free PATH, it did not need Node whatever this
    says.
    """
    shebang = shebang_reader(path)
    if shebang is None:
        return False  # no shebang: a native binary
    tokens = shebang.split()
    if not tokens:
        return False
    # "#!/usr/bin/env node" and "#!/opt/node/bin/node" both name Node directly.
    if any(os.path.basename(token).startswith("node") for token in tokens):
        return True
    # "#!/bin/bash" is a wrapper; whether it needs Node is inside the script.
    if os.path.basename(tokens[0]) in _WRAPPER_INTERPRETERS:
        return bool(re.search(r"\bnode\b", body_reader(path)))
    return False


# --------------------------------------------------------------------------
# Building the PATH a plist will carry
# --------------------------------------------------------------------------


def build_path(
    *bins: Optional[str],
    base: str = LAUNCHD_BASE_PATH,
    optional_dirs: Sequence[str] = OPTIONAL_PATH_DIRS,
    exists: Callable[[str], bool] = os.path.isdir,
) -> str:
    """A `PATH` of absolute directories only.

    Resolved executables come first so the job uses exactly the `claude` and
    `node` that were verified, never something a future install drops earlier
    on the search path.
    """
    dirs = [os.path.dirname(b) for b in bins if b]
    dirs.extend(d for d in base.split(os.pathsep) if d)
    dirs.extend(d for d in optional_dirs if exists(d))
    return os.pathsep.join(dedupe(dirs))


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve_runtime(
    home: str,
    probe: Callable[[str, str], ProbeResult],
    path_env: str = "",
    extra_claude_dirs: Sequence[str] = (),
    extra_node_dirs: Sequence[str] = (),
    is_exec: Callable[[str], bool] = is_executable_file,
    node_hint: Callable[[str], bool] = looks_node_based,
    realpath: Callable[[str], str] = os.path.realpath,
    base_path: str = LAUNCHD_BASE_PATH,
    dir_exists: Callable[[str], bool] = os.path.isdir,
) -> RuntimeResolution:
    """Resolve, and *verify*, everything a scheduled Claude Code run needs.

    The verification step is the point. ``probe(executable, path)`` is expected
    to run the executable with `PATH` set to exactly ``path`` and nothing else
    inherited — the same starting conditions launchd will give it. A resolution
    is returned as ``ok`` only when a probe actually succeeded.
    """
    result = RuntimeResolution(path=base_path)

    # --- 1. locate claude -------------------------------------------------
    result.checks.append("claude_executable_resolved")
    claude_dirs = search_dirs_for("claude", home, path_env, extra_claude_dirs)
    claude = find_executable("claude", claude_dirs, is_exec)
    if claude is None:
        result.problems.append(
            "could not locate a 'claude' executable. Looked in: %s. Install Claude "
            "Code, or export CLAUDE_BIN=/absolute/path/to/claude and re-run."
            % ", ".join(claude_dirs[:8])
        )
        return result
    result.claude_bin = claude
    target = realpath(claude)
    if target != claude:
        result.claude_target = target
        result.notes.append(
            "%s is a link to %s; the link is what gets persisted, so a Claude Code "
            "update does not silently point the schedule at a deleted version."
            % (claude, target)
        )

    # --- 2. try it with no Node at all -----------------------------------
    result.checks.append("claude_starts_under_minimal_launchd_path")
    node_expected = node_hint(claude)
    if node_expected:
        result.notes.append(
            "%s looks Node-based (it has a script interpreter line), so a Node "
            "runtime is expected to be required." % claude
        )

    without_node = build_path(
        claude, base=base_path, exists=dir_exists
    )
    attempt = probe(claude, without_node)
    if attempt.ok:
        result.path = without_node
        result.node_required = False
        # Record Node when it happens to be resolvable, but do not require it.
        node = find_executable(
            "node", search_dirs_for("node", home, path_env, extra_node_dirs), is_exec
        )
        if node:
            result.node_bin = node
        result.notes.append(
            "claude ran under a Node-free PATH, so this install needs no Node "
            "runtime on the schedule's PATH."
        )
        return result

    # --- 3. it needs help; resolve Node ----------------------------------
    result.checks.append("node_executable_resolved")
    node_dirs = search_dirs_for("node", home, path_env, extra_node_dirs)
    node = find_executable("node", node_dirs, is_exec)
    if node is None:
        result.problems.append(
            "'%s' could not start under launchd's minimal PATH (%s) and no 'node' "
            "executable could be found to repair it. Looked in: %s. Install Node, or "
            "export NODE_BIN=/absolute/path/to/node and re-run."
            % (claude, without_node, ", ".join(node_dirs[:8]))
        )
        if attempt.detail:
            result.problems.append("probe said: %s" % attempt.detail)
        return result
    result.node_bin = node

    # --- 4. try again, with Node's directory on PATH ---------------------
    result.checks.append("claude_starts_with_resolved_node_on_path")
    with_node = build_path(claude, node, base=base_path, exists=dir_exists)
    retry = probe(claude, with_node)
    if not retry.ok:
        result.problems.append(
            "'%s' could not start even with the resolved Node (%s) on PATH. This "
            "installation cannot be made independent of shell initialisation, so the "
            "schedule must not be enabled." % (claude, node)
        )
        if retry.detail:
            result.problems.append("probe said: %s" % retry.detail)
        elif attempt.detail:
            result.problems.append("first probe said: %s" % attempt.detail)
        return result

    result.path = with_node
    result.node_required = True
    result.notes.append(
        "Node at %s is required and its directory is now pinned into the job's PATH; "
        "nvm shell initialisation is not consulted at run time." % node
    )
    return result


# --------------------------------------------------------------------------
# Plist rendering
# --------------------------------------------------------------------------


def plist_values(
    repo_root: str,
    home: str,
    resolution: RuntimeResolution,
) -> Dict[str, str]:
    """The placeholder substitutions a rendered plist needs.

    ``NODE_BIN`` is written only when the probe proved Node necessary. A native
    Claude Code binary needs none, and naming one anyway would put a Node
    version ahead of the rest of the job's PATH for no reason — the plist should
    declare exactly what was verified and nothing more.
    """
    return {
        "__REPO_ROOT__": repo_root,
        "__HOME__": home,
        "__PATH__": resolution.path,
        "__CLAUDE_BIN__": resolution.claude_bin or "",
        "__NODE_BIN__": (resolution.node_bin or "") if resolution.node_required else "",
    }


def render_plist(template: str, values: Dict[str, str]) -> str:
    """Substitute every placeholder in a plist template."""
    rendered = template
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def unresolved_placeholders(text: str) -> List[str]:
    """Placeholders a render left behind. A rendered plist must have none."""
    return sorted(set(PLACEHOLDER_RE.findall(text)))
