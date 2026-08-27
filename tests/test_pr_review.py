import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.review_prepare import parse_unified_diff, prepare_review
from aiweekend_target.lab.scenarios import LabPaths
from aiweekend_target.repo_rag.search import RepoSearch


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
