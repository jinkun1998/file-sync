from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from file_sync import cli


class SyncSafetyTests(unittest.TestCase):
    def config(self) -> cli.Config:
        return cli.Config("storage", "user", "/srv/storage", 0, 0, (), (), ("*.tmp",))

    def test_remote_path_rejects_escape_and_reserved_state(self) -> None:
        for value in ("", "/srv/storage", "../escape", "a/../b", ".file-sync-state/x", "models/.git"):
            with self.assertRaises(cli.SyncError):
                cli.safe_remote_path(value)
        self.assertEqual(cli.safe_remote_path("models/2026"), "models/2026")

    def test_target_preserves_client_basename_for_absolute_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root / "models" / "anyka" / "2026_model"
            client.mkdir(parents=True)
            absolute = cli.make_target(str(client), "models/anyka", self.config())
            self.assertEqual(absolute.remote, "models/anyka/2026_model")
            previous = Path.cwd()
            os.chdir(root)
            try:
                relative = cli.make_target("models/anyka/2026_model", "models/anyka", self.config())
            finally:
                os.chdir(previous)
            self.assertEqual(relative, absolute)

    def test_baseline_key_uses_full_server_destination(self) -> None:
        config = self.config()
        first = cli.Target(Path("/tmp/a"), "anyka/a", ())
        second = cli.Target(Path("/tmp/a"), "apical/a", ())
        self.assertNotEqual(cli.state_path(config, first), cli.state_path(config, second))

    def test_estimate_uses_dynamic_decimal_units(self) -> None:
        self.assertEqual(cli.format_size(999), "999 bytes")
        self.assertEqual(cli.format_size(1_234), "1.23 KB")
        self.assertEqual(cli.format_size(1_234_567), "1.23 MB")
        self.assertEqual(cli.format_size(1_101_698_401), "1.10 GB")

    def test_rsync_reports_state_without_a_terminal(self) -> None:
        process = unittest.mock.Mock()
        process.returncode = 0
        process.communicate.return_value = ("copied", "")
        output = io.StringIO()
        with patch.object(cli.subprocess, "Popen", return_value=process), patch.object(cli.sys.stderr, "isatty", return_value=False), contextlib.redirect_stderr(output):
            result = cli.run_rsync(["rsync"], "push")
        self.assertEqual(result.stdout, "copied")
        self.assertIn("push: syncing", output.getvalue())

    def test_excludes_match_directory_and_basename(self) -> None:
        patterns = ("__pycache__/", "*.tmp")
        self.assertTrue(cli.excluded("pkg/__pycache__/a.pyc", patterns))
        self.assertTrue(cli.excluded("build/output.tmp", patterns))
        self.assertFalse(cli.excluded("pkg/code.py", patterns))

    def test_first_sync_existing_difference_conflicts(self) -> None:
        copies, conflicts, skipped = cli.classify({"A.txt": "F:1:1"}, {"A.txt": "F:2:1"}, None)
        self.assertEqual(copies, [])
        self.assertEqual(conflicts, ["A.txt"])
        self.assertEqual(skipped, 0)

    def test_two_sided_edit_conflicts_without_overwrite(self) -> None:
        baseline = {"A.txt": "F:1:1"}
        copies, conflicts, _ = cli.classify({"A.txt": "F:2:2"}, {"A.txt": "F:3:2"}, baseline)
        self.assertEqual(copies, [])
        self.assertEqual(conflicts, ["A.txt"])

    def test_local_deletion_does_not_schedule_remote_deletion(self) -> None:
        baseline = {"A.txt": "F:1:1", "B.txt": "F:1:1"}
        copies, conflicts, skipped = cli.classify({"A.txt": "F:1:1"}, dict(baseline), baseline)
        self.assertEqual(copies, [])
        self.assertEqual(conflicts, [])
        self.assertEqual(skipped, 1)
        self.assertIn("B.txt", baseline)

    def test_git_metadata_is_forced_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("private", encoding="utf-8")
            (root / "model.bin").write_text("model", encoding="utf-8")
            target = cli.make_target(str(root), "models", self.config())
            self.assertEqual(set(cli.local_manifest(root, target.excludes)), {"model.bin"})

    def test_nested_git_folder_derives_server_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "anyka"
            source = root / "2026_model"
            source.mkdir(parents=True)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "git@git.fpt.net:iot/anyka.git"], check=True)
            target = cli.make_target(str(source), "models/anyka", self.config())
            _, remote_root, origin, branch = cli.local_repository(target) or (None, None, None, None)
            self.assertEqual(remote_root, "models/anyka")
            self.assertEqual(origin, "git@git.fpt.net:iot/anyka.git")
            self.assertEqual(branch, "main")

    def test_registry_script_is_deduplicated_and_order_preserving(self) -> None:
        self.assertIn('grep -Fqx -- "$repo" "$registry" || printf', cli.REPOSITORY_SCRIPT)

    def test_git_clone_fallback_allows_data_sync_without_backup(self) -> None:
        target = cli.Target(Path("/tmp/anyka"), "models/anyka", ())
        result = subprocess.CompletedProcess([], 0, "fallback\n", "connection timed out")
        output = io.StringIO()
        with patch.object(cli, "local_repository", return_value=(Path("/tmp/anyka"), "models/anyka", "git@git.fpt.net:anyka.git", "main")), patch.object(cli, "run_remote", return_value=result), contextlib.redirect_stderr(output):
            self.assertFalse(cli.prepare_server_repository(self.config(), target))
        self.assertIn("syncing data without Server Git backup", output.getvalue())

    def test_existing_non_git_repository_root_is_marked_unmanaged(self) -> None:
        self.assertIn('grep -Fqx -- "$repo" "$fallback" || printf', cli.REPOSITORY_SCRIPT)

    def test_log_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, "STATE", Path(directory)):
            path = cli.log_operation("push-models", "exit=0")
            self.assertEqual(path.read_text(encoding="utf-8"), "exit=0")

    def test_invalid_config_returns_safe_exit_code(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = cli.main(["--config", "/not/a/config.toml", "config", "check"])
        self.assertEqual(code, 2)
        self.assertIn("config not found", output.getvalue())


class BackupTests(unittest.TestCase):
    def test_backup_continues_after_failure_and_skips_unchanged_repo(self) -> None:
        script = Path(__file__).resolve().parents[1] / "server" / "backup.sh"
        with tempfile.TemporaryDirectory() as directory:
            storage = Path(directory) / "storage"
            repository = storage / "anyka"
            origin = Path(directory) / "origin.git"
            (storage / ".file-sync-state").mkdir(parents=True)
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repository), "remote", "add", "origin", str(origin)], check=True)
            (repository / "model.bin").write_text("model", encoding="utf-8")
            registry = storage / ".file-sync-state" / "repositories"
            registry.write_text(f"{storage / 'missing'}\n{repository}\n", encoding="utf-8")
            environment = {**os.environ, "STORAGE_ROOT": str(storage), "PUSH_RETRIES": "0"}
            first = subprocess.run(["bash", str(script)], text=True, capture_output=True, env=environment)
            self.assertEqual(first.returncode, 1)
            self.assertIn("backup committed and pushed", first.stdout)
            self.assertIn("Git repository missing", first.stdout)
            self.assertEqual(subprocess.run(["git", "-C", str(origin), "rev-parse", "main"], text=True, capture_output=True).returncode, 0)
            registry.write_text(f"{repository}\n", encoding="utf-8")
            second = subprocess.run(["bash", str(script)], text=True, capture_output=True, env=environment)
            self.assertEqual(second.returncode, 0)
            self.assertIn("unchanged", second.stdout)

    def test_timer_is_monotonic_18_hours(self) -> None:
        timer = (Path(__file__).resolve().parents[1] / "server" / "storage-backup.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=18h", timer)
        self.assertNotIn("OnCalendar", timer)


if __name__ == "__main__":
    unittest.main()
