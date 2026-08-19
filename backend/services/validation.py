import os
import io
from pathlib import Path
from typing import Dict, Tuple, Optional
from mutagen.mp3 import MP3
from PIL import Image

PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

class FileValidationError(Exception):
    pass

class ValidationService:
    @staticmethod
    def process_and_convert_to_png(content: bytes, filename: str) -> bytes:
        """
        Decodes any valid raster image (JPG, JPEG, WebP, BMP, PNG, etc.), converts to real PNG bytes,
        preserves original dimensions, aspect ratio, transparency, and quality, and verifies PNG magic bytes.
        Raises FileValidationError for 0-byte, corrupted, or non-image files.
        """
        if not content or len(content) == 0:
            raise FileValidationError(f"File '{filename}' is empty (0 bytes).")

        if len(content) > MAX_FILE_SIZE_BYTES:
            raise FileValidationError(f"File '{filename}' exceeds maximum allowed size of 50 MB.")

        try:
            # Verify image content integrity with Pillow
            img_verify = Image.open(io.BytesIO(content))
            img_verify.verify()

            # Re-open image after verify() call
            img = Image.open(io.BytesIO(content))
        except Exception as e:
            raise FileValidationError(f"File '{filename}' is not a valid or supported image file: {e}")

        try:
            # Preserve transparency if available (RGBA/LA/Palette with alpha)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                converted_img = img.convert("RGBA")
            else:
                converted_img = img.convert("RGB")

            buf = io.BytesIO()
            converted_img.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()

            if not png_bytes.startswith(PNG_MAGIC_BYTES):
                raise FileValidationError(f"Internal conversion failed for '{filename}'. Output is not a valid PNG.")

            return png_bytes
        except Exception as e:
            raise FileValidationError(f"Failed to process and convert image '{filename}' to PNG: {e}")

    @staticmethod
    def validate_png_bytes(content: bytes, filename: str) -> bytes:
        """Decodes uploaded image content and converts it to a real PNG byte stream."""
        return ValidationService.process_and_convert_to_png(content, filename)

    @staticmethod
    def validate_png_file(file_path: Path) -> bytes:
        """Validates and converts image at file_path into real PNG bytes."""
        if not file_path.exists():
            raise FileValidationError(f"Artwork file '{file_path.name}' does not exist.")
        content = file_path.read_bytes()
        return ValidationService.process_and_convert_to_png(content, file_path.name)

    @staticmethod
    def validate_mp3_file(file_path: Path) -> Dict[str, any]:
        """Validate MP3 audio stream, file size > 0, and duration > 0."""
        if not file_path.exists():
            raise FileValidationError(f"Audio file '{file_path.name}' does not exist.")

        file_size = file_path.stat().st_size
        if file_size == 0:
            raise FileValidationError(f"Zero-Byte Shield Triggered: '{file_path.name}' has file size 0 bytes.")

        if file_size > MAX_FILE_SIZE_BYTES:
            raise FileValidationError(f"Audio file '{file_path.name}' exceeds maximum allowed size of 50 MB.")

        if not file_path.suffix.lower() == ".mp3":
            raise FileValidationError(f"Invalid extension for '{file_path.name}'. Only .mp3 files are supported.")

        try:
            audio = MP3(file_path)
            duration_seconds = int(audio.info.length)
            if duration_seconds <= 0:
                raise FileValidationError(f"Audio file '{file_path.name}' has invalid or zero duration.")

            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            duration_str = f"{minutes}:{seconds:02d}"

            return {
                "valid": True,
                "duration": duration_str,
                "durationSeconds": duration_seconds,
                "bitrate": int(getattr(audio.info, "bitrate", 0)),
                "sampleRate": int(getattr(audio.info, "sample_rate", 0)),
                "channels": int(getattr(audio.info, "channels", 0)),
                "fileSize": file_size
            }
        except FileValidationError:
            raise
        except Exception as e:
            raise FileValidationError(f"Failed to parse audio stream for '{file_path.name}': {e}")
