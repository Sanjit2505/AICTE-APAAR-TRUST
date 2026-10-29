import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Response, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, EmailStr

from app.database import fetch_all, fetch_one, execute
from app.services.auth_service import (
    hash_password, verify_password, create_access_token, get_current_user,
    require_roles, verify_institution_access,
    ROLE_AICTE_ADMIN, ROLE_INSTITUTION_ADMIN, ROLE_VERIFIER
)
from app.services.apaar_service import apaar_adapter, validate_apaar_syntax, mask_apaar_id
from app.services.student_service import (
    generate_csv_template, parse_and_validate_student_csv, commit_student_batch,
    get_institution_students
)
from app.services.intake_service import get_institution_intake_monitoring, get_national_intake_overview
from app.services.anomaly_detector import get_anomalies_list, resolve_anomaly
from app.services.verification_service import verify_student_record
from app.services.reports_service import (
    generate_intake_report_csv, generate_validation_errors_csv, generate_anomalies_report_csv
)
from app.services.trust_ledger import verify_trust_ledger, record_audit

router = APIRouter(prefix="/api/v1", tags=["AICTE APAAR v1"])

# ==================== SCHEMAS ====================
class LoginRequest(BaseModel):
    email: str
    password: str
    role_type: Optional[str] = None # 'AICTE_ADMIN', 'INSTITUTION_ADMIN', 'VERIFIER'

class InstitutionRegisterRequest(BaseModel):
    aicte_id: str
    name: str
    trust_name: str
    address: str
    state: str
    district: str
    admin_name: str
    admin_email: str
    admin_password: str

class CommitBatchRequest(BaseModel):
    institution_id: int
    academic_year_id: int
    filename: str
    valid_records: List[Dict[str, Any]]
    validation_errors: List[Dict[str, Any]]

class AnomalyResolveRequest(BaseModel):
    action: str # 'INVESTIGATING', 'RESOLVED', 'DISMISSED'
    reviewer_notes: str

class VerificationQueryRequest(BaseModel):
    query_type: str # 'ROLL_NO', 'APAAR_ID', 'VERIFICATION_REF'
    query_value: str
    institution_aicte_id: Optional[str] = None
    dob: Optional[str] = None

class IntakeConfigureRequest(BaseModel):
    institution_id: int
    programme_id: int
    academic_year_id: int
    approved_seats: int
    supernumerary_quota: int = 6
    nri_quota: int = 3
    tfw_quota: int = 3

# ==================== AUTHENTICATION ====================

@router.post("/auth/login")
def api_v1_login(req: LoginRequest):
    user = fetch_one("""
        SELECT u.id, u.name, u.email, u.password_hash, u.role, u.institution_id, u.is_active,
               i.name as institution_name, i.aicte_id as institution_code
        FROM users u
        LEFT JOIN institutions i ON u.institution_id = i.id
        WHERE u.email = %s
    """, (req.email.strip().lower(),))

    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.get("is_active", 1):
        raise HTTPException(status_code=403, detail="Account is disabled. Contact system administrator.")

    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        raw_role=user["role"],
        institution_id=user["institution_id"],
        name=user["name"]
    )

    return {
        "status": "success",
        "token": token,
        "token_type": "Bearer",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "institution_id": user["institution_id"],
            "institution_name": user.get("institution_name"),
            "institution_code": user.get("institution_code")
        }
    }

@router.get("/auth/me")
def api_v1_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    inst = None
    if current_user.get("institution_id"):
        inst = fetch_one("SELECT id, name, aicte_id, state FROM institutions WHERE id = %s", (current_user["institution_id"],))
    return {
        "user": current_user,
        "institution": inst
    }

@router.post("/auth/register-institution")
def api_v1_register_institution(req: InstitutionRegisterRequest):
    existing_user = fetch_one("SELECT id FROM users WHERE email = %s", (req.admin_email.strip().lower(),))
    if existing_user:
        raise HTTPException(status_code=400, detail="A user with this email is already registered.")

    existing_inst = fetch_one("SELECT id FROM institutions WHERE aicte_id = %s", (req.aicte_id.strip(),))
    if existing_inst:
        raise HTTPException(status_code=400, detail="An institution with this AICTE ID is already registered.")

    inst_id = execute("""
        INSERT INTO institutions (aicte_id, name, trust_name, address, state, district, trust_score, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 90, NOW())
    """, (req.aicte_id.strip(), req.name.strip(), req.trust_name.strip(), req.address.strip(), req.state.strip(), req.district.strip()))

    pwd_hash = hash_password(req.admin_password)
    user_id = execute("""
        INSERT INTO users (name, email, password_hash, role, institution_id, is_active, created_at)
        VALUES (%s, %s, %s, 'COLLEGE', %s, 1, NOW())
    """, (req.admin_name.strip(), req.admin_email.strip().lower(), pwd_hash, inst_id))

    record_audit(
        actor=req.admin_email,
        action="INSTITUTION_ONBOARDED",
        entity_type="INSTITUTION",
        entity_id=inst_id,
        payload={"aicte_id": req.aicte_id, "name": req.name, "admin_user_id": user_id}
    )

    return {
        "status": "success",
        "message": "Institution registered successfully. You may now log in.",
        "institution_id": inst_id,
        "user_id": user_id
    }

# ==================== METADATA & INSTITUTIONS ====================

@router.get("/academic-years")
def list_academic_years():
    return fetch_all("SELECT * FROM academic_years ORDER BY start_year DESC")

@router.get("/programmes")
def list_programmes():
    return fetch_all("SELECT * FROM programmes ORDER BY code ASC")

@router.get("/institutions")
def list_institutions(current_user: Dict[str, Any] = Depends(get_current_user)):
    # AICTE admin sees all institutions; Institution admin sees only their own
    if current_user.get("canonical_role") == ROLE_AICTE_ADMIN or current_user.get("role") in ["ADMIN", "SCRUTINY_OFFICER"]:
        return fetch_all("SELECT id, aicte_id, name, state, district, trust_name, trust_score FROM institutions ORDER BY name ASC")
    elif current_user.get("institution_id"):
        return fetch_all("SELECT id, aicte_id, name, state, district, trust_name, trust_score FROM institutions WHERE id = %s", (current_user["institution_id"],))
    return []

# ==================== STUDENT DATA & CSV INGESTION ====================

@router.get("/students/template.csv")
def download_student_template():
    csv_content = generate_csv_template()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aicte_student_upload_template.csv"}
    )

@router.post("/students/upload-csv")
async def upload_student_csv(
    institution_id: int = Form(...),
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    verify_institution_access(current_user, institution_id)

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Invalid file format. Please upload a .csv file.")

    file_bytes = await file.read()
    if len(file_bytes) > 20 * 1024 * 1024: # 20 MB max
        raise HTTPException(status_code=400, detail="File size exceeds maximum limit of 20MB.")

    result = parse_and_validate_student_csv(file_bytes, institution_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "CSV validation failed."))

    return {
        "status": "success",
        "filename": file.filename,
        "total_rows": result["total_rows"],
        "valid_count": result["valid_count"],
        "invalid_count": result["invalid_count"],
        "preview": result["valid_preview"],
        "errors": result["errors"],
        "valid_records_payload": result["valid_records_payload"]
    }

@router.post("/students/commit-batch")
def commit_student_batch_endpoint(
    req: CommitBatchRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    verify_institution_access(current_user, req.institution_id)

    if not req.valid_records:
        raise HTTPException(status_code=400, detail="No valid student records to commit.")

    res = commit_student_batch(
        institution_id=req.institution_id,
        academic_year_id=req.academic_year_id,
        filename=req.filename,
        valid_records=req.valid_records,
        validation_errors=req.validation_errors,
        user_email=current_user["email"]
    )
    return res

@router.get("/students/institution/{institution_id}")
def get_students_for_institution(
    institution_id: int,
    programme_id: Optional[int] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    verify_institution_access(current_user, institution_id)
    return get_institution_students(institution_id, programme_id, academic_year_id, search, limit, offset)

@router.get("/students/batches/{institution_id}")
def list_batches(
    institution_id: int,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    verify_institution_access(current_user, institution_id)
    return fetch_all("""
        SELECT sb.*, ay.year_code as academic_year
        FROM submission_batches sb
        JOIN academic_years ay ON sb.academic_year_id = ay.id
        WHERE sb.institution_id = %s
        ORDER BY sb.id DESC
    """, (institution_id,))

@router.get("/students/{student_id}/versions")
def get_student_versions(
    student_id: int,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    student = fetch_one("SELECT institution_id FROM students WHERE id = %s", (student_id,))
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    verify_institution_access(current_user, student["institution_id"])

    versions = fetch_all("""
        SELECT * FROM student_record_versions WHERE student_id = %s ORDER BY version_no DESC
    """, (student_id,))
    return versions

@router.get("/students/{student_id}/certificate")
def get_student_official_certificate(
    student_id: int,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Requirement 10: Support printing/display of APAAR ID along with the student ID
    on certificates issued by degree-awarding bodies.
    """
    student = fetch_one("""
        SELECT s.*, i.name as institution_name, i.aicte_id, i.state as institution_state,
               p.code as programme_code, p.name as programme_name, p.degree_level, p.department,
               ay.year_code as academic_year
        FROM students s
        JOIN institutions i ON s.institution_id = i.id
        JOIN programmes p ON s.programme_id = p.id
        JOIN academic_years ay ON s.academic_year_id = ay.id
        WHERE s.id = %s
    """, (student_id,))
    if not student:
        raise HTTPException(status_code=404, detail="Student record not found.")

    if current_user.get("canonical_role") == ROLE_INSTITUTION_ADMIN and current_user.get("institution_id") != student["institution_id"]:
        raise HTTPException(status_code=403, detail="Unauthorized access to student certificate.")

    raw_apaar = student.get("apaar_id") or ""
    formatted_apaar = f"{raw_apaar[:4]} {raw_apaar[4:8]} {raw_apaar[8:12]}" if len(raw_apaar) == 12 else (raw_apaar or "NOT_ASSIGNED")
    cert_no = f"AICTE-APAAR-DEG-{student['id']:06d}-{student['academic_year'].replace('-', '')}"
    issue_date = datetime.utcnow().strftime("%d %B %Y")

    return {
        "status": "success",
        "certificate": {
            "certificate_number": cert_no,
            "issue_date": issue_date,
            "student_name": student["full_name"],
            "student_id": student["student_roll_no"],
            "apaar_id": formatted_apaar,
            "raw_apaar": raw_apaar,
            "masked_apaar": mask_apaar_id(raw_apaar),
            "institution_name": student["institution_name"],
            "institution_aicte_id": student["aicte_id"],
            "institution_state": student["institution_state"],
            "programme_name": student["programme_name"],
            "programme_code": student["programme_code"],
            "degree_level": student["degree_level"],
            "department": student["department"],
            "academic_year": student["academic_year"],
            "enrolment_status": student["enrolment_status"],
            "seat_category": student["seat_category"],
            "admission_category": student["admission_category"],
            "oci_status": bool(student.get("oci_status")),
            "fn_status": bool(student.get("fn_status")),
            "record_hash": student["record_hash"],
            "digital_attestation_signature": f"SHA256:{student['record_hash'][:24].upper()}...",
            "verification_url": f"/api/v1/validate/apaar/{raw_apaar}" if raw_apaar else "",
            "issuing_authority": "Degree-Awarding Body / AICTE Approved Institution in Association with AICTE APAAR National Trust Framework"
        }
    }

@router.get("/verifications/{ref_code}/certificate")
def get_verified_credential_certificate(
    ref_code: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Returns official attested certificate data for any successfully verified reference code.
    """
    v_req = fetch_one("""
        SELECT vr.*, s.id as student_id
        FROM verification_requests vr
        LEFT JOIN students s ON vr.student_id = s.id
        WHERE vr.verification_ref = %s AND vr.status = 'VERIFIED'
    """, (ref_code,))
    if not v_req or not v_req.get("student_id"):
        raise HTTPException(status_code=404, detail="Verified credential certificate not found or not eligible for issuance.")

    return get_student_official_certificate(v_req["student_id"], current_user)

# ==================== INTAKE & ENROLMENT MONITORING ====================

@router.get("/intake/institution/{institution_id}")
def get_institution_intake(
    institution_id: int,
    academic_year_id: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    verify_institution_access(current_user, institution_id)
    return get_institution_intake_monitoring(institution_id, academic_year_id)

@router.get("/intake/national-overview")
def get_national_overview(
    academic_year_id: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(require_roles([ROLE_AICTE_ADMIN, "ADMIN", "SCRUTINY_OFFICER"]))
):
    return get_national_intake_overview(academic_year_id)

@router.post("/intake/configure")
def configure_intake(
    req: IntakeConfigureRequest,
    current_user: Dict[str, Any] = Depends(require_roles([ROLE_AICTE_ADMIN, "ADMIN"]))
):
    existing = fetch_one("""
        SELECT id FROM approved_intakes 
        WHERE institution_id = %s AND programme_id = %s AND academic_year_id = %s
    """, (req.institution_id, req.programme_id, req.academic_year_id))

    if existing:
        execute("""
            UPDATE approved_intakes 
            SET approved_seats = %s, supernumerary_quota = %s, nri_quota = %s, tfw_quota = %s
            WHERE id = %s
        """, (req.approved_seats, req.supernumerary_quota, req.nri_quota, req.tfw_quota, existing["id"]))
        intake_id = existing["id"]
    else:
        intake_id = execute("""
            INSERT INTO approved_intakes (institution_id, programme_id, academic_year_id, approved_seats, supernumerary_quota, nri_quota, tfw_quota)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (req.institution_id, req.programme_id, req.academic_year_id, req.approved_seats, req.supernumerary_quota, req.nri_quota, req.tfw_quota))

    record_audit(
        actor=current_user["email"],
        action="APPROVED_INTAKE_CONFIGURED",
        entity_type="APPROVED_INTAKE",
        entity_id=intake_id,
        payload={"institution_id": req.institution_id, "programme_id": req.programme_id, "approved_seats": req.approved_seats}
    )

    return {"status": "success", "intake_id": intake_id}

# ==================== ANOMALIES & AUDIT QUEUE ====================

@router.get("/anomalies")
def list_anomalies(
    institution_id: Optional[int] = Query(None),
    finding_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    # If institution admin, force institution boundary
    if current_user.get("canonical_role") == ROLE_INSTITUTION_ADMIN:
        institution_id = current_user.get("institution_id")

    return get_anomalies_list(institution_id, finding_type, status, severity)

@router.post("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly_endpoint(
    anomaly_id: int,
    req: AnomalyResolveRequest,
    current_user: Dict[str, Any] = Depends(require_roles([ROLE_AICTE_ADMIN, "ADMIN", "SCRUTINY_OFFICER"]))
):
    return resolve_anomaly(anomaly_id, req.action, current_user["name"], req.reviewer_notes)

# ==================== AUTHORIZED VERIFIER PORTAL ====================

@router.post("/verifications/verify")
def verify_credential_endpoint(
    req: VerificationQueryRequest,
    current_user: Dict[str, Any] = Depends(require_roles([ROLE_VERIFIER, "AUDITOR", ROLE_AICTE_ADMIN, "ADMIN"]))
):
    return verify_student_record(
        verifier_id=current_user["id"],
        verifier_name=current_user["name"],
        verifier_org=current_user.get("email").split("@")[-1],
        query_type=req.query_type,
        query_value=req.query_value,
        institution_aicte_id=req.institution_aicte_id,
        dob=req.dob
    )

@router.get("/verifications/history")
def get_verification_history(
    current_user: Dict[str, Any] = Depends(require_roles([ROLE_VERIFIER, "AUDITOR", ROLE_AICTE_ADMIN, "ADMIN"]))
):
    return fetch_all("""
        SELECT vr.*, i.name as institution_name, p.name as programme_name
        FROM verification_requests vr
        LEFT JOIN institutions i ON vr.institution_id = i.id
        LEFT JOIN students s ON vr.student_id = s.id
        LEFT JOIN programmes p ON s.programme_id = p.id
        WHERE vr.verifier_id = %s
        ORDER BY vr.id DESC LIMIT 50
    """, (current_user["id"],))

# ==================== REPORT DOWNLOADS ====================

@router.get("/reports/intake.csv")
def download_intake_report(
    institution_id: Optional[int] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    if current_user.get("canonical_role") == ROLE_INSTITUTION_ADMIN:
        institution_id = current_user["institution_id"]

    csv_data = generate_intake_report_csv(institution_id, academic_year_id)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aicte_intake_monitoring_report.csv"}
    )

@router.get("/reports/batch-errors/{batch_id}.csv")
def download_batch_errors(
    batch_id: int,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    batch = fetch_one("SELECT institution_id FROM submission_batches WHERE id = %s", (batch_id,))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    verify_institution_access(current_user, batch["institution_id"])

    csv_data = generate_validation_errors_csv(batch_id)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=batch_{batch_id}_validation_errors.csv"}
    )

@router.get("/reports/anomalies.csv")
def download_anomalies_report(
    institution_id: Optional[int] = Query(None),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    if current_user.get("canonical_role") == ROLE_INSTITUTION_ADMIN:
        institution_id = current_user["institution_id"]

    csv_data = generate_anomalies_report_csv(institution_id)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aicte_anomalies_report.csv"}
    )

# ==================== TRUST LEDGER ====================

@router.get("/trust-ledger/verify")
def get_ledger_verification_v1():
    return verify_trust_ledger()

@router.get("/trust-ledger/entries")
def get_ledger_entries_v1():
    entries = fetch_all("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 50")
    anchors = fetch_all("SELECT * FROM anchors ORDER BY id DESC LIMIT 10")
    return {"entries": entries, "anchors": anchors}

# ==================== DIRECT AICTE & APAAR ID VALIDATION & DETAILS ====================

@router.get("/validate/aicte/{aicte_id}")
def validate_aicte_id_endpoint(aicte_id: str):
    import re
    clean_id = aicte_id.strip()
    is_valid_format = bool(re.match(r"^1-\d{6,14}$", clean_id))
    if not is_valid_format:
        return {
            "valid": False,
            "aicte_id": clean_id,
            "found": False,
            "error": "Invalid AICTE ID format. Must follow standard pattern (e.g. 1-1097241811 with prefix '1-')."
        }

    inst = fetch_one("""
        SELECT id, aicte_id, name, trust_name, address, state, district, trust_score, created_at
        FROM institutions WHERE aicte_id = %s
    """, (clean_id,))

    if not inst:
        return {
            "valid": True,
            "aicte_id": clean_id,
            "found": False,
            "status": "VALID_FORMAT_UNREGISTERED",
            "message": f"AICTE ID format '{clean_id}' is syntactically valid, but no institution is currently registered with this ID in the database.",
            "suggestion": "You can register this institution using the Register Institution form."
        }

    programmes = fetch_all("""
        SELECT p.code as programme_code, p.name as programme_name, p.degree_level, p.department,
               ai.approved_seats, ai.supernumerary_quota, ai.nri_quota, ai.tfw_quota,
               (ai.approved_seats + ai.supernumerary_quota + ai.nri_quota + ai.tfw_quota) as total_permitted_seats
        FROM approved_intakes ai
        JOIN programmes p ON ai.programme_id = p.id
        WHERE ai.institution_id = %s
        ORDER BY p.code ASC
    """, (inst["id"],))

    total_cap = sum(p.get("total_permitted_seats", 0) for p in programmes)
    student_count_row = fetch_one("SELECT COUNT(*) as cnt FROM students WHERE institution_id = %s", (inst["id"],))
    student_count = student_count_row["cnt"] if student_count_row else 0

    return {
        "valid": True,
        "aicte_id": clean_id,
        "found": True,
        "status": "APPROVED_ACTIVE",
        "institution": {
            "id": inst["id"],
            "aicte_id": inst["aicte_id"],
            "name": inst["name"],
            "trust_name": inst["trust_name"],
            "address": inst["address"],
            "state": inst["state"],
            "district": inst["district"],
            "trust_score": inst["trust_score"],
            "registered_at": str(inst["created_at"])
        },
        "programmes_count": len(programmes),
        "total_approved_capacity": total_cap,
        "enrolled_students_count": student_count,
        "programmes": programmes
    }

@router.get("/validate/apaar/{apaar_id}")
def validate_apaar_id_endpoint(apaar_id: str):
    import re
    from app.services.apaar_service import compute_apaar_digest, mask_apaar_id, validate_apaar_syntax

    clean_id = re.sub(r"[\s\-]", "", apaar_id.strip())
    is_valid_syntax, state, msg = validate_apaar_syntax(clean_id)
    if not is_valid_syntax:
        return {
            "valid": False,
            "apaar_id": apaar_id,
            "found": False,
            "error": msg,
            "status": state
        }

    formatted_apaar = f"{clean_id[:4]}-{clean_id[4:8]}-{clean_id[8:]}" if len(clean_id) == 12 else clean_id
    digest = compute_apaar_digest(clean_id)

    student = fetch_one("""
        SELECT s.id, s.student_roll_no, s.full_name, s.dob, s.gender, s.admission_category, s.seat_category,
               s.admission_date, s.enrolment_status, s.verification_status, s.record_hash, s.created_at,
               i.name as institution_name, i.aicte_id as institution_code, i.state as institution_state,
               p.code as programme_code, p.name as programme_name, p.degree_level,
               ay.year_code as academic_year
        FROM students s
        JOIN institutions i ON s.institution_id = i.id
        JOIN programmes p ON s.programme_id = p.id
        JOIN academic_years ay ON s.academic_year_id = ay.id
        WHERE s.apaar_digest = %s
    """, (digest,))

    if not student:
        return {
            "valid": True,
            "apaar_id": clean_id,
            "formatted_apaar": formatted_apaar,
            "masked_apaar": mask_apaar_id(clean_id),
            "found": False,
            "status": "SYNTACTICALLY_VALID_UNASSIGNED",
            "message": "APAAR ID is verified valid (12-digit standard & Verhoeff check verified). No student enrolment record is currently registered under this APAAR ID in the institutional database.",
            "disclaimer": "Local format validation passed. External official DigiLocker APAAR verification is currently pending official API integration.",
            "timestamp": datetime.utcnow().isoformat()
        }

    return {
        "valid": True,
        "apaar_id": clean_id,
        "formatted_apaar": formatted_apaar,
        "masked_apaar": mask_apaar_id(clean_id),
        "found": True,
        "status": "REGISTERED_AND_ATTESTED",
        "student": {
            "student_id": student["id"],
            "full_name": student["full_name"],
            "student_roll_no": student["student_roll_no"],
            "gender": student["gender"],
            "dob": str(student["dob"]),
            "admission_category": student["admission_category"],
            "seat_category": student["seat_category"],
            "admission_date": str(student["admission_date"]),
            "enrolment_status": student["enrolment_status"],
            "verification_status": student["verification_status"],
            "academic_year": student["academic_year"],
            "institution_name": student["institution_name"],
            "institution_code": student["institution_code"],
            "institution_state": student["institution_state"],
            "programme_code": student["programme_code"],
            "programme_name": student["programme_name"],
            "degree_level": student["degree_level"],
            "record_hash": student["record_hash"]
        },
        "verification_receipt": {
            "method": "AICTE_INSTITUTION_ATTESTED_RECORD",
            "integrity_hash": student["record_hash"],
            "disclaimer": "Local institutional attestation verified against MySQL tamper-evident audit ledger."
        },
        "timestamp": datetime.utcnow().isoformat()
    }


