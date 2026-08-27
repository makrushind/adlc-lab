import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anyio
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.review_prepare import parse_unified_diff, prepare_review
from aiweekend_target.lab.scenarios import LabPaths
from aiweekend_target.repo_rag.lint import lint_pr, validate_lint_response
from aiweekend_target.repo_rag.search import RepoSearch
from aiweekend_target.repo_rag.server import create_server, health_check


class PrepareReviewTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, data: bytes | str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def _diff(self, old: str, new: str, hunk: str = "@@ -1 +1 @@\n-old\n+new\n") -> str:
        return f"diff --git a/{old} b/{new}\n--- a/{old}\n+++ b/{new}\n{hunk}"

    def _prepare(self, root: Path, diff: str) -> tuple[dict[str, object], LabPaths, Path]:
        source = root / "source"
        source.mkdir()
        self._write(source, "app.py", "old\n")
        diff_path = root / "pr.diff"
        diff_path.write_text(diff, encoding="utf-8")
        marker = root / "baseline-scenario.json"
        marker.write_text('{"id":"baseline"}\n', encoding="utf-8")
        paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
        return prepare_review(paths, source, diff_path, marker), paths, source

    def test_parses_literal_added_lines_as_immutable_sorted_changes(self) -> None:
        document = self._diff(
            "app.py",
            "app.py",
            "@@ -3,2 +5,3 @@ heading\n keep\n+added one\n+added two\n-old\n",
        )
        changes = parse_unified_diff(document)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].path, "app.py")
        self.assertEqual(changes[0].added_lines, (6, 7))
        self.assertFalse(changes[0].deleted)
        with self.assertRaises(Exception):
            changes[0].path = "other.py"

    def test_parses_added_and_renamed_files_and_rejects_malformed_records(self) -> None:
        added = "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        renamed = "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n"
        self.assertEqual(parse_unified_diff(added)[0].added_lines, (1, 2))
        self.assertEqual(parse_unified_diff(renamed)[0].path, "new.py")
        for malformed in (
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ bad @@\n",
            "diff --git a/app.py b/app.py\n--- a/other.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n same\n",
            "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n"
            "diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n",
        ):
            with self.subTest(malformed=malformed), self.assertRaises(TargetError) as raised:
                parse_unified_diff(malformed)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_parses_git_headers_for_paths_with_spaces(self) -> None:
        quoted = (
            'diff --git "a/space name.py" "b/space name.py"\n'
            '--- "a/space name.py"\n'
            '+++ "b/space name.py"\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
        unquoted = (
            "diff --git a/space name.py b/space name.py\n"
            "--- a/space name.py\n"
            "+++ b/space name.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        renamed_to_spaced = (
            'diff --git a/old.py "b/new name.py"\n'
            'similarity index 100%\n'
            'rename from old.py\n'
            'rename to "new name.py"\n'
        )
        octal_quoted = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            '@@ -1 +1 @@\n-old\n+new\n'
        )
        for document in (quoted, unquoted, renamed_to_spaced, octal_quoted):
            with self.subTest(document=document[:16]):
                expected = "new name.py" if document == renamed_to_spaced else "café.py" if document == octal_quoted else "space name.py"
                self.assertEqual(parse_unified_diff(document)[0].path, expected)

    def test_parses_unquoted_nested_paths_with_a_b_separator(self) -> None:
        modified = (
            "diff --git a/x b/z.py b/x b/z.py\n"
            "--- a/x b/z.py\t\n"
            "+++ b/x b/z.py\t\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        renamed = (
            "diff --git a/x b/old.py b/x b/new.py\n"
            "similarity index 100%\n"
            "rename from x b/old.py\n"
            "rename to x b/new.py\n"
        )
        self.assertEqual(parse_unified_diff(modified)[0].path, "x b/z.py")
        self.assertEqual(parse_unified_diff(renamed)[0].path, "x b/new.py")

    def test_rejects_changed_target_below_a_skipped_symlink_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            outside = root / "outside"
            source.mkdir()
            outside.mkdir()
            self._write(outside, "file.py", "outside\n")
            (source / "node_modules").symlink_to(outside, target_is_directory=True)
            diff = root / "pr.diff"
            diff.write_text(self._diff("node_modules/file.py", "node_modules/file.py"), encoding="utf-8")
            marker = root / "marker.json"
            marker.write_text("{}", encoding="utf-8")
            with self.assertRaises(TargetError) as raised:
                prepare_review(LabPaths(root / "workspace", root / "corpus", root / "rag-index"), source, diff, marker)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_rejects_added_and_deleted_hunks_with_opposite_side_content(self) -> None:
        contradictory_deleted = (
            "diff --git a/removed.py b/removed.py\n"
            "--- a/removed.py\n"
            "+++ /dev/null\n"
            "@@ -1 +1 @@\n-old\n+impossible\n"
        )
        contradictory_added = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        for document in (contradictory_deleted, contradictory_added):
            with self.subTest(document=document[:24]), self.assertRaises(TargetError) as raised:
                parse_unified_diff(document)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_prepares_only_allowlisted_files_and_leaves_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diff = self._diff("app.py", "app.py")
            result, paths, source = self._prepare(root, diff)
            self._write(source, "docs/readme.md", "searchable documentation\n")
            self._write(source, "assets/logo.png", b"\x89PNG\r\n")
            result = prepare_review(paths, source, root / "pr.diff", root / "baseline-scenario.json")
            self.assertEqual(result, {"ok": True, "prepared": True, "copied_files": 2, "copied_bytes": 29, "changed_files": 1})
            self.assertEqual((source / "app.py").read_text(encoding="utf-8"), "old\n")
            self.assertTrue((paths.corpus / "app.py").is_file())
            self.assertTrue((paths.corpus / "docs/readme.md").is_file())
            self.assertFalse((paths.corpus / "assets/logo.png").exists())
            results = RepoSearch(paths.rag_index / "index.sqlite").search_repo("searchable")["results"]
            self.assertEqual([item["path"] for item in results], ["docs/readme.md"])

    def test_deleted_file_is_accepted_without_existing_pr_head_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deleted_diff = "diff --git a/removed.py b/removed.py\n--- a/removed.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
            result, paths, _ = self._prepare(root, deleted_diff)
            self.assertTrue(result["prepared"])
            self.assertEqual(result["changed_files"], 1)
            self.assertFalse((paths.corpus / "removed.py").exists())

    def test_rejects_unsafe_binary_and_oversized_diffs(self) -> None:
        cases = (
            "\x00",
            "diff --git a/app.py b/app.py\nGIT binary patch\n",
            "diff --git a/app.py b/app.py\nBinary files a/app.py and b/app.py differ\n",
            self._diff("../escape.py", "../escape.py"),
            self._diff("app.py", "app.py") + "x" * (512 * 1024),
        )
        for document in cases:
            with self.subTest(document=document[:32]), self.assertRaises(TargetError) as raised:
                parse_unified_diff(document)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_rejects_missing_changed_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            diff = root / "pr.diff"
            diff.write_text(self._diff("missing.py", "missing.py"), encoding="utf-8")
            marker = root / "marker.json"
            marker.write_text("{}", encoding="utf-8")
            with self.assertRaises(TargetError) as raised:
                prepare_review(LabPaths(root / "workspace", root / "corpus", root / "rag-index"), source, diff, marker)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_skips_unsafe_directories_without_traversing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, paths, source = self._prepare(root, self._diff("app.py", "app.py"))
            self._write(source, "node_modules/package/bad.py", b"\xff")
            self._write(source, ".git/config", "ignored")
            prepare_review(paths, source, root / "pr.diff", root / "baseline-scenario.json")
            self.assertFalse((paths.corpus / "node_modules").exists())
            self.assertFalse((paths.corpus / ".git").exists())

    def test_rejects_symlinks_credentials_and_non_utf8_allowed_files(self) -> None:
        cases = (("link.py", "symlink"), (".env", "credential"), ("secret.pem", "credential"), ("bad.txt", "non_utf8"))
        for relative, kind in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, paths, source = self._prepare(root, self._diff("app.py", "app.py"))
                if kind == "symlink":
                    (source / relative).symlink_to(source / "app.py")
                elif kind == "non_utf8":
                    self._write(source, relative, b"\xff")
                else:
                    self._write(source, relative, "secret")
                with self.assertRaises(TargetError) as raised:
                    prepare_review(paths, source, root / "pr.diff", root / "baseline-scenario.json")
                self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_rejects_file_count_per_file_and_total_size_caps(self) -> None:
        cases = (("count", 1001), ("file", 1), ("total", 41))
        for kind, count in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, paths, source = self._prepare(root, self._diff("app.py", "app.py"))
                if kind == "count":
                    for number in range(count):
                        self._write(source, f"many/{number}.txt", "x")
                elif kind == "file":
                    self._write(source, "large.txt", b"x" * (256 * 1024 + 1))
                else:
                    for number in range(count):
                        self._write(source, f"total/{number}.txt", b"x" * (256 * 1024))
                with self.assertRaises(TargetError) as raised:
                    prepare_review(paths, source, root / "pr.diff", root / "baseline-scenario.json")
                self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_preparation_creates_all_volume_outputs_and_cli_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, paths, _ = self._prepare(root, self._diff("app.py", "app.py"))
            self.assertTrue((paths.workspace / "pr.diff").is_file())
            self.assertEqual((paths.workspace / "pr.diff").read_bytes(), (root / "pr.diff").read_bytes().rstrip(b"\n") + b"\n")
            self.assertTrue((paths.rag_index / "index.sqlite").is_file())
            self.assertEqual((paths.rag_index / "scenario.json").read_text(encoding="utf-8"), '{"id":"baseline"}\n')
            self.assertTrue(result["ok"])
            from aiweekend_target import __main__

            output = io.StringIO()
            with mock.patch.object(__main__, "_WORKSPACE", paths.workspace), mock.patch.object(__main__, "_CORPUS", paths.corpus), mock.patch.object(__main__, "_RAG_INDEX", paths.rag_index), mock.patch.object(__main__, "_PREPARE_SOURCE", root / "source"), mock.patch.object(__main__, "_PREPARE_DIFF", root / "pr.diff"), mock.patch.object(__main__, "_PREPARE_MARKER", root / "baseline-scenario.json"):
                self.assertEqual(__main__.main(["prepare-review"], output=output), 0)
            self.assertTrue(json.loads(output.getvalue())["prepared"])


class PullRequestLintTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, data: bytes | str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def test_reports_direct_eval_only_when_its_start_line_was_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "pkg/check.py", "value = eval(source)\nother = eval(source)\n")
            self.assertEqual(
                lint_pr(root, [{"path": "pkg/check.py", "added_lines": [2]}]),
                {
                    "diagnostics": [
                        {
                            "path": "pkg/check.py",
                            "line": 2,
                            "column": 9,
                            "rule": "ADLC001",
                            "severity": "high",
                            "message": "Avoid eval() on untrusted input",
                        }
                    ]
                },
            )

    def test_rejects_unsafe_missing_symlink_oversized_and_invalid_python_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside.py"
            outside.write_text("eval(value)\n", encoding="utf-8")
            self._write(root, "large.py", b"#" * (256 * 1024 + 1))
            self._write(root, "bad.py", "if True print('missing colon')\n")
            self._write(root, "non_utf8.py", b"\xff")
            (root / "link.py").symlink_to(outside)
            for target in (
                {"path": "/absolute.py", "added_lines": [1]},
                {"path": "../outside.py", "added_lines": [1]},
                {"path": "missing.py", "added_lines": [1]},
                {"path": "link.py", "added_lines": [1]},
                {"path": "large.py", "added_lines": [1]},
                {"path": "non_utf8.py", "added_lines": [1]},
                {"path": "bad.py", "added_lines": [1]},
            ):
                with self.subTest(target=target), self.assertRaises(TargetError) as raised:
                    lint_pr(root, [target])
                self.assertIn(raised.exception.code, {ErrorCode.POLICY, ErrorCode.MCP})

    def test_validates_strict_response_shape(self) -> None:
        valid = {
            "diagnostics": [
                {
                    "path": "nested/check.py",
                    "line": 4,
                    "column": 2,
                    "rule": "ADLC001",
                    "severity": "high",
                    "message": "Avoid eval() on untrusted input",
                }
            ]
        }
        self.assertEqual(validate_lint_response(valid), valid)
        for invalid in (
            {"diagnostics": [], "other": []},
            {"diagnostics": [{"path": "../bad.py", "line": 1, "column": 1, "rule": "ADLC001", "severity": "high", "message": "Avoid eval() on untrusted input"}]},
            {"diagnostics": [{"path": "good.py", "line": 0, "column": 1, "rule": "ADLC001", "severity": "high", "message": "Avoid eval() on untrusted input"}]},
            {"diagnostics": [{"path": "good.py", "line": 1, "column": 0, "rule": "ADLC001", "severity": "low", "message": "Avoid eval() on untrusted input"}]},
            {"diagnostics": [{"path": "good.py", "line": 1, "column": 1, "rule": "OTHER", "severity": "high", "message": "wrong"}]},
            {"diagnostics": [{"path": "good.py", "line": 1, "column": 1, "rule": "ADLC001", "severity": "high", "message": "Avoid eval() on untrusted input", "extra": True}]},
            {"diagnostics": [{"path": "good.py", "line": 1, "column": 1, "rule": "ADLC001", "severity": "high", "message": "Avoid eval() on untrusted input"} for _ in range(101)]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(TargetError):
                validate_lint_response(invalid)

    def test_orders_and_caps_diagnostics_and_target_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "z.py", "\n".join("eval(value)" for _ in range(101)) + "\n")
            self._write(root, "a.py", "eval(value)\n")
            result = lint_pr(root, [{"path": "z.py", "added_lines": list(range(1, 102))}, {"path": "a.py", "added_lines": [1]}])
            self.assertEqual(len(result["diagnostics"]), 100)
            self.assertEqual(result["diagnostics"][0]["path"], "a.py")
            self.assertEqual(result["diagnostics"][-1], {"path": "z.py", "line": 99, "column": 1, "rule": "ADLC001", "severity": "high", "message": "Avoid eval() on untrusted input"})
            with self.assertRaises(TargetError):
                lint_pr(root, [{"path": "a.py", "added_lines": list(range(1, 10_002))}])
            with self.assertRaises(TargetError):
                lint_pr(root, [{"path": "a.py", "added_lines": []} for _ in range(101)])

    def test_server_exposes_linter_only_in_explicit_review_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "index.sqlite"
            self.assertEqual([tool.name for tool in anyio.run(create_server(database).list_tools)], ["search_repo"])
            self.assertEqual(
                [tool.name for tool in anyio.run(create_server(database, review_mode=True, corpus_root=root).list_tools)],
                ["search_repo", "lint_pr"],
            )
            with mock.patch.dict("os.environ", {"ADLC_PR_REVIEW_MODE": "invalid"}, clear=False):
                with self.assertRaises(TargetError):
                    create_server(database)

    def test_health_contracts_keep_search_and_follow_the_environment_mode(self) -> None:
        from aiweekend_target.repo_rag.index import build_index

        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            corpus = root / "corpus"
            self._write(corpus, "safe.py", "answer = 42\n")
            database = root / "index.sqlite"
            build_index(corpus, database)
            marker = root / "scenario.json"
            marker.write_text('{"schema":1,"id":"baseline","attack_surface":"none","canary":null,"payload_file":null}\n', encoding="utf-8")
            scenarios = repository / "scenarios"
            self.assertEqual(anyio.run(health_check, database, marker, scenarios), {"status": "ready"})
            with mock.patch.dict("os.environ", {"ADLC_PR_REVIEW_MODE": "1"}, clear=False):
                self.assertEqual(anyio.run(health_check, database, marker, scenarios), {"status": "ready"})
