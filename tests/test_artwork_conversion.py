import io
import os
import sys
import unittest
from pathlib import Path
from PIL import Image

# Add project root to sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.services.validation import ValidationService, FileValidationError, PNG_MAGIC_BYTES

class TestArtworkConversionAndValidation(unittest.TestCase):

    def _create_sample_image(self, fmt: str, mode: str = "RGB", size=(120, 120)) -> bytes:
        img = Image.new(mode, size, color="blue")
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    def test_01_real_png_accepted(self):
        png_input = self._create_sample_image("PNG")
        out_bytes = ValidationService.process_and_convert_to_png(png_input, "cover.png")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

    def test_02_jpeg_converted_to_png(self):
        jpeg_input = self._create_sample_image("JPEG")
        self.assertTrue(jpeg_input.startswith(b"\xff\xd8"))  # Confirm JPEG magic bytes
        out_bytes = ValidationService.process_and_convert_to_png(jpeg_input, "cover.jpg")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

    def test_03_jpg_converted_to_png(self):
        jpg_input = self._create_sample_image("JPEG")
        out_bytes = ValidationService.process_and_convert_to_png(jpg_input, "album_art.jpeg")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

    def test_04_webp_converted_to_png(self):
        webp_input = self._create_sample_image("WEBP")
        out_bytes = ValidationService.process_and_convert_to_png(webp_input, "artwork.webp")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

    def test_05_bmp_converted_to_png(self):
        bmp_input = self._create_sample_image("BMP")
        out_bytes = ValidationService.process_and_convert_to_png(bmp_input, "poster.bmp")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

    def test_06_jpeg_disguised_as_png_converted(self):
        """File named Lovers-day-jpeg-1.png containing actual JPEG data should be decoded & converted."""
        jpeg_input = self._create_sample_image("JPEG")
        out_bytes = ValidationService.process_and_convert_to_png(jpeg_input, "Lovers-day-jpeg-1.png")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

    def test_07_transparent_png_preserves_rgba(self):
        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        rgba_png = buf.getvalue()

        out_bytes = ValidationService.process_and_convert_to_png(rgba_png, "transparent.png")
        self.assertTrue(out_bytes.startswith(PNG_MAGIC_BYTES))

        # Re-open and verify RGBA mode is preserved
        res_img = Image.open(io.BytesIO(out_bytes))
        self.assertEqual(res_img.mode, "RGBA")

    def test_08_empty_file_rejected(self):
        with self.assertRaises(FileValidationError):
            ValidationService.process_and_convert_to_png(b"", "empty.png")

    def test_09_random_binary_file_rejected(self):
        fake_binary = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"  # Fake PE executable
        with self.assertRaises(FileValidationError):
            ValidationService.process_and_convert_to_png(fake_binary, "fake_app.png")

    def test_10_corrupted_image_rejected(self):
        corrupted = b"\x89PNG\r\n\x1a\ncorrupted_data_junk_12345"
        with self.assertRaises(FileValidationError):
            ValidationService.process_and_convert_to_png(corrupted, "corrupted.png")

    def test_11_cloud_upload_endpoint_accepts_jpeg(self):
        """Test POST /api/upload/artwork/{album_name} in Cloud Mode accepts JPEG and stores PNG."""
        os.environ["CLOUD_MODE"] = "1"
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        jpeg_input = self._create_sample_image("JPEG")
        
        res = client.post(
            "/api/upload/artwork/Pushpa",
            files={"file": ("Lovers-day-jpeg-1.png", jpeg_input, "image/png")}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["saved_filename"], "Pushpa.png")

if __name__ == "__main__":
    unittest.main()
