import os
from pathlib import Path
from typing import Dict, Tuple, Optional
from mutagen.mp3 import MP3

PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

class FileValidationError(Exception):
    pass

class ValidationService:
    @staticmethod
    def validate_png_bytes(content: bytes, filename: str) -> bool:
        """Validate PNG magic bytes, non-empty content, and .png extension."""
        if not filename.lower().endswith(".png"):
            raise FileValidationError(f"File '{filename}' rejected. Artwork artwork must strictly be a .png file.")

        if not content or len(content) < 8:
            raise FileValidationError(f"File '{filename}' is empty or truncated.")

        if len(content) > MAX_FILE_SIZE_BYTES:
            raise FileValidationError(f"File '{filename}' exceeds maximum allowed size of 50 MB.")

        if not content.startswith(PNG_MAGIC_BYTES):
            raise FileValidationError(
                f"File '{filename}' failed magic byte signature check. File is not a valid PNG image."
            )

        return True

    @staticmethod
    def validate_png_file(file_path: Path) -> bool:
        """Validate PNG file path by checking existence and reading binary signature."""
        if not file_path.exists():
            raise FileValidationError(f"PNG file '{file_path.name}' does not exist.")
        content = file_path.read_bytes()
        return ValidationService.validate_png_bytes(content, file_path.name)

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
