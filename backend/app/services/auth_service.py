import os
import hmac
import hashlib
import json
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from fastapi import HTTPException, Security, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import SECRET_KEY, ALGORITHM
from app.database import fetch_one

security = HTTPBearer(auto_error=False)

SALT_BYTES = 16
PBKDF2_ITERATIONS = 100_000

# Canonical Roles
ROLE_AICTE_ADMIN = "AICTE_ADMIN"
ROLE_INSTITUTION_ADMIN = "INSTITUTION_ADMIN"
ROLE_VERIFIER = "VERIFIER"

ROLE_MAPPING = {
    "ADMIN": ROLE_AICTE_ADMIN,
    "SCRUTINY_OFFICER": ROLE_AICTE_ADMIN,
    "SENIOR_OFFICER": ROLE_AICTE_ADMIN,
    "COLLEGE": ROLE_INSTITUTION_ADMIN,
    "INSTITUTION_ADMIN": ROLE_INSTITUTION_ADMIN,
    "AUDITOR": ROLE_VERIFIER,
    "VERIFIER": ROLE_VERIFIER,
}

def hash_password(password: str) -> str:
    """Produces a secure PBKDF2-HMAC-SHA256 salted hash: pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>"""
    salt = os.urandom(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"

def verify_password(password: str, hashed_value: str) -> bool:
    """Verifies a password against PBKDF2 hash or legacy SHA-256 hash."""
    if hashed_value.startswith("pbkdf2_sha256$"):
        parts = hashed_value.split("$")
        if len(parts) == 4:
            iters = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = bytes.fromhex(parts[3])
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
            return hmac.compare_digest(actual, expected)
    # Fallback to legacy SHA-256 for backward compatibility with initial prototype seeds
    legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy_hash, hashed_value)

def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def base64url_decode(data: str) -> bytes:
    padding = "=" * (4 - (len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)

def create_access_token(user_id: int, email: str, raw_role: str, institution_id: Optional[int], name: str, expires_delta: Optional[timedelta] = None) -> str:
    """Creates a cryptographically signed HMAC-SHA256 JWT token."""
    canonical_role = ROLE_MAPPING.get(raw_role, raw_role)
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=12))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email,
        "role": canonical_role,
        "raw_role": raw_role,
        "institution_id": institution_id,
        "name": name,
        "exp": int(expire.timestamp()),
        "iat": int(datetime.utcnow().timestamp())
    }

    encoded_header = base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
    signature = hmac.new(SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
    encoded_signature = base64url_encode(signature)

    return f"{encoded_header}.{encoded_payload}.{encoded_signature}"

def decode_access_token(token: str) -> Dict[str, Any]:
    """Validates signature and expiration of JWT token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token structure")

        encoded_header, encoded_payload, encoded_signature = parts
        signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), signing_input, hashlib.sha256).digest()
        actual_sig = base64url_decode(encoded_signature)

        if not hmac.compare_digest(actual_sig, expected_sig):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature")

        payload = json.loads(base64url_decode(encoded_payload).decode("utf-8"))
        if payload.get("exp") and datetime.utcnow().timestamp() > payload["exp"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")

        return payload
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed or unreadable token")

async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Security(security)) -> Dict[str, Any]:
    """Extracts and verifies current user from Bearer token."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization credentials required")

    payload = decode_access_token(credentials.credentials)
    user = fetch_one("SELECT id, name, email, role, institution_id, is_active FROM users WHERE id = %s", (payload["user_id"],))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.get("is_active", 1):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    user["canonical_role"] = ROLE_MAPPING.get(user["role"], user["role"])
    return user

def require_roles(allowed_roles: List[str]):
    """Enforces role-based authorization for endpoint access."""
    async def role_checker(current_user: Dict[str, Any] = Depends(get_current_user)):
        user_role = current_user.get("canonical_role") or current_user.get("role")
        if user_role not in allowed_roles and current_user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Role '{user_role}' lacks required permissions ({', '.join(allowed_roles)})"
            )
        return current_user
    return role_checker

def verify_institution_access(current_user: Dict[str, Any], institution_id: int):
    """Enforces institution data boundary."""
    role = current_user.get("canonical_role")
    if role == ROLE_AICTE_ADMIN:
        return True # AICTE Admin can inspect any institution
    if role == ROLE_INSTITUTION_ADMIN:
        if current_user.get("institution_id") != institution_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: You are not authorized to view or mutate records for another institution."
            )
        return True
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized role for institution record access.")
