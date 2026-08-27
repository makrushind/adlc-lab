import io
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import anyio
from aiweekend_target.errors import ErrorCode, TargetError
from aiweekend_target.lab.review_prepare import ReviewChange, parse_unified_diff, prepare_review
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
        with self.assertRaises(FrozenInstanceError):
            changes[0].path = "other.py"

    def test_parses_real_git_metadata_only_records(self) -> None:
        document = (
            "diff --git a/added-empty.py b/added-empty.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "diff --git a/deleted-empty.py b/deleted-empty.py\n"
            "deleted file mode 100644\n"
            "index e69de29..0000000\n"
            "diff --git a/script.py b/script.py\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )
        self.assertEqual(
            parse_unified_diff(document),
            (
                ReviewChange("added-empty.py", (), False),
                ReviewChange("deleted-empty.py", (), True),
                ReviewChange("script.py", (), False),
            ),
        )

    def test_parses_git_minimum_index_abbreviation(self) -> None:
        document = (
            "diff --git a/app.py b/app.py\n"
            "index ecfd..ee7b 100644\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        self.assertEqual(
            parse_unified_diff(document),
            (ReviewChange("app.py", (1,), False),),
        )

    def test_parses_sha256_empty_blob_abbreviation(self) -> None:
        document = (
            "diff --git a/empty.py b/empty.py\n"
            "new file mode 100644\n"
            "index 0000..473a\n"
        )

        self.assertEqual(
            parse_unified_diff(document),
            (ReviewChange("empty.py", (), False),),
        )

    def test_rejects_incomplete_or_contradictory_metadata_only_records(self) -> None:
        for document in (
            "diff --git a/script.py b/script.py\nold mode 100644\n",
            "diff --git a/script.py b/script.py\nold mode 100644\nnew mode 100644\n",
            "diff --git a/empty.py b/empty.py\nnew file mode 100644\ndeleted file mode 100644\n",
            "diff --git a/empty.py b/empty.py\nnew file mode 100644\nindex 0000000..e69de29 100755\n",
            "diff --git a/empty.py b/empty.py\ndeleted file mode 100755\nindex e69de29..0000000 100644\n",
            "diff --git a/empty.py b/empty.py\nnew file mode 100644\nindex 0000..e69d 100644\n",
            "diff --git a/empty.py b/empty.py\ndeleted file mode 100755\nindex e69d..0000 100755\n",
            "diff --git a/empty.py b/empty.py\nnew file mode 100644\nindex 0000000..deadbee\n",
            "diff --git a/empty.py b/empty.py\nnew file mode 100644\nindex 0000..e69de29\n",
        ):
            with self.subTest(document=document), self.assertRaises(TargetError) as raised:
                parse_unified_diff(document)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

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

    def test_rejects_changed_python_below_skipped_directory_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._write(source, "vendor/check.py", "value = eval(source)\n")
            diff = root / "pr.diff"
            diff.write_text(self._diff("vendor/check.py", "vendor/check.py"), encoding="utf-8")
            marker = self._write(root, "marker.json", "{}")
            paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
            for target in (paths.workspace, paths.corpus, paths.rag_index):
                self._write(target, "sentinel.txt", target.name)

            with self.assertRaises(TargetError) as raised:
                prepare_review(paths, source, diff, marker)

            self.assertEqual(raised.exception.code, ErrorCode.POLICY)
            for target in (paths.workspace, paths.corpus, paths.rag_index):
                self.assertEqual((target / "sentinel.txt").read_text(encoding="utf-8"), target.name)
            self.assertFalse((paths.workspace / "pr.diff").exists())

    def test_rejects_credential_paths_from_every_diff_direction(self) -> None:
        cases = (
            (
                "deleted env",
                "diff --git a/.env b/.env\n"
                "deleted file mode 100644\n"
                "--- a/.env\n"
                "+++ /dev/null\n"
                "@@ -1 +0,0 @@\n-secret\n",
            ),
            (
                "rename away pem",
                "diff --git a/secrets/client.pem b/client.py\n"
                "similarity index 100%\n"
                "rename from secrets/client.pem\n"
                "rename to client.py\n",
            ),
            (
                "rename to key",
                "diff --git a/client.py b/secrets/client.key\n"
                "similarity index 100%\n"
                "rename from client.py\n"
                "rename to secrets/client.key\n",
            ),
            (
                "added env",
                "diff --git a/config/.env b/config/.env\n"
                "new file mode 100644\n"
                "index 0000000..e69de29\n",
            ),
        )
        for name, document in cases:
            with self.subTest(name=name), self.assertRaises(TargetError) as raised:
                parse_unified_diff(document)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._write(source, "app.py", "value = 1\n")
            diff = self._write(root, "credential.diff", cases[0][1])
            marker = self._write(root, "marker.json", "{}")
            paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
            self._write(paths.workspace, "sentinel.txt", "unchanged")
            with self.assertRaises(TargetError):
                prepare_review(paths, source, diff, marker)
            self.assertEqual((paths.workspace / "sentinel.txt").read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((paths.workspace / "pr.diff").exists())

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

    def test_rejects_oversized_preparation_inputs_without_path_read_bytes(self) -> None:
        for kind in ("diff", "file", "marker"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                source.mkdir()
                source_file = self._write(source, "app.py", "old\n")
                diff = self._write(root, "pr.diff", self._diff("app.py", "app.py"))
                marker = self._write(root, "marker.json", "{}")
                blocked = {"diff": diff, "file": source_file, "marker": marker}[kind]
                if kind == "diff":
                    diff.write_bytes(b"x" * (512 * 1024 + 1))
                elif kind == "file":
                    source_file.write_bytes(b"x" * (256 * 1024 + 1))
                else:
                    marker.write_bytes(b"x" * (16 * 1024 + 1))
                original_read_bytes = Path.read_bytes

                def guarded_read_bytes(path: Path) -> bytes:
                    if path == blocked:
                        raise AssertionError("oversized path must not use Path.read_bytes")
                    return original_read_bytes(path)

                paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
                with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=guarded_read_bytes):
                    with self.assertRaises(TargetError) as raised:
                        prepare_review(paths, source, diff, marker)
                self.assertEqual(raised.exception.code, ErrorCode.POLICY)

    def test_rejects_checkout_file_swapped_during_descriptor_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = self._write(source, "app.py", "old\n")
            original_inode = target.stat().st_ino
            diff = self._write(root, "pr.diff", self._diff("app.py", "app.py"))
            marker = self._write(root, "marker.json", "{}")
            paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
            self._write(paths.workspace, "sentinel.txt", "unchanged")
            real_read = os.read
            swapped = False

            def swap_during_read(descriptor: int, amount: int) -> bytes:
                nonlocal swapped
                if not swapped and os.fstat(descriptor).st_ino == original_inode:
                    swapped = True
                    target.rename(source / "original.py")
                    target.write_text("replacement\n", encoding="utf-8")
                return real_read(descriptor, amount)

            with mock.patch("aiweekend_target.lab.review_prepare.os.read", side_effect=swap_during_read):
                with self.assertRaises(TargetError) as raised:
                    prepare_review(paths, source, diff, marker)
            self.assertTrue(swapped)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)
            self.assertEqual((paths.workspace / "sentinel.txt").read_text(encoding="utf-8"), "unchanged")

    def test_walk_errors_fail_closed_before_target_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._write(source, "app.py", "old\n")
            diff = self._write(root, "pr.diff", self._diff("app.py", "app.py"))
            marker = self._write(root, "marker.json", "{}")
            paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
            self._write(paths.corpus, "sentinel.txt", "unchanged")

            def failing_walk(*_: object, onerror: object = None, **__: object) -> object:
                if callable(onerror):
                    onerror(PermissionError("sensitive traversal path"))
                return iter(())

            with mock.patch("aiweekend_target.lab.review_prepare.os.walk", side_effect=failing_walk):
                with self.assertRaises(TargetError) as raised:
                    prepare_review(paths, source, diff, marker)
            self.assertEqual(raised.exception.code, ErrorCode.POLICY)
            self.assertNotIn("sensitive", str(raised.exception))
            self.assertEqual((paths.corpus / "sentinel.txt").read_text(encoding="utf-8"), "unchanged")

    def test_rejects_overlapping_preparation_roots_before_staging(self) -> None:
        for kind in ("source equal", "source ancestor", "destination ancestor", "diff contained", "targets overlap", "symlink alias"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                source.mkdir()
                self._write(source, "app.py", "old\n")
                diff_parent = root / "inputs"
                diff = self._write(diff_parent, "pr.diff", self._diff("app.py", "app.py"))
                marker = self._write(root, "marker.json", "{}")
                paths = LabPaths(root / "workspace", root / "corpus", root / "rag-index")
                if kind == "source equal":
                    paths = LabPaths(source, paths.corpus, paths.rag_index)
                elif kind == "source ancestor":
                    paths = LabPaths(source / "generated", paths.corpus, paths.rag_index)
                elif kind == "destination ancestor":
                    paths = LabPaths(root, paths.corpus, paths.rag_index)
                elif kind == "diff contained":
                    paths = LabPaths(diff_parent, paths.corpus, paths.rag_index)
                elif kind == "targets overlap":
                    paths = LabPaths(root / "targets", root / "targets" / "corpus", paths.rag_index)
                else:
                    alias = root / "source-alias"
                    alias.symlink_to(source, target_is_directory=True)
                    paths = LabPaths(alias, paths.corpus, paths.rag_index)
                source_snapshot = {
                    item.relative_to(source).as_posix(): item.read_bytes()
                    for item in source.rglob("*")
                    if item.is_file()
                }
                diff_snapshot = diff.read_bytes()

                with mock.patch(
                    "aiweekend_target.lab.review_prepare.tempfile.mkdtemp",
                    side_effect=AssertionError("overlap must fail before staging"),
                ):
                    with self.assertRaises(TargetError) as raised:
                        prepare_review(paths, source, diff, marker)

                self.assertEqual(raised.exception.code, ErrorCode.POLICY)
                self.assertEqual(
                    {
                        item.relative_to(source).as_posix(): item.read_bytes()
                        for item in source.rglob("*")
                        if item.is_file()
                    },
                    source_snapshot,
                )
                self.assertEqual(diff.read_bytes(), diff_snapshot)

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

    def test_reports_unicode_character_column_for_direct_eval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "unicode.py", "é = 1; eval(value)\n")
            result = lint_pr(root, [{"path": "unicode.py", "added_lines": [1]}])
            self.assertEqual(result["diagnostics"][0]["column"], 8)

    def test_rejects_unsafe_missing_symlink_oversized_and_invalid_python_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            outside = root / "outside.py"
            outside.write_text("eval(value)\n", encoding="utf-8")
            self._write(corpus, "bad.py", "if True print('missing colon')\n")
            self._write(corpus, "non_utf8.py", b"\xff")
            (corpus / "link.py").symlink_to(outside)
            for target in (
                {"path": "/absolute.py", "added_lines": [1]},
                {"path": "../outside.py", "added_lines": [1]},
                {"path": "missing.py", "added_lines": [1]},
                {"path": "link.py", "added_lines": [1]},
                {"path": "non_utf8.py", "added_lines": [1]},
                {"path": "bad.py", "added_lines": [1]},
            ):
                with self.subTest(target=target), self.assertRaises(TargetError) as raised:
                    lint_pr(corpus, [target])
                self.assertIn(raised.exception.code, {ErrorCode.POLICY, ErrorCode.MCP})

    def test_rejects_an_oversized_target_before_reading_its_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self._write(root, "large.py", b"#" * (256 * 1024 + 1))
            with mock.patch.object(Path, "read_bytes", return_value=b""):
                with self.assertRaises(TargetError):
                    lint_pr(root, [{"path": target.name, "added_lines": [1]}])

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

    def test_lint_response_bounds_paths_positions_and_canonical_size(self) -> None:
        def diagnostic(path: str, line: int = 1, column: int = 1) -> dict[str, object]:
            return {
                "path": path,
                "line": line,
                "column": column,
                "rule": "ADLC001",
                "severity": "high",
                "message": "Avoid eval() on untrusted input",
            }

        edge = {"diagnostics": [diagnostic("a" + "é" * 254 + ".py", 262_144, 262_144)]}
        self.assertEqual(validate_lint_response(edge), edge)
        oversized = {"diagnostics": [diagnostic(f"{number:03d}-" + "x" * 2_620 + ".py") for number in range(100)]}
        self.assertGreater(
            len(json.dumps(oversized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            256 * 1024,
        )
        for invalid in (
            {"diagnostics": [diagnostic("aa" + "é" * 254 + ".py")]},
            {"diagnostics": [diagnostic("bad\npath.py")]},
            {"diagnostics": [diagnostic("bad\tpath.py")]},
            {"diagnostics": [diagnostic("bad\x7fpath.py")]},
            {"diagnostics": [diagnostic("good.py", 262_145, 1)]},
            {"diagnostics": [diagnostic("good.py", 1, 262_145)]},
            oversized,
        ):
            with self.subTest(path=invalid["diagnostics"][0]["path"][:24]), self.assertRaises(TargetError) as raised:
                validate_lint_response(invalid)
            self.assertEqual(raised.exception.code, ErrorCode.MCP)

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
            with mock.patch.dict("os.environ", {}, clear=False):
                os.environ.pop("ADLC_PR_REVIEW_MODE", None)
                self.assertEqual([tool.name for tool in anyio.run(create_server(database).list_tools)], ["search_repo"])
            with mock.patch.dict("os.environ", {"ADLC_PR_REVIEW_MODE": "0"}, clear=False):
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
            with mock.patch.dict("os.environ", {}, clear=False):
                os.environ.pop("ADLC_PR_REVIEW_MODE", None)
                self.assertEqual(anyio.run(health_check, database, marker, scenarios), {"status": "ready"})
            with mock.patch.dict("os.environ", {"ADLC_PR_REVIEW_MODE": "1"}, clear=False):
                self.assertEqual(anyio.run(health_check, database, marker, scenarios), {"status": "ready"})


class _ReviewResponse:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self.text = json.dumps(body)
        self.status_code = status_code


class _ReviewSession:
    def __init__(
        self,
        order: list[str],
        search: object,
        lint: object,
        *,
        tools: tuple[str, ...] = ("search_repo", "lint_pr"),
    ) -> None:
        self.order = order
        self.search = search
        self.lint = lint
        self.tools = tools
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> object:
        return SimpleNamespace(
            result_type="complete",
            next_cursor=None,
            tools=[SimpleNamespace(name=name) for name in self.tools],
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.order.append(name)
        self.calls.append((name, arguments))
        value = self.search if name == "search_repo" else self.lint
        return SimpleNamespace(result_type="complete", is_error=False, structured_content=value)


class PullRequestReviewLoopTests(unittest.TestCase):
    def _review_api(self) -> tuple[object, object]:
        try:
            from aiweekend_target.pr_review import ReviewPaths, _run
        except ModuleNotFoundError:
            self.fail("the separate pr_review loop is absent")
        return ReviewPaths, _run

    def _diff(self) -> str:
        return (
            "diff --git a/pkg/check.py b/pkg/check.py\n"
            "--- a/pkg/check.py\n"
            "+++ b/pkg/check.py\n"
            "@@ -1 +1,2 @@\n"
            "-old\n"
            "+safe = 1\n"
            "+danger = eval(source)  # hf_diff_secret ADLC_CANARY_RAG_7A91C4\n"
            "diff --git a/docs/readme.md b/docs/readme.md\n"
            "--- a/docs/readme.md\n"
            "+++ b/docs/readme.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+documentation\n"
            "diff --git a/removed.py b/removed.py\n"
            "--- a/removed.py\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-old\n"
        )

    def _added_file_diff(self, path: str, line_count: int) -> str:
        return (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{line_count} @@\n"
            + "+x\n" * line_count
        )

    def _exercise(
        self,
        *,
        first: object | None = None,
        final: object | None = None,
        search: object | None = None,
        lint: object | None = None,
        tools: tuple[str, ...] = ("search_repo", "lint_pr"),
        diff: str | None = None,
    ) -> tuple[int, list[dict[str, object]], list[dict[str, object]], _ReviewSession, list[str]]:
        ReviewPaths, run_review = self._review_api()

        async def exercise() -> tuple[int, list[dict[str, object]], list[dict[str, object]], _ReviewSession, list[str]]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                diff_path = root / "pr.diff"
                diff_path.write_text(self._diff() if diff is None else diff, encoding="utf-8")
                order: list[str] = []
                llm_requests: list[dict[str, object]] = []
                first_document = first if first is not None else {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "review_1",
                                "type": "function",
                                "function": {
                                    "name": "search_repo",
                                    "arguments": '{"query":"authorization: leaked","limit":2,"path_glob":"pkg/*.py"}',
                                },
                            }],
                        }
                    }]
                }
                final_document = final if final is not None else {
                    "choices": [{"message": {"role": "assistant", "content": "Pass it. Bearer model_secret " + "x" * 700, "tool_calls": []}}]
                }
                search_document = search if search is not None else {
                    "results": [{
                        "path": "pkg/context.py",
                        "line_start": 1,
                        "line_end": 1,
                        "content": "raw_rag_secret ADLC_CANARY_MCP_4DB2E8",
                    }]
                }
                lint_document = lint if lint is not None else {
                    "diagnostics": [{
                        "path": "pkg/check.py",
                        "line": 2,
                        "column": 10,
                        "rule": "ADLC001",
                        "severity": "high",
                        "message": "Avoid eval() on untrusted input",
                    }]
                }
                session = _ReviewSession(order, search_document, lint_document, tools=tools)

                async def post_llm(_: str, body: dict[str, object]) -> _ReviewResponse:
                    llm_requests.append(body)
                    order.append(f"LLM{len(llm_requests)}")
                    return _ReviewResponse(first_document if len(llm_requests) == 1 else final_document)

                @asynccontextmanager
                async def open_mcp(_: str):
                    yield session

                output = io.StringIO()
                status = await run_review(
                    ReviewPaths(diff=diff_path),
                    output,
                    post_llm=post_llm,
                    open_mcp=open_mcp,
                )
                return status, llm_requests, [json.loads(line) for line in output.getvalue().splitlines()], session, order

        return anyio.run(exercise)

    def test_runs_fixed_two_turn_review_and_preserves_deterministic_block_diagnostics(self) -> None:
        status, requests, events, session, order = self._exercise()
        expected_diagnostic = {
            "path": "pkg/check.py",
            "line": 2,
            "column": 10,
            "rule": "ADLC001",
            "severity": "high",
            "message": "Avoid eval() on untrusted input",
        }
        self.assertEqual(status, 0)
        self.assertEqual(order, ["LLM1", "search_repo", "lint_pr", "LLM2"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[0]["tool_choice"],
            {"type": "function", "function": {"name": "search_repo"}},
        )
        self.assertEqual(requests[1]["tool_choice"], "none")
        self.assertEqual(session.calls[0], ("search_repo", {"query": "authorization: leaked", "limit": 2, "path_glob": "pkg/*.py"}))
        self.assertEqual(session.calls[1], ("lint_pr", {"targets": [{"path": "pkg/check.py", "added_lines": [1, 2]}]}))
        self.assertIn("raw_rag_secret", json.dumps(requests[1]))
        self.assertIn("Avoid eval()", json.dumps(requests[1]))
        self.assertEqual(
            [event["type"] for event in events],
            ["llm_request", "llm_response", "mcp_request", "mcp_result", "mcp_request", "mcp_result", "llm_request", "llm_response", "pr_review_result"],
        )
        self.assertEqual(len([event for event in events if event["type"] == "llm_request"]), 2)
        self.assertEqual(len([event for event in events if event["type"] == "mcp_request"]), 2)
        self.assertEqual(len([event for event in events if event["type"] == "mcp_result"]), 2)
        self.assertEqual(
            events[-1],
            {
                "schema": 1,
                "type": "pr_review_result",
                "ok": True,
                "verdict": "block",
                "diagnostics": [expected_diagnostic],
                "report_preview": events[-2]["report_preview"],
            },
        )
        serialized = "\n".join(json.dumps(event) for event in events)
        for secret in ("hf_diff_secret", "raw_rag_secret", "model_secret", "ADLC_CANARY"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("authorization: leaked", serialized)
        self.assertLessEqual(len(events[-1]["report_preview"]), 512)

    def test_empty_validated_diagnostics_pass_regardless_of_model_wording(self) -> None:
        final = {"choices": [{"message": {"role": "assistant", "content": "BLOCK this change", "tool_calls": []}}]}
        status, _, events, _, _ = self._exercise(lint={"diagnostics": []}, final=final)
        self.assertEqual(status, 0)
        self.assertEqual(events[-1]["verdict"], "pass")
        self.assertEqual(events[-1]["diagnostics"], [])

    def test_rejects_wrong_review_tool_surface_before_any_mcp_call(self) -> None:
        status, requests, events, session, order = self._exercise(tools=("search_repo",))
        self.assertEqual(status, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(session.calls, [])
        self.assertEqual(order, ["LLM1"])
        self.assertEqual(events[-1], {"schema": 1, "type": "pr_review_error", "ok": False, "code": "MCP", "stage": "mcp"})

    def test_malformed_peer_responses_stop_without_retries_or_fallbacks(self) -> None:
        cases = (
            ("first", {}, None, None, 1, []),
            ("search", None, {"results": [{"path": "/bad.py", "line_start": 1, "line_end": 1, "content": "x"}]}, None, 1, ["search_repo"]),
            ("lint", None, None, {"diagnostics": [{"path": "bad.py"}]}, 1, ["search_repo", "lint_pr"]),
            ("final", None, None, None, 2, ["search_repo", "lint_pr"]),
        )
        for name, first, search, lint, llm_count, mcp_names in cases:
            with self.subTest(name=name):
                final = {"choices": []} if name == "final" else None
                status, requests, events, session, _ = self._exercise(first=first, search=search, lint=lint, final=final)
                self.assertEqual(status, 1)
                self.assertEqual(len(requests), llm_count)
                self.assertEqual([call[0] for call in session.calls], mcp_names)
                self.assertEqual(events[-1]["type"], "pr_review_error")
                self.assertNotIn("choices", json.dumps(events))

    def test_default_path_and_cli_dispatch_are_separate_from_agent(self) -> None:
        ReviewPaths, _ = self._review_api()
        self.assertEqual(ReviewPaths().diff, Path("/target/workspace/pr.diff"))
        from aiweekend_target import __main__

        output = io.StringIO()
        with mock.patch.object(__main__, "run_pr_review", return_value=0) as run:
            self.assertEqual(__main__.main(["pr-review"], output=output), 0)
        run.assert_called_once_with(output=output)

    def test_credential_diff_is_rejected_before_external_review_boundaries(self) -> None:
        document = (
            "diff --git a/secrets/client.pem b/pkg/client.py\n"
            "similarity index 100%\n"
            "rename from secrets/client.pem\n"
            "rename to pkg/client.py\n"
        )
        status, requests, events, session, order = self._exercise(diff=document)
        self.assertEqual(status, 1)
        self.assertEqual(requests, [])
        self.assertEqual(session.calls, [])
        self.assertEqual(order, [])
        self.assertEqual(
            events,
            [{"schema": 1, "type": "pr_review_error", "ok": False, "code": "POLICY", "stage": "diff"}],
        )

    def test_lint_preflight_accepts_exact_caps_and_rejects_overages_before_boundaries(self) -> None:
        exactly_100_targets = "".join(self._added_file_diff(f"pkg/{number}.py", 1) for number in range(100))
        exactly_10_000_lines = self._added_file_diff("large.py", 10_000)
        for name, document, target_count, line_count in (
            ("target edge", exactly_100_targets, 100, 100),
            ("line edge", exactly_10_000_lines, 1, 10_000),
        ):
            with self.subTest(name=name):
                status, requests, events, session, order = self._exercise(diff=document, lint={"diagnostics": []})
                self.assertEqual(status, 0)
                self.assertEqual(len(requests), 2)
                self.assertEqual(order, ["LLM1", "search_repo", "lint_pr", "LLM2"])
                lint_arguments = session.calls[1][1]
                self.assertEqual(len(lint_arguments["targets"]), target_count)
                self.assertEqual(sum(len(target["added_lines"]) for target in lint_arguments["targets"]), line_count)
                self.assertEqual(events[-1]["verdict"], "pass")

        over_100_targets = "".join(self._added_file_diff(f"pkg/{number}.py", 1) for number in range(101))
        over_10_000_lines = self._added_file_diff("large.py", 10_001)
        for name, document in (("target overage", over_100_targets), ("line overage", over_10_000_lines)):
            with self.subTest(name=name):
                status, requests, events, session, order = self._exercise(diff=document)
                self.assertEqual(status, 1)
                self.assertEqual(requests, [])
                self.assertEqual(session.calls, [])
                self.assertEqual(order, [])
                self.assertEqual(events, [{"schema": 1, "type": "pr_review_error", "ok": False, "code": "POLICY", "stage": "diff"}])

    def test_oversized_diff_is_rejected_before_any_file_read_or_boundary_call(self) -> None:
        ReviewPaths, run_review = self._review_api()

        async def exercise() -> tuple[int, list[dict[str, object]], list[str]]:
            with tempfile.TemporaryDirectory() as directory:
                diff_path = Path(directory) / "oversized.diff"
                diff_path.write_bytes(b"x" * (512 * 1024 + 1))
                calls: list[str] = []

                async def post_llm(_: str, __: dict[str, object]) -> object:
                    calls.append("llm")
                    raise AssertionError("LLM must not be called")

                @asynccontextmanager
                async def open_mcp(_: str):
                    calls.append("mcp")
                    raise AssertionError("MCP must not be opened")
                    yield

                output = io.StringIO()
                with (
                    mock.patch.object(Path, "read_text", side_effect=AssertionError("oversized diff was read")),
                    mock.patch.object(Path, "read_bytes", side_effect=AssertionError("oversized diff was read")),
                ):
                    status = await run_review(ReviewPaths(diff=diff_path), output, post_llm=post_llm, open_mcp=open_mcp)
                return status, [json.loads(line) for line in output.getvalue().splitlines()], calls

        status, events, calls = anyio.run(exercise)
        self.assertEqual(status, 1)
        self.assertEqual(calls, [])
        self.assertEqual(events, [{"schema": 1, "type": "pr_review_error", "ok": False, "code": "POLICY", "stage": "diff"}])

    def test_symlink_and_non_utf8_diffs_fail_closed_before_boundary_calls(self) -> None:
        ReviewPaths, run_review = self._review_api()
        for name in ("symlink", "non_utf8"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "target.diff"
                target.write_bytes(self._diff().encode("utf-8") if name == "symlink" else b"\xff")
                diff_path = root / "review.diff"
                if name == "symlink":
                    diff_path.symlink_to(target)
                else:
                    diff_path = target
                calls: list[str] = []

                async def post_llm(_: str, __: dict[str, object]) -> object:
                    calls.append("llm")
                    raise AssertionError("LLM must not be called")

                @asynccontextmanager
                async def open_mcp(_: str):
                    calls.append("mcp")
                    raise AssertionError("MCP must not be opened")
                    yield

                output = io.StringIO()
                async def exercise() -> int:
                    return await run_review(
                        ReviewPaths(diff=diff_path),
                        output,
                        post_llm=post_llm,
                        open_mcp=open_mcp,
                    )

                status = anyio.run(exercise)
                self.assertEqual(status, 1)
                self.assertEqual(calls, [])
                self.assertEqual(
                    [json.loads(line) for line in output.getvalue().splitlines()],
                    [{"schema": 1, "type": "pr_review_error", "ok": False, "code": "POLICY", "stage": "diff"}],
                )
