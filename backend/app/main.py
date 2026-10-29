import os
import hashlib
import json
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import UPLOAD_DIR, BASE_DIR
from app.database import fetch_all, fetch_one, execute, get_db
from app.master_setup import ensure_master_setup
from app.services.trust_ledger import record_audit, verify_trust_ledger, calculate_sha256
from app.services.ocr_extraction import extract_real_pdf_data, calculate_file_hash
from app.services.anomaly_engine import inspect_metadata, check_dates_logic
from app.services.rules_engine import evaluate_application_rules, calculate_readiness_score, calculate_risk_score
from app.services.verification_adapters import (
    FireDepartmentAdapter, UniversityAffiliationAdapter, DigiLockerAdapter, toggle_adapter_status
)
from app.services.graph_service import detect_cross_institution_anomalies
from app.services.rag_copilot import query_handbook_copilot
from app.services.metrics_service import get_evaluation_metrics
from app.api_v1 import router as api_v1_router
from app.init_student_db import seed_student_foundations

app = FastAPI(
    title="AICTE APAAR Student Data Management & Verification Platform",
    description="Centralized AICTE APAAR Student Academic Record Ingestion, Intake Compliance, and Verifiable Credentials Platform",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API v1 Router
app.include_router(api_v1_router)

# Static and Upload mounting
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
FRONTEND_DIR = BASE_DIR.parent / "frontend"
ROOT_DIR = BASE_DIR.parent

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend_static")
    if (FRONTEND_DIR / "css").exists():
        app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="frontend_css")
    if (FRONTEND_DIR / "js").exists():
        app.mount("/js", StaticFiles(directory=str(FRONTEND_DIR / "js")), name="frontend_js")
elif ROOT_DIR.exists():
    if (ROOT_DIR / "css").exists():
        app.mount("/css", StaticFiles(directory=str(ROOT_DIR / "css")), name="frontend_css")
    if (ROOT_DIR / "js").exists():
        app.mount("/js", StaticFiles(directory=str(ROOT_DIR / "js")), name="frontend_js")


@app.on_event("startup")
def startup_event():
    ensure_master_setup()
    try:
        seed_student_foundations()
    except Exception as e:
        print(f"Startup notice on student foundations: {e}")

# Request Models
class CollegeRegisterRequest(BaseModel):
    name: str
    aicte_id: str
    trust_name: str
    email: str
    password: str
    state: str
    district: str
    address: str

class LoginRequest(BaseModel):
    email: str
    password: str
    role_type: str # 'COLLEGE' or 'OFFICER'

class FindingActionRequest(BaseModel):
    action: str
    reason: str
    officer_id: int
    officer_name: str

class ApplicationDecisionRequest(BaseModel):
    decision: str
    reason: str
    officer_name: str

class CopilotQueryRequest(BaseModel):
    query: str

class AdapterToggleRequest(BaseModel):
    source_name: str
    is_active: bool

@app.get("/")
def read_root():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "AICTE Trust Layer Real-Time Engine Active"}

@app.get("/api/health")
def health_check():
    try:
        inst_count = fetch_one("SELECT COUNT(*) as count FROM institutions")["count"] or 0
        app_count = fetch_one("SELECT COUNT(*) as count FROM applications")["count"] or 0
        student_count = fetch_one("SELECT COUNT(*) as count FROM students")["count"] or 0
        programme_count = fetch_one("SELECT COUNT(*) as count FROM programmes")["count"] or 0
        audit_count = fetch_one("SELECT COUNT(*) as count FROM audit_log")["count"] or 0
        return {
            "status": "healthy",
            "system": "AICTE APAAR Student Data Management & Verification Platform",
            "version": "2.0.0",
            "database": "connected (MySQL 8.0)",
            "database_name": "aicte_trust_layer",
            "institutions_count": inst_count,
            "students_count": student_count,
            "programmes_count": programme_count,
            "audit_ledger_entries": audit_count,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "unhealthy", "error": str(e)})

# ==================== REAL AUTHENTICATION & REGISTRATION ====================

@app.post("/api/auth/register-college")
def register_college(req: CollegeRegisterRequest):
    # Check if aicte_id or email already exists
    existing_user = fetch_one("SELECT id FROM users WHERE email = %s", (req.email,))
    if existing_user:
        raise HTTPException(status_code=400, detail="A user with this email address is already registered.")

    existing_inst = fetch_one("SELECT id FROM institutions WHERE aicte_id = %s", (req.aicte_id,))
    if existing_inst:
        raise HTTPException(status_code=400, detail="An institution with this AICTE ID is already registered.")

    # 1. Insert Institution
    inst_id = execute("""
        INSERT INTO institutions (aicte_id, name, trust_name, address, state, district, trust_score, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 85, NOW())
    """, (req.aicte_id, req.name, req.trust_name, req.address, req.state, req.district))

    # 2. Insert College User
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    user_id = execute("""
        INSERT INTO users (name, email, password_hash, role, institution_id, is_active, created_at)
        VALUES (%s, %s, %s, 'COLLEGE', %s, 1, NOW())
    """, (req.name, req.email, pwd_hash, inst_id))

    record_audit(req.email, "INSTITUTION_REGISTERED", "INSTITUTION", inst_id, {
        "name": req.name,
        "aicte_id": req.aicte_id,
        "state": req.state
    })

    return {
        "status": "success",
        "message": "College account registered successfully. You can now log in.",
        "user_id": user_id,
        "institution_id": inst_id
    }

@app.post("/api/auth/login")
def login(req: LoginRequest):
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    user = fetch_one("""
        SELECT u.id, u.name, u.email, u.role, u.institution_id, i.name as institution_name, i.aicte_id, i.trust_name
        FROM users u
        LEFT JOIN institutions i ON u.institution_id = i.id
        WHERE u.email = %s AND u.password_hash = %s
    """, (req.email, pwd_hash))

    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    # Verify role type matches
    if req.role_type == "COLLEGE" and user["role"] != "COLLEGE":
        raise HTTPException(status_code=403, detail="This account does not have College credentials.")
    if req.role_type == "OFFICER" and user["role"] not in ["SCRUTINY_OFFICER", "SENIOR_OFFICER", "ADMIN"]:
        raise HTTPException(status_code=403, detail="This account is not authorized as an AICTE Scrutiny Officer.")

    token = f"token_{user['id']}_{user['role']}_{int(datetime.utcnow().timestamp())}"
    return {
        "status": "success",
        "token": token,
        "user": user
    }

# ==================== COLLEGE APPLICATION MANAGEMENT ====================

@app.get("/api/college/checklist")
def get_checklist():
    items = fetch_all("SELECT * FROM checklist_items WHERE checklist_id = 1 ORDER BY id ASC")
    return items

@app.get("/api/college/active-application")
def get_college_application(institution_id: int = Query(...)):
    app_row = fetch_one("""
        SELECT * FROM applications 
        WHERE institution_id = %s 
        ORDER BY id DESC LIMIT 1
    """, (institution_id,))

    if not app_row:
        return {"has_application": False}

    app_id = app_row["id"]
    docs = fetch_all("SELECT * FROM documents WHERE application_id = %s ORDER BY id ASC", (app_id,))
    findings = fetch_all("SELECT * FROM findings WHERE application_id = %s ORDER BY id ASC", (app_id,))
    
    # Get extractions for each document
    for d in docs:
        d["extractions"] = fetch_all("SELECT field_name, value FROM extractions WHERE document_id = %s", (d["id"],))

    return {
        "has_application": True,
        "application": app_row,
        "documents": docs,
        "findings": findings
    }

@app.post("/api/college/submit-details")
def submit_college_details(
    institution_id: int = Form(...),
    applied_intake: int = Form(...),
    sanctioned_intake: int = Form(...),
    faculty_count: int = Form(...),
    total_students: int = Form(...),
    computer_count: int = Form(...),
    category_id: int = Form(1)
):
    form_data = {
        "applied_intake": applied_intake,
        "sanctioned_intake": sanctioned_intake,
        "faculty_count": faculty_count,
        "total_students": total_students,
        "computer_count": computer_count
    }

    # Check if there is an existing draft application
    existing_app = fetch_one("""
        SELECT id FROM applications 
        WHERE institution_id = %s AND status IN ('DRAFT', 'SUBMITTED')
        ORDER BY id DESC LIMIT 1
    """, (institution_id,))

    import json
    form_json = json.dumps(form_data)
    version_hash = calculate_sha256(form_json)

    if existing_app:
        app_id = existing_app["id"]
        execute("""
            INSERT INTO application_versions (application_id, version_no, form_data, submission_hash, created_at)
            VALUES (%s, 1, %s, %s, NOW())
        """, (app_id, form_json, version_hash))
    else:
        app_id = execute("""
            INSERT INTO applications (institution_id, category_id, status, readiness_score, risk_score, current_version, created_at)
            VALUES (%s, %s, 'DRAFT', 25, 20, 1, NOW())
        """, (institution_id, category_id))
        execute("""
            INSERT INTO application_versions (application_id, version_no, form_data, submission_hash, created_at)
            VALUES (%s, 1, %s, %s, NOW())
        """, (app_id, form_json, version_hash))

    record_audit(f"institution_{institution_id}", "DETAILS_UPDATED", "APPLICATION", app_id, form_data)
    return {"status": "success", "application_id": app_id, "message": "College parameters registered."}

@app.post("/api/college/upload-checklist-doc")
async def upload_checklist_doc(
    application_id: int = Form(...),
    doc_type: str = Form(...),
    file: UploadFile = File(...)
):
    app_info = fetch_one("""
        SELECT a.*, i.name as institution_name, i.trust_name, i.aicte_id
        FROM applications a
        JOIN institutions i ON a.institution_id = i.id
        WHERE a.id = %s
    """, (application_id,))

    if not app_info:
        raise HTTPException(status_code=404, detail="Application record not found.")

    file_bytes = await file.read()
    file_sha256 = calculate_file_hash(file_bytes)

    # Save real file
    safe_filename = f"app_{application_id}_{int(datetime.utcnow().timestamp())}_{file.filename.replace(' ', '_')}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)
    with open(file_path, "wb") as f:
        f.write(file_bytes)

    # 1. Real PDF parsing and extraction
    extracted = extract_real_pdf_data(file_path, doc_type)

    # 2. Insert into documents table
    doc_id = execute("""
        INSERT INTO documents (application_id, version_id, doc_type, file_key, original_filename, sha256, mime, page_count, uploaded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """, (application_id, app_info["current_version"], doc_type, safe_filename, file.filename, file_sha256, file.content_type, extracted["page_count"]))

    # 3. Insert real extractions into MySQL
    for k, v in extracted["extractions"].items():
        bbox = extracted["bounding_boxes"].get(k)
        execute("""
            INSERT INTO extractions (document_id, field_name, value, confidence, page_no, bbox, method)
            VALUES (%s, %s, %s, %s, %s, %s, 'ocr')
        """, (doc_id, k, str(v), 0.950, 1, str(bbox) if bbox else None))

    # 4. Invoke Multi-Agent Scrutiny Orchestrator
    # Coordinates: Document Classifier, Forensics Agent, Regulatory APH 2024-27 Agent, Graph Check Agent
    form_version = fetch_one("SELECT form_data FROM application_versions WHERE application_id = %s ORDER BY version_no DESC LIMIT 1", (application_id,))
    if form_version and form_version.get("form_data"):
        raw_fd = form_version["form_data"]
        form_data = json.loads(raw_fd) if isinstance(raw_fd, str) else dict(raw_fd)
    else:
        form_data = {}
    form_data["trust_name"] = app_info["trust_name"]

    from app.services.agents.orchestrator import orchestrator
    scrutiny_res = orchestrator.scrutinize_upload(
        application_id=application_id,
        document_id=doc_id,
        institution_id=app_info["institution_id"],
        institution_name=app_info["institution_name"],
        doc_type=doc_type,
        file_path=file_path,
        file_sha256=file_sha256,
        extracted_data=extracted,
        form_data=form_data
    )

    return {
        "status": "success",
        "document_id": doc_id,
        "doc_type": doc_type,
        "file_sha256": file_sha256,
        "extracted_text_preview": extracted["raw_text"][:300] + ("..." if len(extracted["raw_text"]) > 300 else ""),
        "extractions": extracted["extractions"],
        "classification": scrutiny_res["classification"],
        "readiness_score": scrutiny_res["readiness_score"],
        "risk_score": scrutiny_res["risk_score"],
        "audit_receipt": scrutiny_res["audit_receipt"]
    }

# ==================== OFFICER DASHBOARD & DECISION-SUPPORT ====================

@app.get("/api/officer/applications")
def get_officer_queue():
    query = """
        SELECT a.id, a.status, a.decision, a.decision_reason, a.decided_by,
               a.readiness_score, a.risk_score, a.current_version, a.submitted_at, a.created_at,
               i.id as institution_id, i.name as institution_name, i.aicte_id, i.state, i.district, i.trust_score,
               c.code as category_code, c.name as category_name
        FROM applications a
        JOIN institutions i ON a.institution_id = i.id
        JOIN application_categories c ON a.category_id = c.id
        ORDER BY a.risk_score DESC, a.id DESC
    """
    return fetch_all(query)

@app.get("/api/officer/application/{app_id}")
def get_officer_application(app_id: int):
    app_query = """
        SELECT a.*, i.name as institution_name, i.aicte_id, i.trust_name, i.address, i.state, i.district, i.trust_score,
               c.code as category_code, c.name as category_name, c.handbook_ref
        FROM applications a
        JOIN institutions i ON a.institution_id = i.id
        JOIN application_categories c ON a.category_id = c.id
        WHERE a.id = %s
    """
    application = fetch_one(app_query, (app_id,))
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    docs = fetch_all("SELECT * FROM documents WHERE application_id = %s ORDER BY id ASC", (app_id,))
    for d in docs:
        d["extractions"] = fetch_all("SELECT field_name, value FROM extractions WHERE document_id = %s", (d["id"],))

    findings = fetch_all("""
        SELECT f.*, r.code as rule_code, r.handbook_ref, r.description as rule_description
        FROM findings f
        LEFT JOIN rules r ON f.rule_id = r.id
        WHERE f.application_id = %s
        ORDER BY FIELD(f.severity, 'HIGH', 'MEDIUM', 'LOW'), f.id ASC
    """, (app_id,))

    for f in findings:
        f["evidence"] = fetch_all("SELECT * FROM evidence WHERE finding_id = %s", (f["id"],))
        f["actions"] = fetch_all("SELECT * FROM officer_actions WHERE finding_id = %s ORDER BY id DESC", (f["id"],))

    audits = fetch_all("SELECT * FROM audit_log WHERE entity_type = 'APPLICATION' AND entity_id = %s ORDER BY seq DESC", (app_id,))

    return {
        "application": application,
        "documents": docs,
        "findings": findings,
        "audits": audits
    }

@app.post("/api/findings/{finding_id}/action")
def take_finding_action(finding_id: int, req: FindingActionRequest):
    if req.action not in ["ACCEPT", "DISMISS", "ESCALATE"]:
        raise HTTPException(status_code=400, detail="Invalid action")

    status_map = {"ACCEPT": "ACCEPTED", "DISMISS": "DISMISSED", "ESCALATE": "ESCALATED"}
    new_status = status_map[req.action]

    execute("UPDATE findings SET status = %s WHERE id = %s", (new_status, finding_id))
    execute("""
        INSERT INTO officer_actions (finding_id, officer_id, officer_name, action, reason, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
    """, (finding_id, req.officer_id, req.officer_name, req.action, req.reason))

    audit = record_audit(
        actor=req.officer_name,
        action=f"FINDING_{req.action}",
        entity_type="FINDING",
        entity_id=finding_id,
        payload={"action": req.action, "reason": req.reason}
    )
    return {"status": "success", "new_status": new_status, "audit_receipt": audit}

@app.post("/api/officer/decide-application/{app_id}")
def decide_application(app_id: int, req: ApplicationDecisionRequest):
    if req.decision not in ["APPROVED", "REJECTED"]:
        raise HTTPException(status_code=400, detail="Decision must be APPROVED or REJECTED")

    execute("""
        UPDATE applications
        SET status = 'DECIDED', decision = %s, decision_reason = %s, decided_by = %s
        WHERE id = %s
    """, (req.decision, req.reason, req.officer_name, app_id))

    audit = record_audit(
        actor=req.officer_name,
        action=f"APPLICATION_{req.decision}",
        entity_type="APPLICATION",
        entity_id=app_id,
        payload={"decision": req.decision, "reason": req.reason, "officer": req.officer_name}
    )
    return {"status": "success", "decision": req.decision, "audit_entry": audit}

# ==================== TRUST LEDGER & VERIFICATION ====================

@app.get("/api/trust-ledger/verify")
def get_ledger_verification():
    return verify_trust_ledger()

@app.get("/api/trust-ledger/entries")
def get_ledger_entries():
    entries = fetch_all("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 50")
    anchors = fetch_all("SELECT * FROM anchors ORDER BY id DESC LIMIT 10")
    return {"entries": entries, "anchors": anchors}

# ==================== CROSS-INSTITUTION GRAPH ====================

@app.get("/api/graph/cross-institution")
def get_graph_data():
    return detect_cross_institution_anomalies()

# ==================== RAG COPILOT & ADAPTERS ====================

@app.post("/api/copilot/query")
def copilot_query(req: CopilotQueryRequest):
    return query_handbook_copilot(req.query)

@app.get("/api/adapters")
def list_adapters():
    return fetch_all("SELECT * FROM verification_sources ORDER BY id ASC")

@app.post("/api/adapters/toggle")
def toggle_adapter(req: AdapterToggleRequest):
    new_state = toggle_adapter_status(req.source_name, req.is_active)
    return {"source_name": req.source_name, "is_active": new_state, "status": "ONLINE" if new_state else "CIRCUIT_BREAKER_ACTIVE"}

@app.get("/api/metrics")
def get_metrics():
    return get_evaluation_metrics()

# ==================== INFRASTRUCTURE COMPUTER VISION (YOLOv8) ====================

@app.post("/api/college/upload-infra-photo")
async def upload_infra_photo(
    application_id: int = Form(...),
    photo_type: str = Form("Computer Lab"),
    file: UploadFile = File(...)
):
    from app.services.infra_cv_service import infra_cv_service

    app_info = fetch_one("""
        SELECT a.*, i.name as institution_name, i.lat, i.lng
        FROM applications a
        JOIN institutions i ON a.institution_id = i.id
        WHERE a.id = %s
    """, (application_id,))

    if not app_info:
        raise HTTPException(status_code=404, detail="Application not found")

    file_bytes = await file.read()
    safe_name = f"infra_{application_id}_{int(datetime.utcnow().timestamp())}_{file.filename.replace(' ', '_')}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(file_bytes)

    # Run YOLOv8 and EXIF GPS analysis
    analysis = infra_cv_service.analyze_infra_photo(
        photo_path=file_path,
        campus_lat=float(app_info["lat"]) if app_info["lat"] else None,
        campus_lng=float(app_info["lng"]) if app_info["lng"] else None
    )

    # Record finding if GPS discrepancy or insufficient indicators
    if not analysis["gps_match"]:
        execute("""
            INSERT INTO findings (application_id, source_type, severity, title, reason, confidence, status, created_at)
            VALUES (%s, 'INFRA', 'HIGH', 'Infrastructure Photo Geotag Location Mismatch', %s, 0.95, 'OPEN', NOW())
        """, (application_id, analysis["advisory"]))

    record_audit(
        actor=app_info["institution_name"],
        action="INFRASTRUCTURE_PHOTO_ANALYZED",
        entity_type="APPLICATION",
        entity_id=application_id,
        payload={
            "photo_type": photo_type,
            "filename": file.filename,
            "indicators": analysis["detected_indicators"],
            "gps_match": analysis["gps_match"]
        }
    )

    return {
        "status": "success",
        "photo_file": safe_name,
        "photo_type": photo_type,
        "analysis": analysis
    }

