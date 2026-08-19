import hmac
import hashlib
import base64
import json
import time
import secrets
from typing import Optional, Dict, Any
from backend.core.config import settings

def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def _b64_decode(data: str) -> bytes:
    padded = data + '=' * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode('utf-8'))

def hash_password(password: str, salt: Optional[str] = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()
    return f"{salt}${hashed}"

def verify_password(password: str, hashed_with_salt: str) -> bool:
    try:
        salt, expected_hash = hashed_with_salt.split('$')
        actual_hash = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt.encode('utf-8'),
            100000
        ).hex()
        return hmac.compare_digest(actual_hash, expected_hash)
    except Exception:
        return False

def create_access_token(data: Dict[str, Any], expires_delta_seconds: int = 86400) -> str:
    secret = settings.SECRET_KEY.encode('utf-8')
    header = {"alg": "HS256", "typ": "JWT"}
    payload = data.copy()
    payload["exp"] = int(time.time()) + expires_delta_seconds
    payload["iat"] = int(time.time())

    encoded_header = _b64_encode(json.dumps(header).encode('utf-8'))
    encoded_payload = _b64_encode(json.dumps(payload).encode('utf-8'))
    
    signature_input = f"{encoded_header}.{encoded_payload}".encode('utf-8')
    signature = hmac.new(secret, signature_input, hashlib.sha256).digest()
    encoded_signature = _b64_encode(signature)

    return f"{encoded_header}.{encoded_payload}.{encoded_signature}"

def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None

        encoded_header, encoded_payload, encoded_signature = parts
        secret = settings.SECRET_KEY.encode('utf-8')

        signature_input = f"{encoded_header}.{encoded_payload}".encode('utf-8')
        expected_signature = hmac.new(secret, signature_input, hashlib.sha256).digest()
        actual_signature = _b64_decode(encoded_signature)

        if not hmac.compare_digest(expected_signature, actual_signature):
            return None

        payload = json.loads(_b64_decode(encoded_payload).decode('utf-8'))
        
        if "exp" in payload and time.time() > payload["exp"]:
            return None

        return payload
    except Exception:
        return None
