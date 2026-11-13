import os
import json
import time
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch

import pytest

from backend.services.git_sync import (
    BaseGitSyncService,
    GitSyncError,
    get_repository_lock,
    RepositoryFileLock,
    is_git_process_running_in_worktree,
)
from backend.core.repository import PathTraversalError, IRepositoryProvider
from backend.services.generator import GeneratorService, GeneratorValidationError
from backend.services.validation import (
    ValidationService,
    FileValidationError,
    MAX_AUDIO_FILE_SIZE_BYTES,
    MAX_IMAGE_FILE_SIZE_BYTES,
)
from backend.services.latest_releases.musicdata_uploader import (
    MusicDataSyncOrchestrator,
    STATUS_WAITING,
    STATUS_DOWNLOADING,
    STATUS_VALIDATING,
    STATUS_UPLOADING,
    STATUS_ADDED,
    STATUS_SKIPPED,
    STATUS_FAILED,
    STATUS_PARTIALLY_FAILED,
    TERMINAL_STATUSES,
)
from backend.services.latest_releases.models import NotificationType, NotificationResolutionStatus
from backend.services.latest_releases.db import LatestReleasesDB


class TestBulkUploadFailureRepairs:
    """
    Comprehensive regression suite for bulk upload failure investigation and repairs:
    - Git Concurrency & Process Lock
    - Git index.lock stale cleanup and recovery
    - Explicit path staging avoiding slow 'git add .' tree traversal
    - Deletion shield baseline accuracy & sparse scanner cache merging
    - Genuine deletion shield enforcement
    - Progress accounting and terminal state guarantees (no stuck 92%)
    - Oversized files (up to 95 MB allowed, >95 MB cleanly rejected)
    - Retry idempotency (retrying only failed songs)
    """

    def test_01_repo_lock_serializes_concurrent_git_operations(self, tmp_path):
        repo_mock = MagicMock()
        repo_mock.root = tmp_path

        lock1 = get_repository_lock(tmp_path)
        lock2 = get_repository_lock(tmp_path)
        assert lock1 is lock2  # Same lock instance per path

        sync1 = BaseGitSyncService(repo_mock)
        sync2 = BaseGitSyncService(repo_mock)
        assert sync1._repo_lock is sync2._repo_lock

        # Verify mutual exclusion
        execution_order = []

        def worker(worker_id: int):
            with sync1._repo_lock:
                execution_order.append(f"start_{worker_id}")
                time.sleep(0.05)
                execution_order.append(f"end_{worker_id}")

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Since lock is mutually exclusive, start_1 must end_1 before start_2, or vice versa
        assert execution_order in (
            ["start_1", "end_1", "start_2", "end_2"],
            ["start_2", "end_2", "start_1", "end_1"],
        )

    def test_02_stale_index_lock_cleanup(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir(parents=True)
        index_lock = git_dir / "index.lock"

        repo_mock = MagicMock()
        repo_mock.root = tmp_path
        svc = BaseGitSyncService(repo_mock)

        # 1. Fresh lock (age < 15s) should NOT be deleted
        index_lock.write_text("dummy lock content")
        svc._clean_stale_index_lock()
        assert index_lock.exists()

        # 2. Stale lock (age > 15s) SHOULD be automatically cleaned up
        old_mtime = time.time() - 30
        os.utime(index_lock, (old_mtime, old_mtime))
        svc._clean_stale_index_lock()
        assert not index_lock.exists()

    def test_03_explicit_staging_avoids_git_add_dot(self, tmp_path):
        repo_mock = MagicMock()
        repo_mock.root = tmp_path
        repo_mock.validate_safe_filename.side_effect = lambda x: IRepositoryProvider.validate_safe_filename(None, x)
        (tmp_path / "metadata").mkdir(parents=True)
        (tmp_path / "songs_with_details.json").write_text("[]")

        svc = BaseGitSyncService(repo_mock)
        captured_git_args = []

        def fake_run_git(args, check=True, timeout=60):
            captured_git_args.append(args)
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args == ["status", "--porcelain"]:
                return "M  metadata/statistics.json"
            if args == ["rev-parse", "HEAD"]:
                return "abc123def456"
            return ""

        svc._run_git = fake_run_git
        res = svc.stage_commit_and_push(
            album_name="Aakasamlo Oka Tara",
            mode="edit",
            custom_commit_msg="Add missing song to Aakasamlo Oka Tara",
            push_enabled=False,
        )

        assert res["committed"] is True
        assert res["commit_sha"] == "abc123def456"

        # Verify git add received explicit paths, NEVER 'git add .'
        add_commands = [cmd for cmd in captured_git_args if cmd and cmd[0] == "add"]
        assert len(add_commands) >= 1
        staged_targets = add_commands[0][1:]
        assert "." not in staged_targets
        assert "Aakasamlo Oka Tara" in staged_targets
        assert "metadata" in staged_targets
        assert "songs_with_details.json" in staged_targets

    def test_04_deletion_shield_sparse_scanner_merges_cached_songs(self, tmp_path):
        # Setup mock MusicData tree
        generator_dir = tmp_path / "generator"
        generator_dir.mkdir(parents=True)
        (generator_dir / "__init__.py").write_text("")
        (generator_dir / "config.py").write_text(
            f"from pathlib import Path\nBASE_PATH = Path(r'{tmp_path}')\nAUDIO_EXTENSIONS = ['.mp3']\nIMAGE_EXTENSIONS = ['.png']\n"
        )
        (generator_dir / "scanner.py").write_text("# placeholder")

        meta_albums_dir = tmp_path / "metadata" / "albums" / "A"
        meta_albums_dir.mkdir(parents=True)
        cached_album = {
            "album": {"name": "Aakasamlo Oka Tara", "year": 2026, "language": "Telugu", "image": "Aakasamlo Oka Tara.png"},
            "songs": [
                {"title": "Chitti Chitti Cheema Song", "audio": "Chitti Chitti Cheema Song.mp3"},
                {"title": "Song Two", "audio": "Song Two.mp3"},
                {"title": "Song Three", "audio": "Song Three.mp3"},
            ]
        }
        (meta_albums_dir / "Aakasamlo Oka Tara.json").write_text(json.dumps(cached_album))

        # On-disk directory simulates sparse checkout with ONLY 1 newly added song
        album_disk = tmp_path / "Aakasamlo Oka Tara"
        album_disk.mkdir(parents=True)
        (album_disk / "album_info.json").write_text(json.dumps({"musicDirector": "GV Prakash", "year": 2026}))
        (album_disk / "Aakasamlo Oka Tara.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (album_disk / "Radhe Radhe Song.mp3").write_bytes(b"dummy mp3 content")

        repo_mock = MagicMock()
        repo_mock.root = tmp_path
        repo_mock.metadata_dir = tmp_path / "metadata"
        repo_mock.albums_dir = tmp_path / "metadata" / "albums"
        repo_mock.indexes_dir = tmp_path / "metadata" / "indexes"

        gen_svc = GeneratorService(repo_mock)
        gen_svc._ensure_sparse_scanner_support()

        # Execute scanner in isolated namespace
        import sys
        import importlib.util
        sys.path.insert(0, str(tmp_path))
        try:
            spec = importlib.util.spec_from_file_location("generator.scanner", str(generator_dir / "scanner.py"))
            scanner_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scanner_mod)

            scanned = scanner_mod.scan_all_albums()
        finally:
            if str(tmp_path) in sys.path:
                sys.path.remove(str(tmp_path))
        assert len(scanned) == 1
        alb = scanned[0]
        assert alb["album_name"] == "Aakasamlo Oka Tara"
        # Must have ALL 4 songs: 3 existing cached + 1 new song on disk!
        scanned_filenames = [f.name for f in alb["mp3_files"]]
        assert len(scanned_filenames) == 4
        assert "Chitti Chitti Cheema Song.mp3" in scanned_filenames
        assert "Song Two.mp3" in scanned_filenames
        assert "Song Three.mp3" in scanned_filenames
        assert "Radhe Radhe Song.mp3" in scanned_filenames

    def test_05_deletion_shield_triggers_on_real_reduction(self, tmp_path):
        repo_mock = MagicMock()
        repo_mock.root = tmp_path
        repo_mock.metadata_dir = tmp_path / "metadata"
        repo_mock.indexes_dir = tmp_path / "metadata" / "indexes"
        repo_mock.albums_dir = tmp_path / "metadata" / "albums"

        gen_svc = GeneratorService(repo_mock)
        # Mock metrics to simulate real song reduction (e.g. 4288 down to 4284)
        call_count = 0

        def fake_metrics():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"total_albums": 688, "total_songs": 4288}
            return {"total_albums": 688, "total_songs": 4284}

        gen_svc.get_current_metrics = fake_metrics
        gen_svc._validate_generated_catalogs = MagicMock()

        # Create dummy generate_metadata.py script
        script1 = tmp_path / "generate_metadata.py"
        script1.write_text("import sys; sys.exit(0)")

        with pytest.raises(GeneratorValidationError) as exc:
            gen_svc.run_generator_pipeline()

        assert "UNEXPECTED DELETION SHIELD TRIGGERED" in str(exc.value)
        assert "4288 to 4284" in str(exc.value)

    def test_06_get_current_metrics_authoritative_hierarchy(self, tmp_path):
        meta_dir = tmp_path / "metadata"
        idx_dir = meta_dir / "indexes"
        albums_dir = meta_dir / "albums" / "T"
        meta_dir.mkdir(parents=True)
        idx_dir.mkdir(parents=True)
        albums_dir.mkdir(parents=True)

        repo_mock = MagicMock()
        repo_mock.root = tmp_path
        repo_mock.metadata_dir = meta_dir
        repo_mock.indexes_dir = idx_dir
        repo_mock.albums_dir = meta_dir / "albums"

        gen_svc = GeneratorService(repo_mock)

        # 1. Tier 1: statistics.json
        (meta_dir / "statistics.json").write_text(json.dumps({"totalAlbums": 688, "totalSongs": 4283}))
        m1 = gen_svc.get_current_metrics()
        assert m1["total_albums"] == 688
        assert m1["total_songs"] == 4283

        # 2. Tier 2: indexes/albums.json (when statistics.json is missing)
        (meta_dir / "statistics.json").unlink()
        (idx_dir / "albums.json").write_text(json.dumps([{"name": "Alb1", "songCount": 5}, {"name": "Alb2", "songCount": 3}]))
        m2 = gen_svc.get_current_metrics()
        assert m2["total_albums"] == 2
        assert m2["total_songs"] == 8

        # 3. Tier 3: metadata/albums/*/*.json (when index is also missing)
        (idx_dir / "albums.json").unlink()
        (albums_dir / "Tyson.json").write_text(json.dumps({"songs": [{"title": "S1"}, {"title": "S2"}]}))
        m3 = gen_svc.get_current_metrics()
        assert m3["total_albums"] == 1
        assert m3["total_songs"] == 2

    def test_07_progress_accounting_percentage_and_terminal_states(self, tmp_path):
        db_mock = MagicMock()
        orch = MusicDataSyncOrchestrator(db=db_mock)

        albums = [
            {
                "album_name": "Alb1",
                "status": STATUS_ADDED,
                "songs": [{"song_title": "S1", "status": STATUS_ADDED}, {"song_title": "S2", "status": STATUS_ADDED}],
            },
            {
                "album_name": "Alb2",
                "status": STATUS_PARTIALLY_FAILED,
                "songs": [{"song_title": "S3", "status": STATUS_SKIPPED}, {"song_title": "S4", "status": STATUS_FAILED}],
            },
            {
                "album_name": "Alb3",
                "status": STATUS_WAITING,
                "songs": [{"song_title": "S5", "status": STATUS_WAITING}, {"song_title": "S6", "status": STATUS_WAITING}],
            },
        ]

        # 4 out of 6 songs are done (2 added + 1 skipped + 1 failed = 4). 4/6 = 66%
        p = orch._refresh_progress("job_123", albums, STATUS_UPLOADING, "Processing")
        assert p["total_songs"] == 6
        assert p["songs_added"] == 2
        assert p["songs_skipped"] == 1
        assert p["songs_failed"] == 1
        assert p["percent"] == 66

        # When all are done, percent must be 100%
        albums[2]["songs"][0]["status"] = STATUS_ADDED
        albums[2]["songs"][1]["status"] = STATUS_FAILED
        albums[2]["status"] = STATUS_PARTIALLY_FAILED
        p_final = orch._refresh_progress("job_123", albums, STATUS_PARTIALLY_FAILED, "Finished")
        assert p_final["total_songs"] == 6
        assert p_final["percent"] == 100

    def test_08_failed_job_marks_all_songs_terminal_no_dangling_waiting(self, tmp_path):
        db_mock = MagicMock()
        orch = MusicDataSyncOrchestrator(db=db_mock)

        # Simulate job execution failure during Phase 2
        albums = [
            {
                "notification_id": 1,
                "destination_folder": "AlbumOne",
                "action_mode": "NEW_ALBUM",
                "status": STATUS_WAITING,
                "songs": [{"song_key": "s1", "song_title": "Song 1", "status": STATUS_WAITING, "target_filename": "Song_1.mp3", "candidate_urls": [{"bitrate": "320", "url": "https://naasongs.com.co/s1.mp3"}]}],
            },
            {
                "notification_id": 2,
                "destination_folder": "AlbumTwo",
                "action_mode": "NEW_ALBUM",
                "status": STATUS_WAITING,
                "songs": [{"song_key": "s2", "song_title": "Song 2", "status": STATUS_WAITING, "target_filename": "Song_2.mp3", "candidate_urls": [{"bitrate": "320", "url": "https://naasongs.com.co/s2.mp3"}]}],
            },
        ]

        # Mock downloader so AlbumOne succeeds, AlbumTwo fails
        def fake_dl(candidate_urls, dest, title, on_validating=None):
            if "Song 2" in title:
                raise RuntimeError("Connection dropped by peer")
            dest.write_bytes(b"dummy")
            return {"used_bitrate": "320"}

        orch._downloader = fake_dl
        orch._artwork_downloader = MagicMock(return_value=tmp_path / "art.png")
        orch._provision_workspace_repo = MagicMock()

        # Run job
        orch._execute_job("job_fail_test", albums, acquired_locks=[])

        # Verify DB update call received 100% terminal statuses
        update_calls = db_mock.update_sync_job.call_args_list
        assert len(update_calls) >= 1
        last_call_kwargs = update_calls[-1].kwargs

        assert last_call_kwargs["status"] == STATUS_FAILED
        progress_data = last_call_kwargs["progress_data"]
        assert progress_data["percent"] == 100
        assert progress_data["songs_added"] == 0
        assert progress_data["songs_failed"] >= 1
        # None of the songs can be left in WAITING or DOWNLOADING
        for a in progress_data["albums"]:
            assert a["status"] in TERMINAL_STATUSES
            for s in a["songs"]:
                assert s["status"] in TERMINAL_STATUSES

    def test_09_oversized_audio_files_95mb_boundary(self, tmp_path):
        # 1. Stotram track size (~70.9 MB) must pass validation
        stotram_bytes = 70927827
        assert stotram_bytes <= MAX_AUDIO_FILE_SIZE_BYTES

        # 2. File <= 95 MB is within allowed limit
        exact_95mb = 95 * 1024 * 1024
        assert exact_95mb <= MAX_AUDIO_FILE_SIZE_BYTES

        # 3. File > 95 MB (e.g. 96 MB) exceeds allowed limit
        over_95mb_file = tmp_path / "oversized.mp3"
        over_95mb_file.write_bytes(b"0" * 100)
        # Mock stat size to 96 MB
        stat_mock = MagicMock()
        stat_mock.st_size = 96 * 1024 * 1024

        with patch.object(Path, "stat", return_value=stat_mock):
            with pytest.raises(FileValidationError) as exc:
                ValidationService.validate_mp3_file(over_95mb_file)

        assert "exceeds maximum allowed size of 95 MB" in str(exc.value)

    def test_10_retry_failed_job_isolates_failed_songs_only(self, tmp_path):
        db_mock = MagicMock()
        parent_job = {
            "job_id": "mdsync_parent_123",
            "status": STATUS_PARTIALLY_FAILED,
            "progress": {
                "albums": [
                    {
                        "notification_id": 101,
                        "album_name": "Tyson Naidu",
                        "status": STATUS_ADDED,
                        "songs": [{"song_key": "k1", "status": STATUS_ADDED}],
                    },
                    {
                        "notification_id": 102,
                        "album_name": "Epic",
                        "status": STATUS_FAILED,
                        "songs": [{"song_key": "k2", "status": STATUS_FAILED}],
                    },
                    {
                        "notification_id": 103,
                        "album_name": "Aura",
                        "status": STATUS_SKIPPED,
                        "songs": [{"song_key": "k3", "status": STATUS_SKIPPED}],
                    },
                ]
            }
        }
        db_mock.get_sync_job.return_value = parent_job

        orch = MusicDataSyncOrchestrator(db=db_mock)
        orch.start_sync_job = MagicMock(return_value={"success": True, "job_id": "mdsync_retry_456"})

        orch.retry_failed_job(parent_job_id="mdsync_parent_123", run_async=False)

        assert orch.start_sync_job.called
        call_kwargs = orch.start_sync_job.call_args.kwargs
        selections = call_kwargs["selections"]

        # Only Album 102 (Epic) had failed songs
        assert len(selections) == 1
        assert selections[0]["notification_id"] == 102
        assert selections[0]["selected_song_keys"] == ["k2"]
        assert call_kwargs["parent_job_id"] == "mdsync_parent_123"

    def test_11_cross_process_file_lock_mutual_exclusion(self, tmp_path):
        lock_file = tmp_path / ".git" / "musicdata_repo.lock"
        fl1 = RepositoryFileLock(lock_file, timeout=0.2)
        fl2 = RepositoryFileLock(lock_file, timeout=0.2)

        # 1. Acquire first lock
        assert fl1.acquire() is True

        # 2. Second lock acquisition should time out and raise GitSyncError
        with pytest.raises(GitSyncError) as exc:
            fl2.acquire()
        assert "Timeout waiting for OS repository lock" in str(exc.value)

        # 3. Release first lock, second should now acquire cleanly
        fl1.release()
        assert fl2.acquire() is True
        fl2.release()

    def test_12_stale_index_lock_not_deleted_if_git_process_running(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir(parents=True)
        index_lock = git_dir / "index.lock"
        index_lock.write_text("active git process lock")

        old_mtime = time.time() - 60
        os.utime(index_lock, (old_mtime, old_mtime))

        repo_mock = MagicMock()
        repo_mock.root = tmp_path
        svc = BaseGitSyncService(repo_mock)

        # 1. When an active Git process is detected in the worktree, NEVER delete index.lock
        with patch("backend.services.git_sync.is_git_process_running_in_worktree", return_value=True):
            svc._clean_stale_index_lock()
            assert index_lock.exists()

        # 2. When NO active Git process is running, stale index.lock (>25s) is safely cleaned up
        with patch("backend.services.git_sync.is_git_process_running_in_worktree", return_value=False):
            svc._clean_stale_index_lock()
            assert not index_lock.exists()

    def test_13_stage_commit_and_push_path_traversal_rejection(self, tmp_path):
        repo = MagicMock(spec=IRepositoryProvider)
        repo.root = tmp_path
        repo.validate_safe_filename.side_effect = lambda x: IRepositoryProvider.validate_safe_filename(None, x)
        svc = BaseGitSyncService(repo)
        svc._run_git = MagicMock(return_value="true")

        # 1. Reject album_name with path traversal
        with pytest.raises(PathTraversalError):
            svc.stage_commit_and_push(album_name="../escaped_album", push_enabled=False)

        with pytest.raises(PathTraversalError):
            svc.stage_commit_and_push(album_name="sub/nested/album", push_enabled=False)

        # 2. Reject extra_stage_paths with path traversal
        with pytest.raises(PathTraversalError):
            svc.stage_commit_and_push(
                album_name="ValidAlbum",
                extra_stage_paths=["../../outside.json"],
                push_enabled=False
            )

    def test_14_safe_argument_separation_git_add_double_dash(self, tmp_path):
        repo = MagicMock(spec=IRepositoryProvider)
        repo.root = tmp_path
        repo.validate_safe_filename.side_effect = lambda x: IRepositoryProvider.validate_safe_filename(None, x)
        svc = BaseGitSyncService(repo)

        captured_git_args = []

        def fake_run_git(args, check=True, timeout=60):
            captured_git_args.append(args)
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if args == ["status", "--porcelain"]:
                return "M  metadata/statistics.json"
            if args == ["rev-parse", "HEAD"]:
                return "head_commit_sha"
            return ""

        svc._run_git = fake_run_git
        svc.stage_commit_and_push(album_name="ValidAlbum", push_enabled=False)

        # Verify git add uses safe argument separation with '--'
        add_cmds = [cmd for cmd in captured_git_args if cmd and cmd[0] == "add"]
        assert len(add_cmds) >= 1
        assert add_cmds[0][1] == "--"
        assert "ValidAlbum" in add_cmds[0][2:]
