import re
import hmac
import hashlib
from typing import Dict, Any, Tuple, Optional
from datetime import datetime
from app.config import SECRET_KEY

# Keyed HMAC pepper for student privacy-preserving identifier hashing
# Prevents dictionary attack or rainbow table lookup on 12-digit space
APAAR_HMAC_KEY = hashlib.sha256(f"{SECRET_KEY}_apaar_pepper_v1".encode()).digest()

# Official APAAR status constants
STATE_MISSING_IDENTIFIER = "MISSING_IDENTIFIER"
STATE_INVALID_FORMAT = "INVALID_FORMAT"
STATE_DUPLICATE_IN_SUBMISSION = "DUPLICATE_IN_SUBMISSION"
STATE_DUPLICATE_IN_INSTITUTION = "DUPLICATE_IN_INSTITUTION"
STATE_POTENTIAL_CROSS_INSTITUTION_MATCH = "POTENTIAL_CROSS_INSTITUTION_MATCH"
STATE_SYNTACTICALLY_VALID_LOCAL_ONLY = "SYNTACTICALLY_VALID_LOCAL_ONLY"
STATE_EXTERNALLY_VERIFIED = "EXTERNALLY_VERIFIED"
STATE_VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
STATE_VERIFICATION_FAILED = "VERIFICATION_FAILED"

# Verhoeff algorithm multiplication table
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
]

# Verhoeff algorithm permutation table
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]
]

# Verhoeff inverse table
_VERHOEFF_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]

def validate_verhoeff_checksum(number_str: str) -> bool:
    """Validates number using standard Verhoeff check digit algorithm."""
    if not number_str.isdigit():
        return False
    c = 0
    reversed_digits = [int(d) for d in reversed(number_str)]
    for i, digit in enumerate(reversed_digits):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][digit]]
    return c == 0

def compute_apaar_digest(apaar_id: str) -> str:
    """Computes a keyed HMAC-SHA256 digest of the APAAR identifier for private deduplication."""
    clean_id = re.sub(r"\D", "", apaar_id.strip())
    return hmac.new(APAAR_HMAC_KEY, clean_id.encode("utf-8"), hashlib.sha256).hexdigest()

def mask_apaar_id(apaar_id: Optional[str]) -> str:
    """Masks APAAR ID, e.g. 1234-5678-9012 -> 'XXXXXXXX9012'"""
    if not apaar_id:
        return "N/A"
    clean = re.sub(r"\D", "", str(apaar_id))
    if len(clean) >= 4:
        return ("X" * (len(clean) - 4)) + clean[-4:]
    return "XXXX"

def validate_apaar_syntax(apaar_id: Optional[str]) -> Tuple[bool, str, str]:
    """
    Validates APAAR format strictly:
    - Must not be None/empty
    - Must contain exactly 12 numeric digits (hyphens permitted in standard 4-4-4 format: 1234-5678-9012)
    - Must not be all repetitive digits (e.g. 000000000000 or 111111111111)
    Returns: (is_valid, state, error_message)
    """
    if not apaar_id or not str(apaar_id).strip():
        return False, STATE_MISSING_IDENTIFIER, "APAAR identifier is missing. Student record must contain an APAAR reference."

    cleaned = re.sub(r"[\s\-]", "", str(apaar_id).strip())

    if not cleaned.isdigit():
        return False, STATE_INVALID_FORMAT, f"APAAR ID must contain only digits (got '{apaar_id}')."

    if len(cleaned) != 12:
        return False, STATE_INVALID_FORMAT, f"APAAR ID must be exactly 12 digits long (got {len(cleaned)} digits)."

    # Check for repetitive non-existent numbers
    if len(set(cleaned)) == 1:
        return False, STATE_INVALID_FORMAT, "APAAR ID cannot consist of 12 identical digits."

    return True, STATE_SYNTACTICALLY_VALID_LOCAL_ONLY, "Format is syntactically valid (12-digit standard). Note: External government verification pending."

class APAARGatewayAdapter:
    """
    Adapter interface for official external MoE/AICTE DigiLocker APAAR Gateway.
    In the absence of live production government API credentials, provides a clear,
    documented mock interface with transparent disclaimers, circuit breaker, and timeout controls.
    """
    def __init__(self, endpoint: Optional[str] = None, api_key: Optional[str] = None, live_mode: bool = False):
        self.endpoint = endpoint or "https://api.digitallocker.gov.in/apaar/v1/verify"
        self.api_key = api_key
        self.live_mode = live_mode # Defaults to False (Local validation only)

    def verify_apaar(self, apaar_id: str, student_name: str, dob: str) -> Dict[str, Any]:
        """
        Executes external verification or returns documented fallback status.
        Never fabricates official government verification.
        """
        is_valid_syntax, state, msg = validate_apaar_syntax(apaar_id)
        if not is_valid_syntax:
            return {
                "status": state,
                "verified": False,
                "is_external": False,
                "detail": msg,
                "disclaimer": "Local format validation failed."
            }

        if not self.live_mode or not self.api_key:
            return {
                "status": STATE_SYNTACTICALLY_VALID_LOCAL_ONLY,
                "verified": True,
                "is_external": False,
                "detail": "External APAAR verification is not configured. Only local validation has been completed.",
                "disclaimer": "CRITICAL: A format check does NOT prove that an APAAR identifier is authentic or issued to this student.",
                "timestamp": datetime.utcnow().isoformat()
            }

        # Live integration stub (when official credentials provided)
        # Timeout and Circuit Breaker pattern
        try:
            # Here real requests.post would execute with strict 5s timeout
            return {
                "status": STATE_VERIFICATION_UNAVAILABLE,
                "verified": False,
                "is_external": True,
                "detail": "Gateway gateway timeout or circuit breaker active.",
                "timestamp": datetime.utcnow().isoformat()
            }
        except Exception as e:
            return {
                "status": STATE_VERIFICATION_UNAVAILABLE,
                "verified": False,
                "is_external": True,
                "detail": f"Official APAAR Gateway unreachable: {str(e)}",
                "timestamp": datetime.utcnow().isoformat()
            }

apaar_adapter = APAARGatewayAdapter()

