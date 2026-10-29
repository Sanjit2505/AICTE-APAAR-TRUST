import os
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from fastapi import HTTPException, status
from app.database import get_db, fetch_one, fetch_all, execute
from app.services.trust_ledger import record_audit, calculate_sha256
from app.services.apaar_service import mask_apaar_id, compute_apaar_digest

# In-memory sliding window rate limiter per verifier user
_VERIFIER_RATE_LIMIT = {} # user_id -> [timestamps]
MAX_QUERIES_PER_WINDOW = 30
WINDOW_SECONDS = 600 # 10 minutes

def check_verifier_rate_limit(user_id: int):
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=WINDOW_SECONDS)
    history = _VERIFIER_RATE_LIMIT.get(user_id, [])
    # Keep only timestamps in current window
    history = [ts for ts in history if ts > window_start]
    if len(history) >= MAX_QUERIES_PER_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Verification rate limit exceeded ({MAX_QUERIES_PER_WINDOW} requests / 10 minutes). Please wait before querying again."
        )
    history.append(now)
    _VERIFIER_RATE_LIMIT[user_id] = history

def mask_student_name(full_name: str) -> str:
    """Masks name for privacy preservation: 'Aarav Sharma' -> 'A**** S*****'"""
    parts = full_name.split()
    masked = []
    for p in parts:
        if len(p) <= 2:
            masked.append(p[0] + "*")
        else:
            masked.append(p[0] + ("*" * (len(p) - 1)))
    return " ".join(masked)

def verify_student_record(
    verifier_id: int,
    verifier_name: str,
    verifier_org: str,
    query_type: str, # 'ROLL_NO' or 'APAAR_ID' or 'VERIFICATION_REF'
    query_value: str,
    institution_aicte_id: Optional[str] = None,
    dob: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes authorized student credential verification with strict data minimization.
    Does NOT leak personal contact details, raw APAAR identifiers, or address data.
    """
    check_verifier_rate_limit(verifier_id)

    query_value = query_value.strip()

    student = None
    inst = None

    if query_type == "VERIFICATION_REF":
        prev_req = fetch_one("""
            SELECT vr.*, s.full_name, s.enrolment_status, s.record_hash,
                   i.name as institution_name, i.aicte_id as institution_code,
                   p.name as programme_name, p.degree_level,
                   ay.year_code as academic_year
            FROM verification_requests vr
            LEFT JOIN students s ON vr.student_id = s.id
            LEFT JOIN institutions i ON vr.institution_id = i.id
            LEFT JOIN programmes p ON s.programme_id = p.id
            LEFT JOIN academic_years ay ON s.academic_year_id = ay.id
            WHERE vr.verification_ref = %s
        """, (query_value,))
        
        if not prev_req:
            return {
                "status": "NOT_FOUND",
                "detail": "Verification Reference not recognized in the AICTE Trust Registry.",
                "verified": False,
                "timestamp": datetime.utcnow().isoformat()
            }
        return {
            "status": "VERIFIED",
            "verified": True,
            "verification_ref": prev_req["verification_ref"],
            "verification_hash": prev_req["verification_hash"],
            "student_masked_name": mask_student_name(prev_req["full_name"]),
            "institution_name": prev_req["institution_name"],
            "institution_code": prev_req["institution_code"],
            "programme_name": prev_req["programme_name"],
            "degree_level": prev_req["degree_level"],
            "academic_year": prev_req["academic_year"],
            "enrolment_status": prev_req["enrolment_status"],
            "original_verified_at": prev_req["created_at"].isoformat() if hasattr(prev_req["created_at"], "isoformat") else str(prev_req["created_at"])
        }

    elif query_type == "ROLL_NO":
        if not institution_aicte_id:
            raise HTTPException(status_code=400, detail="Institution AICTE ID is required when verifying by student roll number.")

        inst = fetch_one("SELECT id, name, aicte_id FROM institutions WHERE aicte_id = %s", (institution_aicte_id.strip(),))
        if not inst:
            raise HTTPException(status_code=404, detail=f"Institution with AICTE ID '{institution_aicte_id}' not found.")

        student = fetch_one("""
            SELECT s.*, p.name as programme_name, p.degree_level, ay.year_code as academic_year, i.name as institution_name, i.aicte_id as institution_code
            FROM students s
            JOIN programmes p ON s.programme_id = p.id
            JOIN academic_years ay ON s.academic_year_id = ay.id
            JOIN institutions i ON s.institution_id = i.id
            WHERE s.institution_id = %s AND s.student_roll_no = %s
        """, (inst["id"], query_value))

    elif query_type == "APAAR_ID":
        digest = compute_apaar_digest(query_value)
        student = fetch_one("""
            SELECT s.*, p.name as programme_name, p.degree_level, ay.year_code as academic_year, i.name as institution_name, i.aicte_id as institution_code
            FROM students s
            JOIN programmes p ON s.programme_id = p.id
            JOIN academic_years ay ON s.academic_year_id = ay.id
            JOIN institutions i ON s.institution_id = i.id
            WHERE s.apaar_digest = %s
        """, (digest,))

    now = datetime.utcnow()
    ref_code = f"VR-{int(now.timestamp())}-{os.urandom(4).hex().upper()}"

    if not student:
        # Record non-match audit for safety
        rec_id = execute("""
            INSERT INTO verification_requests (
                verifier_id, verifier_name, verifier_org, query_type, query_value,
                status, response_scope_json, verification_ref, verification_hash, created_at
            ) VALUES (%s, %s, %s, %s, %s, 'NOT_FOUND', %s, %s, %s, NOW())
        """, (
            verifier_id, verifier_name, verifier_org, query_type, query_value[:30],
            json.dumps({"result": "NOT_FOUND"}), ref_code, calculate_sha256(ref_code)
        ))
        return {
            "status": "NOT_FOUND",
            "verified": False,
            "verification_ref": ref_code,
            "detail": "No matching academic record found in AICTE National Repository.",
            "timestamp": now.isoformat()
        }

    # Verify optional DOB match if provided
    if dob:
        student_dob_str = student["dob"].strftime("%Y-%m-%d") if hasattr(student["dob"], "strftime") else str(student["dob"])
        if student_dob_str != dob.strip():
            execute("""
                INSERT INTO verification_requests (
                    verifier_id, verifier_name, verifier_org, query_type, query_value,
                    institution_id, student_id, status, response_scope_json, verification_ref, verification_hash, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'DISCREPANCY', %s, %s, %s, NOW())
            """, (
                verifier_id, verifier_name, verifier_org, query_type, query_value[:30],
                student["institution_id"], student["id"], json.dumps({"discrepancy": "DOB_MISMATCH"}),
                ref_code, calculate_sha256(ref_code)
            ))
            return {
                "status": "DISCREPANCY",
                "verified": False,
                "verification_ref": ref_code,
                "detail": "Record identifier matched, but date of birth verification failed.",
                "timestamp": now.isoformat()
            }

    # Successful verification with Data Minimization
    scoped_response = {
        "status": "VERIFIED",
        "verified": True,
        "verification_ref": ref_code,
        "student_masked_name": mask_student_name(student["full_name"]),
        "institution_name": student["institution_name"],
        "institution_code": student["institution_code"],
        "programme_name": student["programme_name"],
        "degree_level": student["degree_level"],
        "academic_year": student["academic_year"],
        "enrolment_status": student["enrolment_status"],
        "verification_status": student["verification_status"],
        "record_integrity_hash": student["record_hash"][:16] + "...",
        "verification_method": "AICTE_INSTITUTION_ATTESTED_RECORD",
        "timestamp": now.isoformat()
    }

    verification_hash = calculate_sha256(f"{ref_code}:{student['record_hash']}:{verifier_org}:{now.isoformat()}")
    scoped_response["verification_hash"] = verification_hash

    # Save verification record
    execute("""
        INSERT INTO verification_requests (
            verifier_id, verifier_name, verifier_org, query_type, query_value,
            institution_id, student_id, status, response_scope_json, verification_ref, verification_hash, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'VERIFIED', %s, %s, %s, NOW())
    """, (
        verifier_id, verifier_name, verifier_org, query_type, query_value[:30],
        student["institution_id"], student["id"], json.dumps(scoped_response),
        ref_code, verification_hash
    ))

    # Append to Trust Ledger
    record_audit(
        actor=verifier_name,
        action="CREDENTIAL_VERIFIED",
        entity_type="VERIFICATION",
        entity_id=student["id"],
        payload={
            "verification_ref": ref_code,
            "verifier_org": verifier_org,
            "query_type": query_type,
            "institution_code": student["institution_code"],
            "verification_hash": verification_hash
        }
    )

    return scoped_response

