import io
import csv
import json
import hashlib
from datetime import datetime, date
from typing import List, Dict, Any, Tuple, Optional
from app.database import get_db, fetch_all, fetch_one, execute
from app.services.apaar_service import (
    validate_apaar_syntax, compute_apaar_digest, mask_apaar_id,
    STATE_SYNTACTICALLY_VALID_LOCAL_ONLY, STATE_MISSING_IDENTIFIER, STATE_INVALID_FORMAT
)
from app.services.trust_ledger import record_audit, calculate_sha256
from app.services.anomaly_detector import detect_batch_anomalies

CORE_MANDATORY_HEADERS = [
    "student_roll_no",
    "apaar_id",
    "full_name",
    "dob",
    "gender",
    "programme_code",
    "academic_year",
    "admission_category",
    "seat_category",
    "admission_date",
    "enrolment_status"
]

CSV_HEADERS = [
    "student_roll_no",
    "apaar_id",
    "full_name",
    "dob",
    "gender",
    "programme_code",
    "academic_year",
    "admission_category",
    "seat_category",
    "oci_status",
    "fn_status",
    "admission_date",
    "enrolment_status"
]

VALID_GENDERS = {"MALE", "FEMALE", "OTHER"}
VALID_ADMISSION_CATEGORIES = {"GENERAL", "OBC", "SC", "ST", "EWS", "NRI", "TFW"}
VALID_SEAT_CATEGORIES = {"REGULAR", "SUPERNUMERARY", "NRI", "TFW", "OCI", "FN"}
VALID_ENROLMENT_STATUSES = {"ENROLLED", "COMPLETED", "DROPPED", "SUSPENDED"}

def sanitize_csv_cell(val: Any) -> str:
    """Neutralizes spreadsheet formula injection risks (=, +, -, @)."""
    s = str(val or "").strip()
    if s.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + s
    return s

def generate_csv_template() -> str:
    """Generates standard CSV template content for student onboarding."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    # Realistic synthetic examples
    writer.writerow([
        "2024-CSE-001",
        "123456789012",
        "Aarav Sharma",
        "2004-05-14",
        "MALE",
        "BTECH-CSE",
        "2024-2025",
        "GENERAL",
        "REGULAR",
        "NO",
        "NO",
        "2024-08-01",
        "ENROLLED"
    ])
    writer.writerow([
        "2024-CSE-002",
        "987654321098",
        "Priya Patel",
        "2005-01-22",
        "FEMALE",
        "BTECH-CSE",
        "2024-2025",
        "OBC",
        "REGULAR",
        "NO",
        "NO",
        "2024-08-01",
        "ENROLLED"
    ])
    writer.writerow([
        "2024-AI-001",
        "556677889900",
        "Rohan Verma",
        "2004-11-09",
        "MALE",
        "BTECH-AI-DS",
        "2024-2025",
        "TFW",
        "TFW",
        "NO",
        "NO",
        "2024-08-05",
        "ENROLLED"
    ])
    writer.writerow([
        "2024-CSE-099",
        "778899001122",
        "Zaid Khan",
        "2004-03-19",
        "MALE",
        "BTECH-CSE",
        "2024-2025",
        "NRI",
        "OCI",
        "YES",
        "NO",
        "2024-08-08",
        "ENROLLED"
    ])
    return output.getvalue()

def calculate_student_record_hash(student_dict: Dict[str, Any]) -> str:
    """Calculates canonical SHA-256 hash of immutable and core student fields."""
    canonical_repr = (
        f"{student_dict.get('institution_id')}:"
        f"{student_dict.get('student_roll_no')}:"
        f"{student_dict.get('apaar_digest') or 'NONE'}:"
        f"{student_dict.get('full_name').strip().upper()}:"
        f"{student_dict.get('dob')}:"
        f"{student_dict.get('programme_id')}:"
        f"{student_dict.get('academic_year_id')}:"
        f"{student_dict.get('admission_category')}:"
        f"{student_dict.get('admission_date')}"
    )
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()

def parse_and_validate_student_csv(file_bytes: bytes, institution_id: int) -> Dict[str, Any]:
    """
    Parses and validates uploaded student CSV row-by-row.
    Returns preview summary, validation errors matrix, and validated rows.
    """
    try:
        decoded = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = file_bytes.decode("latin-1")
        except Exception:
            return {
                "success": False,
                "error": "Unable to decode CSV file. Please upload a valid UTF-8 encoded CSV document."
            }

    reader = csv.reader(io.StringIO(decoded))
    rows = list(reader)

    if not rows:
        return {"success": False, "error": "The uploaded CSV file is empty."}

    raw_headers = [h.strip().lower() for h in rows[0]]
    missing_headers = [h for h in CORE_MANDATORY_HEADERS if h not in raw_headers]
    if missing_headers:
        return {
            "success": False,
            "error": f"Missing mandatory CSV columns: {', '.join(missing_headers)}. Required columns are: {', '.join(CORE_MANDATORY_HEADERS)}"
        }

    header_indices = {h: raw_headers.index(h) for h in raw_headers}

    # Fetch reference caches
    progs = {p["code"]: p["id"] for p in fetch_all("SELECT id, code FROM programmes")}
    years = {y["year_code"]: y["id"] for y in fetch_all("SELECT id, year_code FROM academic_years")}
    existing_rolls = {r["student_roll_no"] for r in fetch_all("SELECT student_roll_no FROM students WHERE institution_id = %s", (institution_id,))}
    existing_apaars = {r["apaar_digest"] for r in fetch_all("SELECT apaar_digest FROM students WHERE institution_id = %s AND apaar_digest IS NOT NULL", (institution_id,))}

    valid_records = []
    validation_errors = []
    seen_batch_rolls = set()
    seen_batch_apaars = set()

    for idx, row in enumerate(rows[1:], start=2): # 1-based indexing for header, row 2 is first data line
        if not row or not any(cell.strip() for cell in row):
            continue # skip empty line

        # Extract cells safely
        def get_val(col_name: str) -> str:
            i = header_indices.get(col_name)
            return row[i].strip() if i is not None and i < len(row) else ""

        roll_no = get_val("student_roll_no")
        apaar = get_val("apaar_id")
        name = get_val("full_name")
        dob = get_val("dob")
        gender = get_val("gender").upper()
        prog_code = get_val("programme_code").upper()
        ay_code = get_val("academic_year")
        adm_cat = get_val("admission_category").upper()
        seat_cat = get_val("seat_category").upper() or "REGULAR"
        oci_val = get_val("oci_status").upper()
        fn_val = get_val("fn_status").upper()
        oci_status = 1 if (oci_val in ("1", "TRUE", "YES", "Y") or seat_cat == "OCI") else 0
        fn_status = 1 if (fn_val in ("1", "TRUE", "YES", "Y") or seat_cat == "FN") else 0
        adm_date = get_val("admission_date")
        enrol_status = get_val("enrolment_status").upper() or "ENROLLED"

        row_errors = []

        # 1. Roll No Validation
        if not roll_no:
            row_errors.append({"field": "student_roll_no", "code": "MISSING_FIELD", "msg": "Student Roll Number is required."})
        elif roll_no in seen_batch_rolls:
            row_errors.append({"field": "student_roll_no", "code": "DUPLICATE_IN_BATCH", "msg": f"Duplicate roll number '{roll_no}' within this file."})
        elif roll_no in existing_rolls:
            row_errors.append({"field": "student_roll_no", "code": "DUPLICATE_IN_INSTITUTION", "msg": f"Student with roll number '{roll_no}' already exists in your institution records."})
        else:
            seen_batch_rolls.add(roll_no)

        # 2. APAAR Identifier Validation
        apaar_digest = None
        if not apaar:
            row_errors.append({"field": "apaar_id", "code": "MISSING_APAAR", "msg": "APAAR ID is mandatory for centralized academic tracking."})
        else:
            is_valid_syntax, state, msg = validate_apaar_syntax(apaar)
            if not is_valid_syntax:
                row_errors.append({"field": "apaar_id", "code": state, "msg": msg})
            else:
                apaar_digest = compute_apaar_digest(apaar)
                if apaar_digest in seen_batch_apaars:
                    row_errors.append({"field": "apaar_id", "code": "DUPLICATE_APAAR_IN_BATCH", "msg": "Same APAAR ID appears multiple times in this upload."})
                elif apaar_digest in existing_apaars:
                    row_errors.append({"field": "apaar_id", "code": "DUPLICATE_APAAR_IN_INSTITUTION", "msg": "A student with this APAAR ID is already enrolled in this institution."})
                else:
                    seen_batch_apaars.add(apaar_digest)

        # 3. Full Name Validation
        if not name or len(name) < 2:
            row_errors.append({"field": "full_name", "code": "INVALID_NAME", "msg": "Full name must be at least 2 characters."})

        # 4. DOB Validation
        if not dob:
            row_errors.append({"field": "dob", "code": "MISSING_FIELD", "msg": "Date of Birth is required (format: YYYY-MM-DD)."})
        else:
            try:
                dob_parsed = datetime.strptime(dob, "%Y-%m-%d").date()
                if dob_parsed > date.today():
                    row_errors.append({"field": "dob", "code": "FUTURE_DATE", "msg": "Date of Birth cannot be in the future."})
                elif (date.today().year - dob_parsed.year) < 14:
                    row_errors.append({"field": "dob", "code": "INVALID_AGE", "msg": "Student must be at least 14 years old for higher education admission."})
            except ValueError:
                row_errors.append({"field": "dob", "code": "INVALID_DATE_FORMAT", "msg": "Date of Birth must be in YYYY-MM-DD format."})

        # 5. Gender Validation
        if gender not in VALID_GENDERS:
            row_errors.append({"field": "gender", "code": "INVALID_ENUM", "msg": f"Gender must be one of: {', '.join(VALID_GENDERS)}."})

        # 6. Programme Validation
        prog_id = progs.get(prog_code)
        if not prog_id:
            row_errors.append({"field": "programme_code", "code": "UNKNOWN_PROGRAMME", "msg": f"Unrecognized programme code '{prog_code}'. Supported codes: {', '.join(progs.keys())}."})

        # 7. Academic Year Validation
        ay_id = years.get(ay_code)
        if not ay_id:
            row_errors.append({"field": "academic_year", "code": "UNKNOWN_ACADEMIC_YEAR", "msg": f"Unrecognized academic year '{ay_code}'. Supported years: {', '.join(years.keys())}."})

        # 8. Admission Category
        if adm_cat not in VALID_ADMISSION_CATEGORIES:
            row_errors.append({"field": "admission_category", "code": "INVALID_ENUM", "msg": f"Category must be one of: {', '.join(VALID_ADMISSION_CATEGORIES)}."})

        # 9. Seat Category
        if seat_cat not in VALID_SEAT_CATEGORIES:
            row_errors.append({"field": "seat_category", "code": "INVALID_ENUM", "msg": f"Seat Category must be one of: {', '.join(VALID_SEAT_CATEGORIES)}."})

        # 10. Admission Date
        if not adm_date:
            row_errors.append({"field": "admission_date", "code": "MISSING_FIELD", "msg": "Admission date is required (YYYY-MM-DD)."})
        else:
            try:
                datetime.strptime(adm_date, "%Y-%m-%d")
            except ValueError:
                row_errors.append({"field": "admission_date", "code": "INVALID_DATE_FORMAT", "msg": "Admission date must be YYYY-MM-DD."})

        # 11. Enrolment Status
        if enrol_status not in VALID_ENROLMENT_STATUSES:
            row_errors.append({"field": "enrolment_status", "code": "INVALID_ENUM", "msg": f"Status must be one of: {', '.join(VALID_ENROLMENT_STATUSES)}."})

        raw_row_data = {
            "row_num": idx,
            "student_roll_no": roll_no,
            "apaar_id": apaar,
            "full_name": name,
            "dob": dob,
            "gender": gender,
            "programme_code": prog_code,
            "academic_year": ay_code,
            "admission_category": adm_cat,
            "seat_category": seat_cat,
            "oci_status": oci_status,
            "fn_status": fn_status,
            "admission_date": adm_date,
            "enrolment_status": enrol_status
        }

        if row_errors:
            for err in row_errors:
                validation_errors.append({
                    "row_num": idx,
                    "student_roll_no": roll_no,
                    "field_name": err["field"],
                    "error_code": err["code"],
                    "error_message": err["msg"],
                    "raw_data": raw_row_data
                })
        else:
            valid_records.append({
                "institution_id": institution_id,
                "programme_id": prog_id,
                "academic_year_id": ay_id,
                "student_roll_no": roll_no,
                "apaar_id": apaar,
                "apaar_digest": apaar_digest,
                "full_name": name,
                "dob": dob,
                "gender": gender,
                "admission_category": adm_cat,
                "seat_category": seat_cat,
                "oci_status": oci_status,
                "fn_status": fn_status,
                "admission_date": adm_date,
                "enrolment_status": enrol_status,
                "raw_row": raw_row_data
            })

    total_submitted = len(valid_records) + len(set(e["row_num"] for e in validation_errors))
    return {
        "success": True,
        "total_rows": total_submitted,
        "valid_count": len(valid_records),
        "invalid_count": len(set(e["row_num"] for e in validation_errors)),
        "errors": validation_errors,
        "valid_preview": [
            {
                "student_roll_no": r["student_roll_no"],
                "masked_apaar": mask_apaar_id(r["apaar_id"]),
                "full_name": r["full_name"],
                "gender": r["gender"],
                "programme_code": r["raw_row"]["programme_code"],
                "academic_year": r["raw_row"]["academic_year"],
                "admission_category": r["admission_category"]
            }
            for r in valid_records[:20]
        ],
        "valid_records_payload": valid_records
    }

def commit_student_batch(
    institution_id: int,
    academic_year_id: int,
    filename: str,
    valid_records: List[Dict[str, Any]],
    validation_errors: List[Dict[str, Any]],
    user_email: str
) -> Dict[str, Any]:
    """
    Persists batch transactionally into MySQL:
    - Creates submission_batches entry
    - Inserts student records with SHA-256 hashes and version 1 history
    - Inserts line validation errors for permanent record
    - Triggers automated anomaly detection
    - Appends entry into Trust Ledger cryptographic audit chain
    """
    batch_ref = f"BATCH-{institution_id}-{int(datetime.utcnow().timestamp())}"
    total = len(valid_records) + len(set(e["row_num"] for e in validation_errors))
    valid_count = len(valid_records)
    invalid_count = len(set(e["row_num"] for e in validation_errors))

    with get_db() as conn:
        with conn.cursor() as cursor:
            # 1. Insert Batch Header
            cursor.execute("""
                INSERT INTO submission_batches (
                    batch_ref, institution_id, academic_year_id, filename,
                    total_rows, valid_rows, invalid_rows, status, uploaded_by, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'COMMITTED', %s, NOW())
            """, (batch_ref, institution_id, academic_year_id, filename, total, valid_count, invalid_count, user_email))
            batch_id = cursor.lastrowid

            # 2. Insert Valid Student Records
            for rec in valid_records:
                rec_hash = calculate_student_record_hash(rec)
                cursor.execute("""
                    INSERT INTO students (
                        institution_id, programme_id, academic_year_id, submission_batch_id,
                        student_roll_no, apaar_id, apaar_digest, full_name, dob, gender,
                        admission_category, seat_category, oci_status, fn_status, admission_date, enrolment_status,
                        verification_status, version, record_hash, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'LOCAL_ATTESTED', 1, %s, NOW(), NOW()
                    )
                """, (
                    institution_id, rec["programme_id"], rec["academic_year_id"], batch_id,
                    rec["student_roll_no"], rec["apaar_id"], rec["apaar_digest"], rec["full_name"],
                    rec["dob"], rec["gender"], rec["admission_category"], rec["seat_category"],
                    rec.get("oci_status", 0), rec.get("fn_status", 0),
                    rec["admission_date"], rec["enrolment_status"], rec_hash
                ))
                student_id = cursor.lastrowid

                # Snapshot initial record version
                snapshot = json.dumps({
                    "student_id": student_id,
                    "student_roll_no": rec["student_roll_no"],
                    "full_name": rec["full_name"],
                    "apaar_masked": mask_apaar_id(rec["apaar_id"]),
                    "programme_id": rec["programme_id"],
                    "academic_year_id": rec["academic_year_id"],
                    "enrolment_status": rec["enrolment_status"],
                    "batch_ref": batch_ref
                })
                cursor.execute("""
                    INSERT INTO student_record_versions (
                        student_id, version_no, changed_by, change_reason, snapshot_json, record_hash, created_at
                    ) VALUES (%s, 1, %s, 'Initial batch CSV ingestion', %s, %s, NOW())
                """, (student_id, user_email, snapshot, rec_hash))

            # 3. Insert Validation Failures for Permanent Auditing
            for err in validation_errors:
                cursor.execute("""
                    INSERT INTO batch_validation_results (
                        batch_id, row_num, student_roll_no, field_name, error_code, error_message, raw_data_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    batch_id, err["row_num"], err.get("student_roll_no"), err["field_name"],
                    err["error_code"], err["error_message"], json.dumps(err.get("raw_data") or {})
                ))

    # 4. Trigger Anomaly & Intake Exceedance Engine
    detected_anomalies = detect_batch_anomalies(batch_id, institution_id, academic_year_id)

    # 5. Append to Cryptographic Trust Ledger
    audit_receipt = record_audit(
        actor=user_email,
        action="STUDENT_BATCH_COMMITTED",
        entity_type="SUBMISSION_BATCH",
        entity_id=batch_id,
        payload={
            "batch_ref": batch_ref,
            "institution_id": institution_id,
            "academic_year_id": academic_year_id,
            "filename": filename,
            "valid_records_persisted": valid_count,
            "invalid_rows_logged": invalid_count,
            "anomalies_flagged": len(detected_anomalies)
        }
    )

    return {
        "status": "success",
        "batch_id": batch_id,
        "batch_ref": batch_ref,
        "persisted_records": valid_count,
        "logged_errors": invalid_count,
        "anomalies_flagged": len(detected_anomalies),
        "audit_receipt": audit_receipt
    }

def get_institution_students(
    institution_id: int,
    programme_id: Optional[int] = None,
    academic_year_id: Optional[int] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> Dict[str, Any]:
    """Retrieves paginated student records for institution or admin."""
    query = """
        SELECT s.id, s.institution_id, s.student_roll_no, s.apaar_id, s.full_name, s.dob, s.gender,
               s.admission_category, s.seat_category, s.oci_status, s.fn_status, s.admission_date, s.enrolment_status,
               s.verification_status, s.version, s.record_hash, s.created_at,
               p.code as programme_code, p.name as programme_name,
               ay.year_code as academic_year,
               i.name as institution_name
        FROM students s
        JOIN programmes p ON s.programme_id = p.id
        JOIN academic_years ay ON s.academic_year_id = ay.id
        JOIN institutions i ON s.institution_id = i.id
        WHERE s.institution_id = %s
    """
    args = [institution_id]

    if programme_id:
        query += " AND s.programme_id = %s"
        args.append(programme_id)
    if academic_year_id:
        query += " AND s.academic_year_id = %s"
        args.append(academic_year_id)
    if search:
        query += " AND (s.full_name LIKE %s OR s.student_roll_no LIKE %s OR s.apaar_id LIKE %s)"
        like_arg = f"%{search}%"
        args.extend([like_arg, like_arg, like_arg])

    count_query = f"SELECT COUNT(*) as total FROM ({query}) as subq"
    total_row = fetch_one(count_query, tuple(args))
    total = total_row["total"] if total_row else 0

    query += " ORDER BY s.id DESC LIMIT %s OFFSET %s"
    args.extend([limit, offset])
    rows = fetch_all(query, tuple(args))

    for r in rows:
        r["masked_apaar"] = mask_apaar_id(r.get("apaar_id"))

    return {"total": total, "limit": limit, "offset": offset, "students": rows}

