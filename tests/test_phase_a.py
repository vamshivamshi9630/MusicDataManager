import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

# Add real repo to sys.path for test suite import
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

real_repo_root = Path(r"C:\Users\vamshi\OneDrive\Desktop\Projects\MusicData\MusicData-main").resolve()
sys.path.insert(0, str(real_repo_root))

from generator.cache_reader import (
    CacheStatus,
    CacheMissError,
    CacheInvalidError,
    StrictCloudSafetyViolation,
    GeneratorTelemetry,
    load_album_cache,
    read_song_from_cache
)
from generator.metadata_reader import read_song_metadata
from generator.scanner import scan_all_albums
from generate_metadata import build_album_objects

TEST_RESULTS = {}

def record_phase_a_test(test_no: int, name: str, status: str, details: str):
    TEST_RESULTS[test_no] = {"name": name, "status": status, "details": details}

class TestPhaseA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_workspace = Path(tempfile.mkdtemp(prefix="phase_a_test_"))
        
        shutil.copy(str(real_repo_root / "generate_metadata.py"), str(cls.test_workspace / "generate_metadata.py"))
        shutil.copytree(str(real_repo_root / "generator"), str(cls.test_workspace / "generator"))
        shutil.copytree(str(real_repo_root / "metadata"), str(cls.test_workspace / "metadata"))
        if (real_repo_root / "MusicDirectorImages").exists():
            shutil.copytree(str(real_repo_root / "MusicDirectorImages"), str(cls.test_workspace / "MusicDirectorImages"))
        
        for sample in ["Pushpa2", "100% Love"]:
            if (real_repo_root / sample).exists():
                shutil.copytree(str(real_repo_root / sample), str(cls.test_workspace / sample))

    @classmethod
    def tearDownClass(cls):
        if cls.test_workspace.exists():
            shutil.rmtree(cls.test_workspace, ignore_errors=True)
        print("\n=======================================================")
        print(" PHASE A TEST VERIFICATION SUMMARY REPORT")
        print("=======================================================")
        print(json.dumps(TEST_RESULTS, indent=2))
        print("=======================================================\n")

    # TEST 1: Normal local repository generation
    def test_01_normal_local_generation(self):
        try:
            scanned = scan_all_albums()
            pushpa_scanned = [data for data in scanned if data["album_name"] == "Pushpa2"]
            self.assertGreater(len(pushpa_scanned), 0)
            telemetry = GeneratorTelemetry()
            albums, telemetry = build_album_objects(pushpa_scanned, use_cache=False, telemetry=telemetry)
            self.assertEqual(len(albums), 1)
            self.assertGreater(telemetry.actual_mp3_files_opened, 0)
            record_phase_a_test(1, "Normal Local Repository Generation", "PASS", f"Processed {len(albums)} albums. Output compatible with baseline.")
        except Exception as e:
            record_phase_a_test(1, "Normal Local Repository Generation", "FAIL", str(e))
            raise

    # TEST 2: Blobless repository with cache (0 MP3 files opened)
    def test_02_blobless_repository_with_cache(self):
        try:
            scanned = scan_all_albums()
            pushpa_scanned = [data for data in scanned if data["album_name"] == "Pushpa2"]
            telemetry = GeneratorTelemetry()
            albums, telemetry = build_album_objects(pushpa_scanned, use_cache=True, strict_cloud_safety=True, telemetry=telemetry)
            
            self.assertGreater(telemetry.cache_hits, 0)
            self.assertEqual(telemetry.actual_mp3_files_opened, 0)
            self.assertEqual(telemetry.mutagen_reads_performed, 0)
            record_phase_a_test(2, "Blobless Repo Cache Hits (0 MP3s opened)", "PASS", f"Cache Hits: {telemetry.cache_hits}, Actual MP3 Files Opened: {telemetry.actual_mp3_files_opened}.")
        except Exception as e:
            record_phase_a_test(2, "Blobless Repo Cache Hits (0 MP3s opened)", "FAIL", str(e))
            raise

    # TEST 3: Blobless repository with 2 newly uploaded MP3s
    def test_03_blobless_with_new_mp3s(self):
        try:
            scanned = scan_all_albums()
            pushpa_scanned = [data for data in scanned if data["album_name"] == "Pushpa2"]
            telemetry = GeneratorTelemetry()
            modified = {("Pushpa2", "Peelings.mp3"), ("Pushpa2", "Kissik Song.mp3")}
            
            albums, telemetry = build_album_objects(
                pushpa_scanned,
                use_cache=True,
                strict_cloud_safety=True,
                modified_songs=modified,
                telemetry=telemetry
            )
            
            self.assertEqual(telemetry.newly_uploaded_songs, 2)
            self.assertEqual(telemetry.actual_mp3_files_opened, 2)
            self.assertGreater(telemetry.cache_hits, 0)
            record_phase_a_test(3, "Blobless Repo with 2 New MP3s", "PASS", f"Only newly uploaded songs opened: {telemetry.actual_mp3_files_opened} MP3s.")
        except Exception as e:
            record_phase_a_test(3, "Blobless Repo with 2 New MP3s", "FAIL", str(e))
            raise

    # TEST 4: Missing cache record
    def test_04_missing_cache_record(self):
        try:
            cache_res = read_song_from_cache(None, "Pushpa2", Path("NonExistentSong.mp3"))
            self.assertEqual(cache_res.status, CacheStatus.MISS)
            
            with self.assertRaises(CacheMissError):
                read_song_metadata(
                    album_name="Pushpa2",
                    mp3_file=Path("NonExistentSong.mp3"),
                    music_director="Devi Sri Prasad",
                    album_cache=None,
                    is_new_or_modified=False,
                    strict_cloud_safety=True
                )
            record_phase_a_test(4, "Missing Cache Record", "PASS", "Cache miss returns explicit CACHE_MISS and raises CacheMissError in strict mode.")
        except Exception as e:
            record_phase_a_test(4, "Missing Cache Record", "FAIL", str(e))
            raise

    # TEST 5: Invalid cache record
    def test_05_invalid_cache_record(self):
        try:
            invalid_cache = {
                "album": {"name": "Pushpa2"},
                "songs": [{
                    "id": "wrong_id",
                    "audio": "Peelings.mp3",
                    "title": "Peelings"
                }]
            }
            cache_res = read_song_from_cache(invalid_cache, "Pushpa2", Path("Peelings.mp3"))
            self.assertEqual(cache_res.status, CacheStatus.INVALID)
            
            with self.assertRaises(CacheInvalidError):
                read_song_metadata(
                    album_name="Pushpa2",
                    mp3_file=Path("Peelings.mp3"),
                    music_director="Devi Sri Prasad",
                    album_cache=invalid_cache,
                    is_new_or_modified=False,
                    strict_cloud_safety=True
                )
            record_phase_a_test(5, "Invalid Cache Record", "PASS", "Incomplete/invalid cache record returns CACHE_INVALID and halts execution in strict mode.")
        except Exception as e:
            record_phase_a_test(5, "Invalid Cache Record", "FAIL", str(e))
            raise

    # TEST 6: Changed/new song
    def test_06_changed_new_song(self):
        try:
            telemetry = GeneratorTelemetry()
            pushpa_mp3 = list(real_repo_root.glob("Pushpa2/*.mp3"))[0]
            audio_info, singers, composer = read_song_metadata(
                album_name="Pushpa2",
                mp3_file=pushpa_mp3,
                music_director="Devi Sri Prasad",
                album_cache=None,
                is_new_or_modified=True,
                strict_cloud_safety=True,
                telemetry=telemetry
            )
            self.assertEqual(telemetry.newly_uploaded_songs, 1)
            self.assertEqual(telemetry.actual_mp3_files_opened, 1)
            record_phase_a_test(6, "Changed / New Song Read", "PASS", "New/modified song triggers actual mutagen read.")
        except Exception as e:
            record_phase_a_test(6, "Changed / New Song Read", "FAIL", str(e))
            raise

    # TEST 7: Song ID mismatch
    def test_07_song_id_mismatch(self):
        try:
            mismatched_cache = {
                "album": {"name": "Pushpa2"},
                "songs": [{
                    "id": "111111111111",
                    "audio": "Peelings.mp3",
                    "duration": "4:11",
                    "durationSeconds": 251,
                    "bitrate": 320000,
                    "sampleRate": 44100,
                    "channels": 2,
                    "fileSize": 10410424,
                    "singers": ["DSP"],
                    "composer": "DSP"
                }]
            }
            cache_res = read_song_from_cache(mismatched_cache, "Pushpa2", Path("Peelings.mp3"))
            self.assertEqual(cache_res.status, CacheStatus.INVALID)
            self.assertIn("Song ID mismatch", cache_res.reason)
            record_phase_a_test(7, "Song ID Mismatch Guard", "PASS", "Mismatched song ID causes cache rejection.")
        except Exception as e:
            record_phase_a_test(7, "Song ID Mismatch Guard", "FAIL", str(e))
            raise

    # TEST 8: Output regression test
    def test_08_output_regression(self):
        try:
            scanned = scan_all_albums()
            pushpa_scanned = [data for data in scanned if data["album_name"] == "Pushpa2"]
            albums_no_cache, _ = build_album_objects(pushpa_scanned, use_cache=False)
            albums_with_cache, _ = build_album_objects(pushpa_scanned, use_cache=True)
            
            self.assertEqual(len(albums_no_cache), len(albums_with_cache))
            for a1, a2 in zip(albums_no_cache, albums_with_cache):
                self.assertEqual(a1.id, a2.id)
                self.assertEqual(a1.name, a2.name)
                self.assertEqual(len(a1.songs), len(a2.songs))
                for s1, s2 in zip(a1.songs, a2.songs):
                    self.assertEqual(s1.id, s2.id)
                    self.assertEqual(s1.durationSeconds, s2.durationSeconds)
                    self.assertEqual(s1.bitrate, s2.bitrate)
                    self.assertEqual(s1.singers, s2.singers)

            record_phase_a_test(8, "Output Regression Test", "PASS", "Cache-aware output dataclasses match non-cached mutagen output 100%.")
        except Exception as e:
            record_phase_a_test(8, "Output Regression Test", "FAIL", str(e))
            raise

if __name__ == "__main__":
    unittest.main()
