import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine
from backend.services.latest_releases.models import NotificationType

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSchedulerCLIAndWorkflow:

    def test_workflow_and_render_yaml_configuration(self):
        import yaml

        wf_path = REPO_ROOT / ".github" / "workflows" / "latest-releases-daily.yml"
        assert wf_path.exists(), "GitHub Actions workflow file must exist."
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))

        assert wf["name"] == "Latest Releases Daily Scan"
        on_section = wf.get("on") or wf.get(True)
        assert on_section is not None
        assert "workflow_dispatch" in on_section
        schedules = on_section.get("schedule", [])
        assert any(s.get("cron") == "30 3 * * *" for s in schedules)

        assert wf.get("permissions", {}).get("contents") == "read"
        assert wf.get("concurrency", {}).get("group") == "latest-releases-discovery-scan"

        job = wf["jobs"]["discovery-scan"]
        assert job["runs-on"] == "ubuntu-latest"
        assert job["timeout-minutes"] >= 10
        assert job["env"]["CLOUD_MODE"] == "1"
        assert job["env"]["LATEST_RELEASES_ENABLED"] == "1"
        assert job["env"]["REQUIRE_POSTGRES"] == "1"

        run_cmds = "\n".join(str(step.get("run", "")) for step in job["steps"])
        assert "python -m backend.services.latest_releases.cli scan" in run_cmds
        assert "requirements.txt" in run_cmds
        assert "secrets.DATABASE_URL" in wf_path.read_text(encoding="utf-8")

        render_path = REPO_ROOT / "render.yaml"
        render_cfg = yaml.safe_load(render_path.read_text(encoding="utf-8"))
        dbs = [d["name"] for d in render_cfg.get("databases", [])]
        assert "musicdata-db" in dbs
        services = render_cfg.get("services", [])
        assert len(services) == 1
        assert services[0]["type"] == "web"
        assert services[0]["name"] == "musicdata-manager-cloud"
        assert services[0]["plan"] == "free"
        assert not any(s.get("type") == "cron" for s in services)

    def test_require_postgres_refuses_sqlite_fallback(self, tmp_path):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            LatestReleasesDB(
                database_url="",
                sqlite_path=str(tmp_path / "should_not_use.db"),
                require_postgres=True
            )

        mock_pg = MagicMock()
        mock_pg.connect.side_effect = ConnectionError("connection refused")
        with patch.dict("sys.modules", {"psycopg2": mock_pg, "psycopg2.extras": MagicMock()}):
            with pytest.raises(RuntimeError, match="PostgreSQL connection failed"):
                LatestReleasesDB(
                    database_url="postgresql://user:secret@dpg-test.oregon-postgres.render.com/db",
                    sqlite_path=str(tmp_path / "should_not_use2.db"),
                    require_postgres=True
                )
            # Verify sslmode=require was requested for external Render PostgreSQL host
            _, call_kwargs = mock_pg.connect.call_args
            assert call_kwargs.get("sslmode") == "require"

    def test_failed_scan_creates_no_false_notifications_and_cli_exits_nonzero(self, tmp_path):
        db = LatestReleasesDB(database_url="", sqlite_path=str(tmp_path / "fail_test.db"), require_postgres=False)
        mock_source = MagicMock()
        mock_source.fetch_page.return_value = None

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        result = engine.run_scan(locked_by="github_actions_cron")

        assert result["success"] is False
        assert result["status"] == "FAILED"
        assert db.get_notifications(status="all") == []
        assert db.get_unread_count() == 0
        assert db.get_lock_status()["is_locked"] is False

        with patch("backend.services.latest_releases.discovery.LatestReleasesDiscoveryEngine.run_scan", return_value=result):
            from backend.services.latest_releases.cli import main
            with patch.object(sys, "argv", ["cli.py", "scan"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    def test_cli_skips_cleanly_when_locked_and_exits_zero_on_success(self):
        from backend.services.latest_releases.cli import main

        with patch("backend.services.latest_releases.discovery.LatestReleasesDiscoveryEngine.run_scan", return_value={"success": False, "status": "LOCKED"}):
            with patch.object(sys, "argv", ["cli.py", "scan"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0

        with patch("backend.services.latest_releases.discovery.LatestReleasesDiscoveryEngine.run_scan", return_value={"success": True, "status": "COMPLETED"}):
            with patch.object(sys, "argv", ["cli.py", "scan"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0

    def test_distributed_lock_and_deduplication_via_shared_db(self, tmp_path):
        db = LatestReleasesDB(database_url="", sqlite_path=str(tmp_path / "lock_dedup.db"), require_postgres=False)

        assert db.acquire_scan_lock(locked_by="github_actions_cron", timeout_minutes=15) is True
        assert db.acquire_scan_lock(locked_by="manual_web_ui", timeout_minutes=15) is False
        db.release_scan_lock(locked_by="github_actions_cron")
        assert db.acquire_scan_lock(locked_by="manual_web_ui", timeout_minutes=15) is True
        db.release_scan_lock(locked_by="manual_web_ui")

        songs = [{"song_title": "Kobali Song", "exists_in_catalog": False}]
        first = db.create_or_update_notification(
            album_id=10,
            notification_type=NotificationType.NEW_SONG,
            title="New Song: Ranabaali",
            message="Kobali Song missing",
            album_name="Ranabaali",
            artwork_url="https://example.com/art.jpg",
            music_director="Ajay Atul",
            year=2026,
            source_url="https://naasongs.com.co/ranabaali-2026-songs-c.html",
            songs_list=songs,
            missing_count=1,
            language="Telugu"
        )
        second = db.create_or_update_notification(
            album_id=10,
            notification_type=NotificationType.NEW_SONG,
            title="New Song: Ranabaali",
            message="Kobali Song missing",
            album_name="Ranabaali",
            artwork_url="https://example.com/art.jpg",
            music_director="Ajay Atul",
            year=2026,
            source_url="https://naasongs.com.co/ranabaali-2026-songs-c.html",
            songs_list=songs,
            missing_count=1,
            language="Telugu"
        )
        assert first is True
        assert second is False
        assert db.get_unread_count() == 1

    def test_database_url_normalization_validation_and_safe_diagnostics(self, caplog, tmp_path):
        from backend.services.latest_releases.db import normalize_database_url, describe_database_url

        # Normalizes wrapping whitespace, quotes, accidental key prefix, and psycopg2 scheme cleanly
        assert normalize_database_url('  "postgres://u:p@h.render.com/db"  ') == "postgresql://u:p@h.render.com/db"
        assert normalize_database_url("DATABASE_URL='postgresql+psycopg2://u:p@h.render.com/db'") == "postgresql://u:p@h.render.com/db"
        assert normalize_database_url("POSTGRESQL://u:p@h.render.com/db") == "postgresql://u:p@h.render.com/db"

        # Safe diagnostics never expose credentials, host, or database name
        secret_sample = "postgresql://secret_user_123:super_secret_pw_999@dpg-secret-host.oregon-postgres.render.com/musicdata_prod"
        diag_ok = describe_database_url(f'  "{secret_sample}"  ')
        assert diag_ok["present"] is True
        assert diag_ok["scheme"] == "postgresql"
        assert diag_ok["valid_postgres_scheme"] is True
        assert diag_ok["normalized_wrapping"] is True
        for forbidden in ("secret_user_123", "super_secret_pw_999", "dpg-secret-host", "musicdata_prod"):
            assert forbidden not in str(diag_ok)

        # Detects psql command strings safely without leaking PGPASSWORD
        psql_cmd = "PGPASSWORD=leaked_secret_pw psql -h host.render.com -U user db"
        diag_psql = describe_database_url(psql_cmd)
        assert diag_psql["present"] is True
        assert diag_psql["valid_postgres_scheme"] is False
        assert "psql" in diag_psql["scheme"]
        assert "leaked_secret_pw" not in str(diag_psql)

        # Missing / invalid URI error message remains descriptive yet secret-free under REQUIRE_POSTGRES=1
        with pytest.raises(RuntimeError) as exc_info:
            LatestReleasesDB(
                database_url="sqlite:///secret_local_path.db",
                sqlite_path=str(tmp_path / "unused.db"),
                require_postgres=True,
            )
        err_msg = str(exc_info.value)
        assert "PostgreSQL is required (REQUIRE_POSTGRES=1)" in err_msg
        assert "scheme='sqlite'" in err_msg
        assert "secret_local_path" not in err_msg

        # CLI logs safe pre-execution diagnostics and never exposes credentials
        from backend.services.latest_releases import cli
        with patch.dict("os.environ", {"DATABASE_URL": secret_sample, "REQUIRE_POSTGRES": "1"}, clear=False), \
             patch("backend.services.latest_releases.cli.LatestReleasesDiscoveryEngine") as mock_cls, \
             patch("sys.argv", ["cli.py", "scan"]):
            mock_inst = MagicMock()
            mock_inst.run_scan.return_value = {"success": True, "status": "COMPLETED"}
            mock_cls.return_value = mock_inst
            with caplog.at_level("INFO"):
                with pytest.raises(SystemExit) as sys_exit:
                    cli.main()
                assert sys_exit.value.code == 0

        joined_logs = "\n".join(caplog.messages)
        assert "Database environment pre-check:" in joined_logs
        assert "present=True, scheme='postgresql', valid_postgres_scheme=True, require_postgres=True" in joined_logs
        for forbidden in ("secret_user_123", "super_secret_pw_999", "dpg-secret-host", "musicdata_prod"):
            assert forbidden not in joined_logs

    def test_workflow_maps_secret_without_empty_overwrite(self):
        import yaml

        wf_path = REPO_ROOT / ".github" / "workflows" / "latest-releases-daily.yml"
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        job = wf["jobs"]["discovery-scan"]
        assert "DATABASE_URL" not in job.get("env", {}) or job["env"]["DATABASE_URL"] != ""

        verify_step = next(s for s in job["steps"] if s.get("name") == "Verify required secret configuration")
        cli_step = next(s for s in job["steps"] if s.get("name") == "Run Latest Releases Discovery Engine CLI")
        assert verify_step["env"]["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
        assert cli_step["env"]["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
        assert "describe_database_url" in verify_step["run"]
        for step in job["steps"]:
            step_env = step.get("env") or {}
            if "DATABASE_URL" in step_env:
                assert str(step_env["DATABASE_URL"]).strip() != ""
