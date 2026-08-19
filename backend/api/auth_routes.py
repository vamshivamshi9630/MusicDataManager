import os
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Response, Depends, status
from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.core.security import verify_password, hash_password, create_access_token
from backend.api.auth import get_current_user

router = APIRouter(prefix="/api/auth", tags=["Cloud Authentication"])

class LoginRequest(BaseModel):
    username: str = Field(..., description="Username")
    password: str = Field(..., description="Password")

class UserResponse(BaseModel):
    user_id: str
    username: str
    role: str = "admin"

@router.post("/login")
def login(req: LoginRequest, response: Response) -> Dict[str, Any]:
    expected_username = os.environ.get("ADMIN_USERNAME", "admin")
    expected_password = os.environ.get("ADMIN_PASSWORD", "musicdata2026")

    # Simple secure authentication check
    valid_username = req.username.strip().lower() == expected_username.strip().lower()
    valid_password = req.password == expected_password

    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials."
        )

    user_payload = {"sub": expected_username, "username": expected_username, "role": "admin"}
    token = create_access_token(user_payload, expires_delta_seconds=86400)

    # Set HTTP-only session cookie
    response.set_cookie(
        key="musicdata_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400
    )

    return {
        "success": True,
        "access_token": token,
        "token_type": "bearer",
        "user": UserResponse(user_id=expected_username, username=expected_username)
    }

@router.post("/logout")
def logout(response: Response) -> Dict[str, Any]:
    response.delete_cookie(key="musicdata_session")
    return {"success": True, "message": "Logged out successfully."}

@router.get("/me")
def get_me(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    return {
        "user_id": user.get("sub", "admin"),
        "username": user.get("username", "admin"),
        "role": user.get("role", "admin")
    }
