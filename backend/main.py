import sys
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.core.config import settings
from backend.api.routes import router as local_router
from backend.api.auth_routes import router as auth_router
from backend.api.cloud_routes import router as cloud_job_router, ws_router as cloud_ws_router

app = FastAPI(
    title="MusicData Manager API & Agent (Dual Mode: Cloud + Local)",
    version="2.0.0",
    description="Automated MusicData repository management, metadata generation, and Git synchronization backend."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(local_router)
app.include_router(auth_router)
app.include_router(cloud_job_router)
app.include_router(cloud_ws_router)

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

@app.get("/")
def read_root():
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {
        "app": "MusicData Manager API & Agent",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/api/health"
    }

if __name__ == "__main__":
    print(f"Starting MusicData Manager on http://{settings.HOST}:{settings.API_PORT}")
    uvicorn.run("backend.main:app", host=settings.HOST, port=settings.API_PORT, reload=True)
