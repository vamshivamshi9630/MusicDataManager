import unittest
from unittest.mock import MagicMock, patch
from backend.services.git_sync import BaseGitSyncService, GitSyncError
from backend.core.config import settings

class TestGitPushTimeoutResilience(unittest.TestCase):

    def setUp(self):
        self.mock_repo = MagicMock()
        self.mock_repo.root = "/mock/repo/root"
        self.svc = BaseGitSyncService(self.mock_repo)

    @patch.object(BaseGitSyncService, "_run_git")
    def test_01_push_succeeds_normally(self, mock_run_git):
        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if cmd == "rev-parse --is-inside-work-tree":
                return "true"
            elif cmd == "add .":
                return ""
            elif cmd == "status --porcelain":
                return "M  metadata/statistics.json"
            elif cmd == "commit -m added songs/album with TestAlbum":
                return "[main 1234567] added songs/album with TestAlbum"
            elif cmd == "rev-parse HEAD":
                return "1234567890abcdef"
            elif "push" in cmd:
                return "Everything up-to-date"
            return ""

        mock_run_git.side_effect = side_effect

        res = self.svc.stage_commit_and_push("TestAlbum", mode="add")
        self.assertTrue(res["committed"])
        self.assertTrue(res["pushed"])
        self.assertEqual(res["commit_sha"], "1234567890ab")

    @patch.object(BaseGitSyncService, "get_remote_head_sha")
    @patch.object(BaseGitSyncService, "_run_git")
    def test_02_push_times_out_but_remote_commit_matches(self, mock_run_git, mock_remote_head):
        target_sha = "abcd1234efgh"

        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if cmd == "rev-parse --is-inside-work-tree":
                return "true"
            elif cmd == "add .":
                return ""
            elif cmd == "status --porcelain":
                return "M  metadata/statistics.json"
            elif cmd == "commit -m added songs/album with TestAlbum":
                return "[main abcd123] added songs/album with TestAlbum"
            elif cmd == "rev-parse HEAD":
                return target_sha
            elif "push" in cmd:
                raise GitSyncError("Git command 'git push' timed out after 180 seconds.", stage="git_push", exit_code=124)
            return ""

        mock_run_git.side_effect = side_effect
        mock_remote_head.return_value = target_sha

        res = self.svc.stage_commit_and_push("TestAlbum", mode="add")
        self.assertTrue(res["committed"])
        self.assertTrue(res["pushed"])
        self.assertIn("Verified via remote HEAD check", res["push_log"])

    @patch.object(BaseGitSyncService, "get_remote_head_sha")
    @patch.object(BaseGitSyncService, "_run_git")
    def test_03_push_times_out_and_remote_commit_mismatches(self, mock_run_git, mock_remote_head):
        local_sha = "abcd1234efgh"
        different_remote_sha = "999999999999"

        def side_effect(args, **kwargs):
            cmd = " ".join(args)
            if cmd == "rev-parse --is-inside-work-tree":
                return "true"
            elif cmd == "add .":
                return ""
            elif cmd == "status --porcelain":
                return "M  metadata/statistics.json"
            elif cmd == "commit -m added songs/album with TestAlbum":
                return "[main abcd123] added songs/album with TestAlbum"
            elif cmd == "rev-parse HEAD":
                return local_sha
            elif "push" in cmd:
                raise GitSyncError("Git command 'git push' timed out after 180 seconds.", stage="git_push", exit_code=124)
            return ""

        mock_run_git.side_effect = side_effect
        mock_remote_head.return_value = different_remote_sha

        with self.assertRaises(GitSyncError):
            self.svc.stage_commit_and_push("TestAlbum", mode="add")

    def test_04_configurable_push_timeout_setting(self):
        self.assertGreaterEqual(settings.GIT_PUSH_TIMEOUT_SECONDS, 60)
        self.assertGreaterEqual(settings.GIT_CLONE_TIMEOUT_SECONDS, 30)

if __name__ == "__main__":
    unittest.main()
