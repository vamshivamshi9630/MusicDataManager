import os
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Dict, Optional, Any
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Depends, Header
from pydantic import BaseModel

from backend.core.config import settings
from backend.core.repository import RepositoryContext, CloudRepository, PathTraversalError
from backend.services.validation import ValidationService, FileValidationError
from backend.services.metadata import MetadataService
from backend.services.duplicate import DuplicateDetectionService
from backend.services.generator import GeneratorService, GeneratorValidationError
from backend.services.git_sync import GitSyncService, GitSyncError
from backend.services.catalog import cloud_catalog_service
from backend.services.fuzzy_search import search_albums_fuzzy, check_near_duplicate_album

router = APIRouter(prefix="/api")

validation_service = ValidationService()

def is_cloud_mode() -> bool:
    return os.environ.get("CLOUD_MODE", "").strip().lower() in ("1", "true")

def get_repo_context() -> RepositoryContext:
    if is_cloud_mode():
        raise HTTPException(
            status_code=400,
            detail="Local agent repository operations are disabled in Cloud Mode. Please use Cloud Sync API (/api/sync/execute)."
        )
    return RepositoryContext()

def get_metadata_service(rc: Optional[RepositoryContext] = None) -> MetadataService:
    return MetadataService(rc or get_repo_context())

def get_duplicate_service(rc: Optional[RepositoryContext] = None) -> DuplicateDetectionService:
    return DuplicateDetectionService(rc or get_repo_context())

def get_generator_service(rc: Optional[RepositoryContext] = None) -> GeneratorService:
    return GeneratorService(rc or get_repo_context())

def get_git_service(rc: Optional[RepositoryContext] = None) -> GitSyncService:
    return GitSyncService(rc or get_repo_context())

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
    mode: Optional[str] = "add"

class CheckDuplicateRequest(BaseModel):
    album_name: str
    mode: Optional[str] = "add"

class RenameSongRequest(BaseModel):
    album_name: str
    old_filename: str
    new_song_title: str
    confirm_id_impact: bool = False

class SyncExecuteRequest(BaseModel):
    album_name: str
    mode: Optional[str] = "add"
    custom_commit_message: Optional[str] = None
    force_continue_on_warning: bool = False

def check_token(x_api_token: Optional[str] = Header(None)):
    required_token = os.environ.get("AGENT_AUTH_TOKEN")
    if required_token and x_api_token != required_token:
        raise HTTPException(status_code=401, detail="Unauthorized API call.")

@router.get("/health")
def get_health():
    is_cloud = is_cloud_mode()
    if is_cloud:
        git_info = {
            "branch": settings.GITHUB_BRANCH,
            "head": "cloud-ephemeral",
            "remote": f"https://github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}.git",
            "is_clean": True
        }
        repo_root = "Ephemeral Cloud Workspace"
    else:
        try:
            rc = get_repo_context()
            git_info = get_git_service(rc).get_git_status()
            repo_root = str(rc.root)
        except Exception as e:
            git_info = {"branch": "unknown", "error": str(e)}
            repo_root = "Not Discovered"

    return {
        "status": "online",
        "mode": "CLOUD" if is_cloud else "LOCAL",
        "repository_root": repo_root,
        "github_repository": f"{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}",
        "git": git_info
    }

@router.get("/stats")
def get_stats():
    if is_cloud_mode():
        return cloud_catalog_service.get_statistics()
    
    try:
        rc = get_repo_context()
        album_dirs = rc.list_all_album_directories()
        total_albums = len(album_dirs)
        total_songs = sum(len(list(d.glob("*.mp3"))) for d in album_dirs)
        total_png = sum(1 for d in album_dirs if (d / f"{d.name}.png").exists())
        return {
            "albums": total_albums,
            "songs": total_songs,
            "png_artwork": total_png,
            "zero_byte_shield": "Active",
            "mode": "LOCAL"
        }
    except Exception as e:
        return {
            "albums": 0,
            "songs": 0,
            "png_artwork": 0,
            "zero_byte_shield": "Active",
            "mode": "LOCAL",
            "error": str(e)
        }

@router.get("/albums/search")
def search_albums_endpoint(q: str = Query("", description="Query string for fuzzy album search")):
    if is_cloud_mode():
        return cloud_catalog_service.search_albums(q)

    rc = get_repo_context()
    ms = get_metadata_service(rc)
    album_dirs = rc.list_all_album_directories()

    candidates = []
    for d in album_dirs:
        info = ms.load_album_info(d.name)
        candidates.append({
            "name": d.name,
            "musicDirector": info.get("musicDirector", "Unknown"),
            "year": info.get("year", 2026),
            "songCount": len(list(d.glob("*.mp3"))),
            "hasArtwork": (d / f"{d.name}.png").exists()
        })

    matches = search_albums_fuzzy(q, candidates, limit=10)
    return {"query": q, "total_matches": len(matches), "suggestions": matches}

@router.post("/albums/check-duplicate")
def check_duplicate_album_endpoint(req: CheckDuplicateRequest):
    if is_cloud_mode():
        return cloud_catalog_service.check_duplicate_album(req.album_name)

    rc = get_repo_context()
    existing_names = [d.name for d in rc.list_all_album_directories()]
    return check_near_duplicate_album(req.album_name, existing_names)

@router.get("/directors/autocomplete")
def autocomplete_directors(q: str = Query("", description="Query string for music director")):
    if is_cloud_mode():
        return cloud_catalog_service.match_music_director(q)
    rc = get_repo_context()
    ms = get_metadata_service(rc)
    return ms.match_music_director(q)

@router.get("/albums")
def list_albums():
    if is_cloud_mode():
        return cloud_catalog_service.get_albums_list()

    rc = get_repo_context()
    ms = get_metadata_service(rc)
    album_dirs = rc.list_all_album_directories()
    result = []
    for album_dir in album_dirs:
        album_name = album_dir.name
        info = ms.load_album_info(album_name)
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
    if is_cloud_mode():
        albums_idx = cloud_catalog_service.get_albums_index()
        match = next((a for a in albums_idx if a.get("name", "").lower() == album_name.lower()), None)
        if not match:
            raise HTTPException(status_code=404, detail=f"Album '{album_name}' not found in cloud catalog.")
        
        art_name = match.get("name")
        part = match.get("partition", "0-9")
        has_art = bool(match.get("image"))
        art_url = f"https://raw.githubusercontent.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}/{settings.GITHUB_BRANCH}/{part}/{art_name}/{art_name}.png" if has_art else None

        return {
            "album_name": match.get("name"),
            "metadata": {
                "album_name": match.get("name"),
                "musicDirector": match.get("artist", "Unknown"),
                "year": match.get("year", 2026),
                "genre": match.get("genre", "Tollywood Soundtrack"),
                "language": match.get("language", "Telugu")
            },
            "has_artwork": has_art,
            "artwork_url": art_url,
            "song_count": match.get("songCount", 0),
            "songs": []
        }

    rc = get_repo_context()
    ms = get_metadata_service(rc)
    try:
        album_dir = rc.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not album_dir.exists():
        raise HTTPException(status_code=404, detail=f"Album '{album_name}' not found.")

    info = ms.load_album_info(album_name)
    artwork_file = album_dir / f"{album_name}.png"
    has_artwork = artwork_file.exists()

    mp3_files = sorted(list(album_dir.glob("*.mp3")))
    songs = []
    for mp3 in mp3_files:
        song_title = mp3.stem
        song_id = rc.get_stable_id(f"{album_name}{mp3.name}")
        st = mp3.stat()
        songs.append({
            "title": song_title,
            "filename": mp3.name,
            "song_id": song_id,
            "file_size": st.st_size,
            "audio_url": rc.build_raw_audio_url(album_name, mp3.name)
        })

    return {
        "album_name": album_name,
        "metadata": info,
        "has_artwork": has_artwork,
        "artwork_url": rc.build_raw_image_url(album_name, f"{album_name}.png") if has_artwork else None,
        "song_count": len(songs),
        "songs": songs
    }

@router.post("/albums/create-or-select")
def create_or_select_album(req: AlbumMetadataRequest, _auth=Depends(check_token)):
    if req.mode == "add":
        if is_cloud_mode():
            dup_res = cloud_catalog_service.check_duplicate_album(req.album_name)
        else:
            rc = get_repo_context()
            existing_names = [d.name for d in rc.list_all_album_directories()]
            dup_res = check_near_duplicate_album(req.album_name, existing_names)

        if dup_res.get("duplicate"):
            raise HTTPException(
                status_code=400,
                detail=dup_res
            )

    if is_cloud_mode():
        album_name = req.album_name.strip()
        staging_dir = Path(tempfile.gettempdir()) / "musicdata_staging" / album_name
        staging_dir.mkdir(parents=True, exist_ok=True)
        album_info_file = staging_dir / "album_info.json"
        
        metadata_dict = {
            "musicDirector": req.musicDirector if req.musicDirector and req.musicDirector != "Unknown" else (req.director or "Unknown"),
            "year": req.year or 2026,
            "genre": req.genre or "Tollywood Soundtrack",
            "language": req.language or "Telugu",
            "country": req.country or "India",
            "releaseDate": req.releaseDate or f"{req.year or 2026}-01-01"
        }
        
        with open(album_info_file, "w", encoding="utf-8") as f:
            json.dump(metadata_dict, f, indent=2)

        return {
            "success": True,
            "already_exists": False,
            "album_name": album_name,
            "metadata": metadata_dict,
            "message": f"Album '{album_name}' initialized for Cloud Sync."
        }

    rc = get_repo_context()
    ms = get_metadata_service(rc)
    try:
        album_name = req.album_name.strip()
        album_dir = rc.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    already_exists = album_dir.exists()
    album_dir.mkdir(parents=True, exist_ok=True)
    ms.save_album_info(album_name, req.dict())
    existing_info = ms.load_album_info(album_name)

    return {
        "success": True,
        "already_exists": already_exists,
        "album_name": album_name,
        "metadata": existing_info,
        "message": f"Album '{album_name}' selected." if already_exists else f"Album '{album_name}' initialized."
    }

@router.post("/upload/artwork/{album_name}")
async def upload_artwork(album_name: str, file: UploadFile = File(...), _auth=Depends(check_token)):
    content = await file.read()
    try:
        png_bytes = validation_service.process_and_convert_to_png(content, file.filename)
    except FileValidationError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ARTWORK_VALIDATION_ERROR",
                "file": file.filename,
                "message": f"Artwork validation failed: {str(e)}"
            }
        )

    if is_cloud_mode():
        staging_dir = Path(tempfile.gettempdir()) / "musicdata_staging" / album_name
        staging_dir.mkdir(parents=True, exist_ok=True)
        target_png = staging_dir / f"{album_name}.png"
        with open(target_png, "wb") as f:
            f.write(png_bytes)

        return {
            "success": True,
            "album_name": album_name,
            "saved_filename": f"{album_name}.png",
            "saved_path": str(target_png),
            "image_url": None
        }

    rc = get_repo_context()
    try:
        album_dir = rc.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    album_dir.mkdir(parents=True, exist_ok=True)
    target_png = rc.get_image_path(album_name)
    with open(target_png, "wb") as f:
        f.write(png_bytes)

    return {
        "success": True,
        "album_name": album_name,
        "saved_filename": f"{album_name}.png",
        "saved_path": str(target_png.relative_to(rc.root)),
        "image_url": rc.build_raw_image_url(album_name, f"{album_name}.png")
    }

@router.post("/upload/song/{album_name}")
async def upload_song(
    album_name: str,
    desired_song_name: Optional[str] = Form(None),
    file: UploadFile = File(...),
    _auth=Depends(check_token)
):
    if is_cloud_mode():
        staging_dir = Path(tempfile.gettempdir()) / "musicdata_staging" / album_name
        staging_dir.mkdir(parents=True, exist_ok=True)
        
        final_stem = desired_song_name.strip() if desired_song_name and desired_song_name.strip() else Path(file.filename).stem
        final_filename = f"{final_stem}.mp3"
        target_mp3 = staging_dir / final_filename

        with open(target_mp3, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            audio_specs = validation_service.validate_mp3_file(target_mp3)
        except FileValidationError as e:
            if target_mp3.exists():
                target_mp3.unlink()
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "MP3_VALIDATION_ERROR",
                    "file": file.filename,
                    "message": f"MP3 validation failed for file '{file.filename}': {str(e)}"
                }
            )

        return {
            "success": True,
            "album_name": album_name,
            "original_filename": file.filename,
            "final_filename": final_filename,
            "song_title": final_stem,
            "song_id": f"cloud_{final_stem}",
            "audio_specs": audio_specs,
            "duplicate_analysis": {"is_duplicate": False, "reason": "Cloud Staging Active"}
        }

    rc = get_repo_context()
    ds = get_duplicate_service(rc)
    try:
        album_dir = rc.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    safe_original_filename = rc.sanitize_name(file.filename)
    filename_stem = desired_song_name.strip() if desired_song_name and desired_song_name.strip() else Path(safe_original_filename).stem
    filename_stem = rc.sanitize_name(filename_stem)

    final_filename = f"{filename_stem}.mp3"
    target_file = rc.get_song_path(album_name, final_filename)

    staging_file = rc.get_staging_path("session_upload", f"_temp_{safe_original_filename}")
    with open(staging_file, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        audio_specs = validation_service.validate_mp3_file(staging_file)
    except FileValidationError as e:
        if staging_file.exists():
            staging_file.unlink()
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MP3_VALIDATION_ERROR",
                "file": file.filename,
                "message": f"MP3 validation failed for file '{file.filename}': {str(e)}"
            }
        )

    dup_analysis = ds.analyze_uploaded_song(
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
        "song_id": rc.get_stable_id(f"{album_name}{final_filename}"),
        "audio_specs": audio_specs,
        "duplicate_analysis": dup_analysis
    }

@router.post("/sync/execute/{album_name}")
def sync_execute(req: SyncExecuteRequest, _auth=Depends(check_token)):
    album_name = req.album_name.strip()
    mode = req.mode or "add"

    if is_cloud_mode():
        job_id = f"job_cloud_sync_{int(time.time() * 1000)}"
        staging_dir = Path(tempfile.gettempdir()) / "musicdata_staging" / album_name
        try:
            # Provision minimal sparse blobless workspace
            cloud_repo = CloudRepository(job_id=job_id)
            workspace_dir = cloud_repo.provision_blobless_workspace(album_name=album_name)
            
            # Copy staged files into blobless workspace album directory
            target_album_dir = cloud_repo.root / album_name
            target_album_dir.mkdir(parents=True, exist_ok=True)

            if staging_dir.exists():
                for item in staging_dir.iterdir():
                    if item.is_file():
                        shutil.copy2(str(item), str(target_album_dir / item.name))

            # Step 3 & 4: Run Authoritative Generator #1 and Generator #2
            gen_svc = GeneratorService(cloud_repo)
            gen_result = gen_svc.run_generator_pipeline()

            # Step 6, 7 & 8: Git add, commit, and push
            git_svc = GitSyncService(cloud_repo)
            git_result = git_svc.stage_commit_and_push(
                album_name=album_name,
                mode=mode,
                custom_commit_msg=req.custom_commit_message,
                push_enabled=True
            )

            # Cleanup staging & workspace
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            cloud_repo.cleanup_workspace()

            return {
                "success": True,
                "status": "COMPLETED",
                "album_name": album_name,
                "mode": mode,
                "generator_summary": gen_result,
                "git_summary": git_result,
                "tunezy_ready": True
            }
        except Exception as e:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "CLOUD_SYNC_FAILED",
                    "message": f"Cloud Sync Execution Failed: {str(e)}"
                }
            )

    rc = get_repo_context()
    gen_svc = get_generator_service(rc)
    git_svc = get_git_service(rc)
    try:
        album_dir = rc.get_album_path(album_name)
    except PathTraversalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not album_dir.exists():
        raise HTTPException(status_code=404, detail=f"Album '{album_name}' does not exist.")

    # Step 3 & 4: Run Authoritative Generator #1 and Generator #2
    try:
        gen_result = gen_svc.run_generator_pipeline()
    except GeneratorValidationError as e:
        raise HTTPException(status_code=400, detail=f"Metadata Generation Failed: {e}")

    # Step 6, 7 & 8: Git add, commit, and push
    try:
        git_result = git_svc.stage_commit_and_push(
            album_name=album_name,
            mode=mode,
            custom_commit_msg=req.custom_commit_message,
            push_enabled=True
        )
    except GitSyncError as e:
        raise HTTPException(status_code=500, detail=f"Git Synchronization Failed: {e}")

    return {
        "success": True,
        "status": "COMPLETED",
        "album_name": album_name,
        "mode": mode,
        "generator_summary": gen_result,
        "git_summary": git_result,
        "tunezy_ready": True
    }


@router.get("/sync/diagnostics")
def sync_diagnostics(_auth=Depends(check_token)):
    """Return diagnostic info useful for debugging cloud git push and auth issues."""
    try:
        # Use cloud repo context when in cloud mode, else local repo
        if is_cloud_mode():
            rc = RepositoryContext()  # lightweight; don't perform repo operations
            git_svc = GitSyncService(CloudRepository(job_id="diag_check"))
        else:
            rc = get_repo_context()
            git_svc = get_git_service(rc)

        git_status = git_svc.get_git_status()
        remote = git_status.get("remote", "") or ""
        token_env = os.environ.get("GITHUB_TOKEN") or os.environ.get("AGENT_AUTH_TOKEN") or ""
        token_present = bool(token_env)
        token_is_mock = str(token_env).startswith("ghs_mock") if token_present else False

        push_possible = True
        message = "Push appears possible."
        if "github.com" in remote and not token_present and is_cloud_mode():
            push_possible = False
            message = (
                "Remote is GitHub and no GITHUB_TOKEN/AGENT_AUTH_TOKEN detected. "
                "Provide a token or configure a GitHub App as described in docs/GitHub-Deploy.md"
            )

        return {
            "git": git_status,
            "remote": remote,
            "token_present": token_present,
            "token_is_mock": token_is_mock,
            "push_possible": push_possible,
            "message": message
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})
