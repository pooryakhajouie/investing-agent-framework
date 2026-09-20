"""launchd runtime resolution — the fix for `claude: not found` at 10:30 AM.

The property under test: **the installed job depends on nothing a shell did.**
No `.zshrc`, no `nvm.sh`, no open Terminal. Every executable is resolved to an
absolute path at install time, verified by running it under a launchd-like
environment, and persisted into the plist.

The probe is injected throughout, so these tests describe the resolution
*logic* deterministically instead of depending on what happens to be installed
on the machine running them.
"""

from __future__ import annotations

import os
import shutil
import plistlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.launchd import (
    TCC_PROTECTED_DIRS,
    tcc_protected_ancestor,  # noqa: E402
    LAUNCHD_BASE_PATH,
    ProbeResult,
    build_path,
    dedupe,
    find_executable,
    looks_node_based,
    nvm_node_dirs,
    plist_values,
    read_shebang,
    render_plist,
    resolve_runtime,
    search_dirs_for,
    unresolved_placeholders,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLIST_TEMPLATES = (
    "com.robinhood-agent.evaluation.plist",
    "com.robinhood-agent.evaluation-pm.plist",
)


def always(result: ProbeResult):
    return lambda executable, path: result


def probe_requiring(needle: str, detail: str = "node: command not found"):
    """A probe that succeeds only when ``needle`` appears in the PATH."""

    def probe(executable: str, path: str) -> ProbeResult:
        if needle in path:
            return ProbeResult(True, "1.2.3 (Claude Code)")
        return ProbeResult(False, detail)

    return probe


def touch_exec(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class PathBuildingTests(unittest.TestCase):
    def test_dedupe_preserves_order_and_drops_blanks(self):
        self.assertEqual(dedupe(["a", "", "b", "a", "c", "b"]), ["a", "b", "c"])

    def test_a_built_path_puts_resolved_executables_first(self):
        path = build_path(
            "/opt/claude/bin/claude", "/nvm/v20/bin/node",
            base="/usr/bin:/bin", optional_dirs=(), exists=lambda d: False,
        )
        self.assertEqual(path, "/opt/claude/bin:/nvm/v20/bin:/usr/bin:/bin")

    def test_a_built_path_contains_only_absolute_directories(self):
        path = build_path(
            "/opt/claude/bin/claude", base=LAUNCHD_BASE_PATH,
            optional_dirs=(), exists=lambda d: False,
        )
        for entry in path.split(os.pathsep):
            with self.subTest(entry=entry):
                self.assertTrue(entry.startswith("/"), entry)

    def test_optional_directories_are_included_only_when_they_exist(self):
        with_brew = build_path(
            "/opt/claude/bin/claude", base="/usr/bin",
            optional_dirs=("/opt/homebrew/bin",), exists=lambda d: True,
        )
        without = build_path(
            "/opt/claude/bin/claude", base="/usr/bin",
            optional_dirs=("/opt/homebrew/bin",), exists=lambda d: False,
        )
        self.assertIn("/opt/homebrew/bin", with_brew)
        self.assertNotIn("/opt/homebrew/bin", without)

    def test_a_missing_executable_contributes_no_directory(self):
        self.assertEqual(
            build_path(None, base="/usr/bin", optional_dirs=(), exists=lambda d: False),
            "/usr/bin",
        )


class SearchDirTests(unittest.TestCase):
    def test_the_callers_own_path_is_searched_first(self):
        dirs = search_dirs_for("claude", "/home/u", path_env="/opt/first:/opt/second")
        self.assertEqual(dirs[0], "/opt/first")
        self.assertEqual(dirs[1], "/opt/second")

    def test_shell_independent_fallbacks_follow(self):
        dirs = search_dirs_for("claude", "/home/u", path_env="")
        self.assertIn("/home/u/.local/bin", dirs)
        self.assertIn("/home/u/.claude/local", dirs)
        self.assertIn("/opt/homebrew/bin", dirs)

    def test_nvm_directories_are_read_from_disk_not_from_the_shell(self):
        """nvm is shell functions plus a PATH entry. launchd sees neither."""
        with tempfile.TemporaryDirectory() as home:
            versions = os.path.join(home, ".nvm", "versions", "node")
            for name in ("v18.20.4", "v20.17.0", "v24.19.0"):
                os.makedirs(os.path.join(versions, name, "bin"))
            dirs = nvm_node_dirs(home)
            self.assertEqual(
                dirs,
                [
                    os.path.join(versions, "v24.19.0", "bin"),
                    os.path.join(versions, "v20.17.0", "bin"),
                    os.path.join(versions, "v18.20.4", "bin"),
                ],
            )

    def test_a_missing_nvm_directory_is_not_an_error(self):
        self.assertEqual(nvm_node_dirs("/nonexistent-home-xyz"), [])

    def test_node_search_includes_nvm(self):
        with tempfile.TemporaryDirectory() as home:
            nvm_bin = os.path.join(home, ".nvm", "versions", "node", "v20.17.0", "bin")
            os.makedirs(nvm_bin)
            self.assertIn("node", "node")
            self.assertIn(nvm_bin, search_dirs_for("node", home))


class FindExecutableTests(unittest.TestCase):
    def test_the_first_executable_match_wins(self):
        with tempfile.TemporaryDirectory() as root:
            second = touch_exec(os.path.join(root, "b", "claude"))
            touch_exec(os.path.join(root, "a", "other"))
            found = find_executable("claude", [os.path.join(root, "a"), os.path.join(root, "b")])
            self.assertEqual(found, second)

    def test_a_non_executable_file_is_not_a_match(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "claude")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not executable")
            self.assertIsNone(find_executable("claude", [root]))

    def test_nothing_found_returns_none(self):
        self.assertIsNone(find_executable("claude", ["/nonexistent-xyz"]))


class NodeDetectionTests(unittest.TestCase):
    def test_a_native_binary_has_no_shebang(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "claude")
            with open(path, "wb") as handle:
                handle.write(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)  # Mach-O magic
            self.assertIsNone(read_shebang(path))
            self.assertFalse(looks_node_based(path))

    def test_a_node_shebang_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "claude")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/usr/bin/env node\nconsole.log('hi')\n")
            self.assertEqual(read_shebang(path), "/usr/bin/env node")
            self.assertTrue(looks_node_based(path))

    def test_a_shell_wrapper_that_calls_node_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "claude")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/bash\nexec node /opt/cli.js \"$@\"\n")
            self.assertTrue(looks_node_based(path))

    def test_a_shell_wrapper_that_does_not_call_node_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "claude")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/bash\nexec /opt/claude/claude-native \"$@\"\n")
            self.assertFalse(looks_node_based(path))


class ResolutionTests(unittest.TestCase):
    """Resolution is only ever reported as ok when a probe actually succeeded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_a_native_binary_resolves_without_node(self):
        claude = touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        result = resolve_runtime(
            home=self.home,
            probe=always(ProbeResult(True, "2.1.263 (Claude Code)")),
            dir_exists=lambda d: False,
        )
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.claude_bin, claude)
        self.assertFalse(result.node_required)
        self.assertIn("claude_starts_under_minimal_launchd_path", result.checks)

    def test_the_resolved_path_is_absolute_and_shell_independent(self):
        touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        result = resolve_runtime(
            home=self.home,
            probe=always(ProbeResult(True)),
            dir_exists=lambda d: False,
        )
        for entry in result.path.split(os.pathsep):
            with self.subTest(entry=entry):
                self.assertTrue(entry.startswith("/"))
        self.assertNotIn("$", result.path)
        self.assertNotIn("~", result.path)

    def test_a_node_based_install_pins_the_resolved_node_directory(self):
        """The nvm case: claude only runs once node's bin dir is on PATH."""
        touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        nvm_bin = os.path.join(self.home, ".nvm", "versions", "node", "v20.17.0", "bin")
        node = touch_exec(os.path.join(nvm_bin, "node"))

        result = resolve_runtime(
            home=self.home,
            probe=probe_requiring(nvm_bin),
            dir_exists=lambda d: False,
        )
        self.assertTrue(result.ok, result.problems)
        self.assertTrue(result.node_required)
        self.assertEqual(result.node_bin, node)
        self.assertIn(nvm_bin, result.path)
        self.assertIn("claude_starts_with_resolved_node_on_path", result.checks)

    def test_failure_when_claude_cannot_be_found_at_all(self):
        result = resolve_runtime(
            home=self.home,
            probe=always(ProbeResult(True)),
            dir_exists=lambda d: False,
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.problems)
        self.assertIn("could not locate a 'claude' executable", result.problems[0])
        self.assertIn("CLAUDE_BIN", result.problems[0])

    def test_failure_when_node_is_needed_but_absent(self):
        touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        result = resolve_runtime(
            home=self.home,
            probe=probe_requiring("/nowhere/that/exists"),
            dir_exists=lambda d: False,
        )
        self.assertFalse(result.ok)
        blob = " ".join(result.problems)
        self.assertIn("no 'node' executable could be found", blob)
        self.assertIn("NODE_BIN", blob)

    def test_failure_when_claude_will_not_start_even_with_node(self):
        touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        touch_exec(os.path.join(self.home, ".nvm", "versions", "node", "v20.17.0", "bin", "node"))
        result = resolve_runtime(
            home=self.home,
            probe=always(ProbeResult(False, "exited 127")),
            dir_exists=lambda d: False,
        )
        self.assertFalse(result.ok)
        blob = " ".join(result.problems)
        self.assertIn("could not start even with the resolved Node", blob)
        self.assertIn("must not be enabled", blob)

    def test_a_failed_resolution_is_never_reported_as_ok(self):
        """The load-bearing invariant: no probe success, no installation."""
        touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        result = resolve_runtime(
            home=self.home,
            probe=always(ProbeResult(False, "boom")),
            dir_exists=lambda d: False,
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.to_dict()["ok"])

    def test_a_symlinked_claude_persists_the_link_not_the_version(self):
        claude = touch_exec(os.path.join(self.home, ".local", "bin", "claude"))
        result = resolve_runtime(
            home=self.home,
            probe=always(ProbeResult(True)),
            realpath=lambda p: "/versions/2.1.263",
            dir_exists=lambda d: False,
        )
        self.assertEqual(result.claude_bin, claude)
        self.assertEqual(result.claude_target, "/versions/2.1.263")
        self.assertTrue(any("is a link to" in note for note in result.notes))


class PlistRenderingTests(unittest.TestCase):
    def test_every_placeholder_is_substituted(self):
        for name in PLIST_TEMPLATES:
            with self.subTest(name=name):
                with open(os.path.join(REPO_ROOT, "scheduler", name)) as handle:
                    template = handle.read()
                resolution = resolve_runtime(
                    home="/home/u",
                    probe=always(ProbeResult(True)),
                    extra_claude_dirs=[],
                    is_exec=lambda p: p == "/opt/bin/claude",
                    node_hint=lambda p: False,
                    realpath=lambda p: p,
                    dir_exists=lambda d: False,
                    path_env="/opt/bin",
                )
                self.assertTrue(resolution.ok, resolution.problems)
                rendered = render_plist(
                    template, plist_values("/repo", "/home/u", resolution)
                )
                self.assertEqual(unresolved_placeholders(rendered), [])

    def test_a_rendered_plist_carries_the_absolute_claude_path(self):
        with open(os.path.join(REPO_ROOT, "scheduler", PLIST_TEMPLATES[0])) as handle:
            template = handle.read()
        resolution = resolve_runtime(
            home="/home/u",
            probe=always(ProbeResult(True)),
            is_exec=lambda p: p == "/opt/bin/claude",
            node_hint=lambda p: False,
            realpath=lambda p: p,
            dir_exists=lambda d: False,
            path_env="/opt/bin",
        )
        rendered = render_plist(template, plist_values("/repo", "/home/u", resolution))
        data = plistlib.loads(rendered.encode("utf-8"))
        env = data["EnvironmentVariables"]
        self.assertEqual(env["CLAUDE_BIN"], "/opt/bin/claude")
        self.assertEqual(env["HOME"], "/home/u")
        self.assertEqual(env["TZ"], "America/Chicago")
        self.assertIn("/opt/bin", env["PATH"])
        self.assertEqual(data["ProgramArguments"][0], "/repo/scripts/scheduled_evaluation.sh")

    def test_node_bin_is_written_only_when_node_is_required(self):
        with open(os.path.join(REPO_ROOT, "scheduler", PLIST_TEMPLATES[0])) as handle:
            template = handle.read()

        not_required = plist_values(
            "/repo", "/home/u",
            resolve_runtime(
                home="/home/u",
                probe=always(ProbeResult(True)),
                is_exec=lambda p: p in ("/opt/bin/claude", "/opt/bin/node"),
                node_hint=lambda p: False,
                realpath=lambda p: p,
                dir_exists=lambda d: False,
                path_env="/opt/bin",
            ),
        )
        self.assertEqual(not_required["__NODE_BIN__"], "")

        required = plist_values(
            "/repo", "/home/u",
            resolve_runtime(
                home="/home/u",
                probe=probe_requiring("/nodes"),
                is_exec=lambda p: p in ("/opt/bin/claude", "/nodes/node"),
                node_hint=lambda p: True,
                realpath=lambda p: p,
                dir_exists=lambda d: False,
                path_env="/opt/bin:/nodes",
            ),
        )
        self.assertEqual(required["__NODE_BIN__"], "/nodes/node")

        rendered = render_plist(template, not_required)
        self.assertEqual(unresolved_placeholders(rendered), [])
        self.assertEqual(
            plistlib.loads(rendered.encode("utf-8"))["EnvironmentVariables"]["NODE_BIN"], ""
        )

    def test_unresolved_placeholders_are_detected(self):
        self.assertEqual(
            unresolved_placeholders("<string>__REPO_ROOT__/x</string> __PATH__"),
            ["__PATH__", "__REPO_ROOT__"],
        )
        self.assertEqual(unresolved_placeholders("<string>/opt/bin</string>"), [])

    def test_the_shipped_templates_still_declare_the_runtime_keys(self):
        """A template that loses these silently reintroduces the PATH bug."""
        for name in PLIST_TEMPLATES:
            with self.subTest(name=name):
                with open(os.path.join(REPO_ROOT, "scheduler", name), "rb") as handle:
                    data = plistlib.loads(handle.read())
                env = data["EnvironmentVariables"]
                self.assertEqual(env["CLAUDE_BIN"], "__CLAUDE_BIN__")
                self.assertEqual(env["NODE_BIN"], "__NODE_BIN__")
                self.assertEqual(env["PATH"], "__PATH__")


if __name__ == "__main__":
    unittest.main()


class TccProtectedFolderTests(unittest.TestCase):
    """A repository under Desktop/Documents/Downloads cannot be scheduled.

    macOS grants access to those folders per application, and launchd does not
    inherit the grant the Terminal holds. The job dies before its first line:

        getcwd: cannot access parent directories: Operation not permitted
        /bin/bash: .../scheduled_evaluation.sh: Operation not permitted

    with exit code 126 — while running the same script by hand works perfectly.
    That asymmetry is what makes it worth a check: verification performed from a
    Terminal cannot see the failure, so it has to be reasoned about from the
    path rather than probed for. It cost one missed weekday evaluation and one
    missed weekly discovery run before it was caught.
    """

    HOME = "/Users/someone"

    def test_each_protected_folder_is_detected(self):
        for name in TCC_PROTECTED_DIRS:
            with self.subTest(folder=name):
                repo = os.path.join(self.HOME, name, "robinhood-agent")
                self.assertEqual(tcc_protected_ancestor(repo, self.HOME),
                                 os.path.join(self.HOME, name))

    def test_the_protected_folder_itself_is_detected(self):
        self.assertIsNotNone(
            tcc_protected_ancestor(os.path.join(self.HOME, "Desktop"), self.HOME))

    def test_a_nested_repository_is_detected(self):
        repo = os.path.join(self.HOME, "Documents", "a", "b", "repo")
        self.assertIsNotNone(tcc_protected_ancestor(repo, self.HOME))

    def test_an_unprotected_location_is_clear(self):
        for path in ("projects/robinhood-agent", "src/repo", "code/x"):
            with self.subTest(path=path):
                self.assertIsNone(
                    tcc_protected_ancestor(os.path.join(self.HOME, path), self.HOME))

    def test_a_prefix_collision_is_not_a_false_positive(self):
        """'DesktopStuff' is not 'Desktop'."""
        for name in ("DesktopStuff", "Documents2", "Downloads-old"):
            with self.subTest(name=name):
                self.assertIsNone(
                    tcc_protected_ancestor(os.path.join(self.HOME, name, "r"),
                                           self.HOME))

    def test_another_users_desktop_is_not_ours(self):
        self.assertIsNone(
            tcc_protected_ancestor("/Users/other/Desktop/repo", self.HOME))

    def test_a_symlink_into_a_protected_folder_is_caught(self):
        """The kernel enforces on the real path, so the check must resolve."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "home")
        real = os.path.join(home, "Desktop", "repo")
        os.makedirs(real)
        link = os.path.join(tmp, "link-to-repo")
        os.symlink(real, link)
        self.assertIsNotNone(tcc_protected_ancestor(link, home))

    def test_the_shipped_repository_is_not_in_a_protected_folder(self):
        """The live check: this repo must be schedulable."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertIsNone(
            tcc_protected_ancestor(repo_root, os.path.expanduser("~")),
            "this repository cannot be scheduled from its current location")

    def test_the_installer_refuses_to_enable_from_a_protected_folder(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_root, "scheduler", "install.sh"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("check_tcc_path", body)
        # the guard must run on the enable path, before anything is installed
        enable = body[body.index("  enable)"):body.index("verifying the rendered")]
        self.assertIn("check_tcc_path", enable)
        self.assertIn("Operation not permitted", body)
        self.assertIn("126", body)

    def test_the_installer_does_not_recommend_full_disk_access(self):
        """The fix is to move the repo, not to grant a shell blanket access."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_root, "scheduler", "install.sh"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("NOT", body.upper()[body.upper().index("FULL DISK ACCESS") - 200:
                                          body.upper().index("FULL DISK ACCESS") + 200])
