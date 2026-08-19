import os
import json
import shutil
from pathlib import Path
from typing import List, Dict, Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Depends, Header
from pydantic import BaseModel

from backend.core.repository import RepositoryContext, PathTraversalError
from backend.services.validation import ValidationService, FileValidationError
from backend.services.metadata import MetadataService
from backend.services.duplicate import DuplicateDetectionService
from backend.services.generator import GeneratorService, GeneratorValidationError
from backend.services.git_sync import GitSyncService, GitSyncError

router = APIRouter(prefix="/api")

repo_context = RepositoryContext()
validation_service = ValidationService()
metadata_service = MetadataService(repo_context)
duplicate_service = DuplicateDetectionService(repo_context)
generator_service = GeneratorService(repo_context)
git_service = GitSyncService(repo_context)

class AlbumMetadataRequest(BaseModel):
    album_name: str
    year: int = 2026
    musicDirector: str = "Unknown"
    genre: str = "Tollywood Soundtrack"
    language: str = "Telugu"
    country: str = "India"
    releaseDate: Optional[str] = None
    director: Optional[str] = "Unknown"
    producer: Optional[str] = "Unknown"
    banner: Optional[str] = "Unknown"

class RenameSongRequest(BaseModel):
    album_name: str
    old_filename: str
    new_song_title: str
    confirm_id_impact: bool = False

class SyncExecuteRequest(BaseModel):
    album_name: str
    custom_commit_message: Optional[str] = None
    force_continue_on_warning: bool = False

def check_token(x_api_token: Optional[str] = Header(None)):
    required_token = os.environ.get("AGENT_AUTH_TOKEN")
    if required_token and x_api_token != required_token:
        raise HTTPException(status_code=401, detail="Unauthorized Local Agent API call.")

@router.get("/health")
def get_health():
    is_cloud = os.environ.get("CLOUD_MODE") == "1"
    git_info = git_service.get_git_status()
    return {
        "status": "online",
        "mode": "CLOUD" if is_cloud else "LOCAL",
        "repository_root": "Ephemeral Cloud Workspace" if is_cloud else str(repo_context.root),
        "github_repository": f"{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}",
        "git": git_info
    }

@router.get("/directors/autocomplete")
def autocomplete_directors(q: str = Query("", description="Query string for music director")):
    return metadata_service.match_music_director(q)

@router.get("/albums")
def list_albums():
    album_dirs = repo_context.list_all_album_directories()
    result = []
    for album_dir in album_dirs:
        album_name = album_dir.name
        info = metadata_service.load_album_info(album_name)
        has_png = (album_dir / f"{album_name}.png").exists()
        mp3_count = len(list(album_dir.glob("*.mp3")))
        result.append({
            "name": album_name,
            "musicDirector": info.get("musicDirector", "Unknown"),
            "year": info.get("year", 2026),
            "songCount": mp3_count,
            "hasArtwork": has_png
        })
    return {"total_albums": len(result), "albums": result}

@router.get("/albums/{album_name}")
def get_album_details(album_name: str):
    try:
        album_dir = repo_context.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not album_dir.exists():
        raise HTTPException(status_code=404, detail=f"Album '{album_name}' not found.")

    info = metadata_service.load_album_info(album_name)
    artwork_file = album_dir / f"{album_name}.png"
    has_artwork = artwork_file.exists()

    mp3_files = sorted(list(album_dir.glob("*.mp3")))
    songs = []
    for mp3 in mp3_files:
        song_title = mp3.stem
        song_id = repo_context.get_stable_id(f"{album_name}{mp3.name}")
        st = mp3.stat()
        songs.append({
            "title": song_title,
            "filename": mp3.name,
            "song_id": song_id,
            "file_size": st.st_size,
            "audio_url": repo_context.build_raw_audio_url(album_name, mp3.name)
        })

    return {
        "album_name": album_name,
        "metadata": info,
        "has_artwork": has_artwork,
        "artwork_url": repo_context.build_raw_image_url(album_name, f"{album_name}.png") if has_artwork else None,
        "song_count": len(songs),
        "songs": songs
    }

@router.post("/albums/create-or-select")
def create_or_select_album(req: AlbumMetadataRequest, _auth=Depends(check_token)):
    try:
        album_name = repo_context.sanitize_name(req.album_name.strip())
        album_dir = repo_context.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    already_exists = album_dir.exists()

    metadata_service.save_album_info(album_name, req.dict())
    existing_info = metadata_service.load_album_info(album_name)

    return {
        "already_exists": already_exists,
        "album_name": album_name,
        "metadata": existing_info,
        "message": f"Album '{album_name}' selected. Would you like to add more songs?" if already_exists else f"Album '{album_name}' initialized."
    }

@router.post("/upload/artwork/{album_name}")
async def upload_artwork(album_name: str, file: UploadFile = File(...), _auth=Depends(check_token)):
    try:
        album_dir = repo_context.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    content = await file.read()
    try:
        validation_service.validate_png_bytes(content, file.filename)
    except FileValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    album_dir.mkdir(parents=True, exist_ok=True)
    target_png = repo_context.get_image_path(album_name)
    with open(target_png, "wb") as f:
        f.write(content)

    return {
        "success": True,
        "album_name": album_name,
        "saved_filename": f"{album_name}.png",
        "saved_path": str(target_png.relative_to(repo_context.root)),
        "image_url": repo_context.build_raw_image_url(album_name, f"{album_name}.png")
    }

@router.post("/upload/song/{album_name}")
async def upload_song(
    album_name: str,
    desired_song_name: Optional[str] = Form(None),
    file: UploadFile = File(...),
    _auth=Depends(check_token)
):
    try:
        album_dir = repo_context.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    safe_original_filename = repo_context.sanitize_name(file.filename)
    filename_stem = desired_song_name.strip() if desired_song_name and desired_song_name.strip() else Path(safe_original_filename).stem
    filename_stem = repo_context.sanitize_name(filename_stem)

    final_filename = f"{filename_stem}.mp3"
    target_file = repo_context.get_song_path(album_name, final_filename)

    # Use isolated staging dir
    staging_file = repo_context.get_staging_path("session_upload", f"_temp_{safe_original_filename}")
    with open(staging_file, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        audio_specs = validation_service.validate_mp3_file(staging_file)
    except FileValidationError as e:
        if staging_file.exists():
            staging_file.unlink()
        raise HTTPException(status_code=400, detail=str(e))

    dup_analysis = duplicate_service.analyze_uploaded_song(
        album_name=album_name,
        song_filename=final_filename,
        file_size=audio_specs["fileSize"],
        duration_seconds=audio_specs["durationSeconds"],
        temp_file_path=staging_file
    )

    album_dir.mkdir(parents=True, exist_ok=True)
    if target_file.exists():
        target_file.unlink()
    shutil.move(str(staging_file), str(target_file))

    return {
        "success": True,
        "album_name": album_name,
        "original_filename": file.filename,
        "final_filename": final_filename,
        "song_title": filename_stem,
        "song_id": repo_context.get_stable_id(f"{album_name}{final_filename}"),
        "audio_specs": audio_specs,
        "duplicate_analysis": dup_analysis
    }

@router.post("/songs/rename")
def rename_existing_song(req: RenameSongRequest, _auth=Depends(check_token)):
    try:
        album_name = repo_context.sanitize_name(req.album_name)
        old_filename = repo_context.sanitize_name(req.old_filename)
        new_stem = repo_context.sanitize_name(req.new_song_title)

        old_path = repo_context.get_song_path(album_name, old_filename)
        new_filename = f"{new_stem}.mp3"
        new_path = repo_context.get_song_path(album_name, new_filename)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not old_path.exists():
        raise HTTPException(status_code=404, detail=f"Existing song file '{old_filename}' not found.")

    old_song_id = repo_context.get_stable_id(f"{album_name}{old_filename}")
    new_song_id = repo_context.get_stable_id(f"{album_name}{new_filename}")

    if not req.confirm_id_impact:
        return {
            "requires_confirmation": True,
            "warning": f"Renaming '{old_filename}' to '{new_filename}' changes its Song ID from '{old_song_id}' to '{new_song_id}' and alters its Raw GitHub URL.",
            "old_song_id": old_song_id,
            "new_song_id": new_song_id,
            "old_filename": old_filename,
            "new_filename": new_filename
        }

    shutil.move(str(old_path), str(new_path))

    return {
        "success": True,
        "album_name": album_name,
        "old_filename": old_filename,
        "new_filename": new_filename,
        "old_song_id": old_song_id,
        "new_song_id": new_song_id,
        "message": f"Successfully renamed song to '{new_filename}'."
    }

@router.post("/sync/preview/{album_name}")
def sync_preview(album_name: str):
    try:
        album_dir = repo_context.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not album_dir.exists():
        raise HTTPException(status_code=404, detail=f"Album '{album_name}' does not exist.")

    info = metadata_service.load_album_info(album_name)
    has_artwork = (album_dir / f"{album_name}.png").exists()
    mp3_files = sorted(list(album_dir.glob("*.mp3")))

    songs_preview = []
    for mp3 in mp3_files:
        st = mp3.stat()
        songs_preview.append({
            "title": mp3.stem,
            "filename": mp3.name,
            "song_id": repo_context.get_stable_id(f"{album_name}{mp3.name}"),
            "size": st.st_size
        })

    return {
        "album_name": album_name,
        "metadata": info,
        "has_artwork": has_artwork,
        "total_songs": len(songs_preview),
        "songs": songs_preview,
        "git_status": git_service.get_git_status()
    }

@router.post("/sync/execute/{album_name}")
def sync_execute(req: SyncExecuteRequest, _auth=Depends(check_token)):
    try:
        album_name = repo_context.sanitize_name(req.album_name)
        album_dir = repo_context.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not album_dir.exists():
        raise HTTPException(status_code=404, detail=f"Album '{album_name}' does not exist.")

    try:
        gen_result = generator_service.run_generator_pipeline()
    except GeneratorValidationError as e:
        raise HTTPException(status_code=400, detail=f"Metadata Generation Failed: {e}")

    try:
        git_result = git_service.stage_commit_and_push(
            album_name=album_name,
            custom_commit_msg=req.custom_commit_message
        )
    except GitSyncError as e:
        raise HTTPException(status_code=500, detail=f"Git Synchronization Failed: {e}")

    return {
        "status": "COMPLETED",
        "album_name": album_name,
        "generator_summary": gen_result,
        "git_summary": git_result,
        "tunezy_ready": True
    }
