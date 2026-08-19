import os
import sys
import json
import hashlib
import subprocess
from pathlib import Path
from collections import defaultdict

def discover_repository_root() -> Path:
    env_root = os.environ.get("MUSICDATA_REPOSITORY_ROOT")
    if env_root and Path(env_root).exists():
        return Path(env_root).resolve()

    current_dir = Path(__file__).resolve().parent
    candidate = current_dir.parent / "MusicData" / "MusicData-main"
    if candidate.exists() and (candidate / ".git").exists():
        return candidate.resolve()

    target = current_dir
    while target != target.parent:
        if (target / "MusicData-main").exists():
            return (target / "MusicData-main").resolve()
        target = target.parent

    raise FileNotFoundError("Could not auto-discover MusicData repository root.")

def get_git_info(repo_root: Path):
    info = {"git_root": str(repo_root), "remote": "Unknown", "branch": "Unknown", "status": "Unknown", "head": "Unknown"}
    try:
        remote_res = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=repo_root, capture_output=True, text=True, check=True)
        info["remote"] = remote_res.stdout.strip()
    except Exception as e:
        info["remote"] = f"Error: {e}"

    try:
        branch_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True)
        info["branch"] = branch_res.stdout.strip()
    except Exception as e:
        info["branch"] = f"Error: {e}"

    try:
        status_res = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True)
        info["status"] = "Clean" if not status_res.stdout.strip() else f"Dirty ({len(status_res.stdout.strip().splitlines())} modified/untracked files)"
    except Exception as e:
        info["status"] = f"Error: {e}"

    try:
        head_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True)
        info["head"] = head_res.stdout.strip()[:12]
    except Exception as e:
        info["head"] = f"Error: {e}"

    return info

def get_stable_id(val: str) -> str:
    return hashlib.sha256(val.strip().lower().encode('utf-8')).hexdigest()[:12]

def run_diagnostic():
    repo_root = discover_repository_root()
    git_info = get_git_info(repo_root)

    ignored_dirs = {".git", "generator", "metadata", "tmp", "__pycache__", ".idea", "MusicDirectorImages", "New_Icons"}
    
    albums = []
    total_mp3s = 0
    total_images = 0
    png_images = 0
    non_png_images = []
    zero_byte_mp3s = []
    missing_album_info = []
    missing_artwork = []
    malformed_artwork_name = []
    
    song_ids = defaultdict(list)
    filenames_map = defaultdict(list)
    
    album_dirs = sorted([d for d in repo_root.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in ignored_dirs])
    
    for album_dir in album_dirs:
        album_name = album_dir.name
        mp3_files = sorted(list(album_dir.glob("*.mp3")))
        image_files = sorted(list(album_dir.glob("*.png")) + list(album_dir.glob("*.jpg")) + list(album_dir.glob("*.jpeg")) + list(album_dir.glob("*.webp")))
        
        info_file = album_dir / "album_info.json"
        has_info = info_file.exists()
        if not has_info:
            missing_album_info.append(album_name)

        if mp3_files:
            total_mp3s += len(mp3_files)
            for mp3 in mp3_files:
                st = mp3.stat()
                if st.st_size == 0:
                    zero_byte_mp3s.append(str(mp3.relative_to(repo_root)))
                
                song_id = get_stable_id(f"{album_name}{mp3.name}")
                song_ids[song_id].append(str(mp3.relative_to(repo_root)))
                filenames_map[mp3.name.lower()].append(str(mp3.relative_to(repo_root)))

        if not image_files:
            missing_artwork.append(album_name)
        else:
            total_images += len(image_files)
            exact_png = album_dir / f"{album_name}.png"
            if exact_png.exists():
                png_images += 1
            else:
                malformed_artwork_name.append(album_name)
            
            for img in image_files:
                if img.suffix.lower() != ".png":
                    non_png_images.append(str(img.relative_to(repo_root)))

        albums.append({
            "name": album_name,
            "mp3_count": len(mp3_files),
            "image_count": len(image_files),
            "has_info": has_info
        })

    duplicate_song_ids = {k: v for k, v in song_ids.items() if len(v) > 1}

    generator_files = {
        "v2_main": (repo_root / "generate_metadata.py").exists(),
        "v2_package": (repo_root / "generator").exists(),
        "legacy_main": (repo_root / "generate_or_update_songs_with_details.py").exists(),
        "migration_script": (repo_root / "migrate_to_album_info.py").exists()
    }

    report = {
        "repository_root": str(repo_root),
        "git_info": git_info,
        "summary": {
            "total_album_directories": len(album_dirs),
            "total_mp3_files": total_mp3s,
            "total_image_files": total_images,
            "exact_png_artwork_count": png_images,
            "missing_album_info_count": len(missing_album_info),
            "missing_artwork_count": len(missing_artwork),
            "non_exact_png_artwork_name_count": len(malformed_artwork_name),
            "non_png_images_count": len(non_png_images),
            "zero_byte_mp3_count": len(zero_byte_mp3s),
            "duplicate_song_id_count": len(duplicate_song_ids)
        },
        "generator_files": generator_files,
        "diagnostics": {
            "zero_byte_mp3s": zero_byte_mp3s,
            "non_png_images": non_png_images[:10],
            "missing_artwork_albums": missing_artwork[:10],
            "missing_album_info_albums": missing_album_info[:10],
            "duplicate_song_ids_samples": dict(list(duplicate_song_ids.items())[:5])
        }
    }

    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    run_diagnostic()
