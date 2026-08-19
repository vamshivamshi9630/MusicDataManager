import os
import sys
import shutil
import unittest
import subprocess
from pathlib import Path
from PIL import Image

# Ensure project root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.core.repository import LocalRepository
from backend.services.validation import ValidationService, PNG_MAGIC_BYTES
from backend.services.metadata import MetadataService
from backend.services.generator import GeneratorService
from backend.services.git_sync import LocalGitSyncService

class TestLocalRealEndToEndPipeline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Force LOCAL mode for this test
        if "CLOUD_MODE" in os.environ:
            del os.environ["CLOUD_MODE"]
        
        cls.source_dir = Path(r"C:\Users\vamshi\Downloads\New folder")
        cls.repo = LocalRepository()
        cls.album_name = "Lovers Day"
        cls.music_director = "Unknown"
        cls.year = 2019
        cls.release_date = "2019-02-14"

        print(f"\n[LOCAL E2E TEST] Using Local Repository root: '{cls.repo.root}'")

    def test_01_verify_test_input_files_exist(self):
        self.assertTrue(self.source_dir.exists(), f"Source test folder '{self.source_dir}' must exist.")
        artwork_candidates = list(self.source_dir.glob("*.png")) + list(self.source_dir.glob("*.jpg")) + list(self.source_dir.glob("*.jpeg"))
        self.assertTrue(len(artwork_candidates) > 0, "Source artwork image must exist.")
        mp3_files = list(self.source_dir.glob("*.mp3"))
        self.assertEqual(len(mp3_files), 8, f"Expected 8 MP3 files in '{self.source_dir}', found {len(mp3_files)}.")

    def test_02_create_album_identity_and_metadata(self):
        album_dir = self.repo.get_album_path(self.album_name)
        album_dir.mkdir(parents=True, exist_ok=True)
        
        ms = MetadataService(self.repo)
        metadata_dict = {
            "album_name": self.album_name,
            "musicDirector": self.music_director,
            "year": self.year,
            "genre": "Tollywood Soundtrack",
            "language": "Telugu",
            "country": "India",
            "releaseDate": self.release_date
        }
        ms.save_album_info(self.album_name, metadata_dict)
        
        saved_info = ms.load_album_info(self.album_name)
        self.assertEqual(saved_info.get("musicDirector"), self.music_director)
        self.assertEqual(int(saved_info.get("year")), self.year)

    def test_03_artwork_conversion_and_magic_byte_validation(self):
        artwork_file = next(iter(self.source_dir.glob("*.png")))
        raw_bytes = artwork_file.read_bytes()
        
        png_bytes = ValidationService.process_and_convert_to_png(raw_bytes, artwork_file.name)
        self.assertTrue(png_bytes.startswith(PNG_MAGIC_BYTES), "Converted artwork must start with PNG magic bytes.")
        
        target_art_path = self.repo.get_image_path(self.album_name)
        target_art_path.write_bytes(png_bytes)
        self.assertTrue(target_art_path.exists())

    def test_04_mp3_validation_and_file_copy(self):
        v_svc = ValidationService()
        album_dir = self.repo.get_album_path(self.album_name)
        
        mp3_files = list(self.source_dir.glob("*.mp3"))
        for mp3_src in mp3_files:
            target_mp3 = album_dir / mp3_src.name
            shutil.copy2(str(mp3_src), str(target_mp3))
            
            # Validate MP3 audio specs
            specs = v_svc.validate_mp3_file(target_mp3)
            self.assertGreater(specs["durationSeconds"], 0)
            self.assertGreater(specs["fileSize"], 0)

    def test_05_execute_generator_pipeline_authoritative(self):
        gen_svc = GeneratorService(self.repo)
        result = gen_svc.run_generator_pipeline()
        
        self.assertTrue(result["success"])
        self.assertEqual(len(result["stages"]), 2)
        
        stage1 = result["stages"][0]
        self.assertEqual(stage1["stage"], "generate_metadata")
        self.assertEqual(stage1["exit_code"], 0)
        
        stage2 = result["stages"][1]
        self.assertEqual(stage2["stage"], "generate_or_update_songs_with_details")
        self.assertEqual(stage2["exit_code"], 0)

    def test_06_verify_generated_metadata_files(self):
        # 1. Check metadata/albums/L/Lovers Day.json
        part = self.repo.get_partition(self.album_name)
        album_json = self.repo.metadata_dir / "albums" / part / f"{self.album_name}.json"
        self.assertTrue(album_json.exists(), f"Generated album JSON '{album_json}' must exist.")
        
        # 2. Check songs_with_details.json
        songs_details_file = self.repo.root / "songs_with_details.json"
        self.assertTrue(songs_details_file.exists())
        
        import json
        with open(songs_details_file, "r", encoding="utf-8") as f:
            songs_data = json.load(f)
            
        matching = [s for s in songs_data if s.get("album", "").strip().lower() == self.album_name.lower()]
        self.assertEqual(len(matching), 8, f"Expected 8 songs for '{self.album_name}' in songs_with_details.json, found {len(matching)}.")
        
        for s in matching:
            self.assertEqual(s.get("musicDirector"), self.music_director)
            self.assertIn(str(s.get("year")), [str(self.year), "Unknown"])

    def test_07_git_stage_and_commit(self):
        git_svc = LocalGitSyncService(self.repo)
        
        commit_res = git_svc.stage_commit_and_push(
            album_name=self.album_name,
            mode="add",
            push_enabled=False
        )
        
        self.assertTrue(commit_res["committed"] or ("No new changes" in commit_res.get("message", "")))
        self.assertEqual(commit_res["commit_message"], f"added songs/album with {self.album_name}")

if __name__ == "__main__":
    unittest.main()
