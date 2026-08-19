import os
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, Security, Depends
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from backend.core.config import settings
from backend.core.security import decode_access_token

api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

def verify_agent_token(request: Request, api_key: str = Security(api_key_header)):
    """Enforce token authentication for Local Agent endpoints."""
    token_required = os.environ.get("REQUIRE_AGENT_AUTH", "true").lower() == "true"
    expected_token = os.environ.get("AGENT_AUTH_TOKEN") or settings.SECRET_KEY

    if not token_required:
        return True

    if not api_key or api_key != expected_token:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Local Agent token missing or invalid. Set X-API-Token header."
        )
    return True

def get_current_user(
    request: Request,
    auth_credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
) -> Dict[str, Any]:
    """Enforce JWT session authentication for Cloud API endpoints (returns 401 Unauthorized if invalid)."""
    token: Optional[str] = None

    # Option 1: Bearer Header
    if auth_credentials and auth_credentials.credentials:
        token = auth_credentials.credentials
    # Option 2: Cookie
    elif "musicdata_session" in request.cookies:
        token = request.cookies["musicdata_session"]
    # Option 3: X-API-Token Header
    elif request.headers.get("X-API-Token"):
        token = request.headers.get("X-API-Token")

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized: Authentication required.")

    # Check if token matches agent secret token directly (Local/Admin bypass)
    if token == settings.SECRET_KEY or token == os.environ.get("AGENT_AUTH_TOKEN"):
        return {"sub": "admin", "username": "admin", "role": "admin"}

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or expired session token.")

    return payload
