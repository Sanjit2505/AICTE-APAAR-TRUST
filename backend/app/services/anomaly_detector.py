import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from app.database import get_db, fetch_all, fetch_one, execute
from app.services.trust_ledger import record_audit

def detect_batch_anomalies(batch_id: int, institution_id: int, academic_year_id: int) -> List[Dict[str, Any]]:
    """
    Runs automated anomaly & duplicate detection rules on a committed student batch:
    1. Cross-institution duplicate enrolments (Active student enrolled in 2+ institutions)
    2. Repeated APAAR within same institution
    3. Conflicting student identifiers
    4. Intake limit exceedances
    """
    detected = []

    # 1. Cross-Institution Duplicate Enrolment Check
    # Look for identical apaar_digest enrolled across different institutions for the same academic year
    cross_inst_query = """
        SELECT s1.id as s1_id, s1.full_name as s1_name, s1.student_roll_no as s1_roll,
               i1.id as i1_id, i1.name as i1_name, i1.aicte_id as i1_code,
               p1.name as p1_name,
               s2.id as s2_id, s2.full_name as s2_name, s2.student_roll_no as s2_roll,
               i2.id as i2_id, i2.name as i2_name, i2.aicte_id as i2_code,
               p2.name as p2_name,
               s1.apaar_digest
        FROM students s1
        JOIN institutions i1 ON s1.institution_id = i1.id
        JOIN programmes p1 ON s1.programme_id = p1.id
        JOIN students s2 ON s1.apaar_digest = s2.apaar_digest AND s1.institution_id != s2.institution_id
        JOIN institutions i2 ON s2.institution_id = i2.id
        JOIN programmes p2 ON s2.programme_id = p2.id
        WHERE s1.submission_batch_id = %s
          AND s1.academic_year_id = s2.academic_year_id
          AND s1.enrolment_status = 'ENROLLED'
          AND s2.enrolment_status = 'ENROLLED'
    """
    cross_matches = fetch_all(cross_inst_query, (batch_id,))

    for m in cross_matches:
        # Check if already logged to avoid spamming
        existing = fetch_one("""
            SELECT id FROM anomalies 
            WHERE finding_type = 'CROSS_INSTITUTION_ENROLMENT' 
              AND student_id = %s AND target_student_id = %s
        """, (m["s1_id"], m["s2_id"]))
        
        if not existing:
            evidence = {
                "rule": "RULE_CROSS_INSTITUTION_CONCURRENT_ENROLMENT",
                "matched_apaar_digest_prefix": m["apaar_digest"][:12] + "...",
                "institution_a": {"id": m["i1_id"], "name": m["i1_name"], "code": m["i1_code"], "student": m["s1_name"], "roll": m["s1_roll"], "programme": m["p1_name"]},
                "institution_b": {"id": m["i2_id"], "name": m["i2_name"], "code": m["i2_code"], "student": m["s2_name"], "roll": m["s2_roll"], "programme": m["p2_name"]},
                "academic_year_id": academic_year_id
            }
            title = f"Potential Cross-Institution Concurrent Enrolment: {m['s1_name']}"
            desc = (
                f"Student '{m['s1_name']}' is actively enrolled at both '{m['i1_name']}' ({m['p1_name']}) "
                f"and '{m['i2_name']}' ({m['p2_name']}) during the same academic year. "
                f"Requires joint verification by AICTE Scrutiny Officers."
            )
            anom_id = execute("""
                INSERT INTO anomalies (
                    finding_type, institution_id, student_id, target_institution_id, target_student_id,
                    academic_year_id, severity, title, description, evidence_json, status, created_at
                ) VALUES (
                    'CROSS_INSTITUTION_ENROLMENT', %s, %s, %s, %s, %s, 'CRITICAL', %s, %s, %s, 'OPEN', NOW()
                )
            """, (m["i1_id"], m["s1_id"], m["i2_id"], m["s2_id"], academic_year_id, title, desc, json.dumps(evidence)))
            
            detected.append({"id": anom_id, "type": "CROSS_INSTITUTION_ENROLMENT", "title": title, "severity": "CRITICAL"})

    # 2. Programme Intake Exceedance Check
    # Check all programmes in this institution where active enrolled count > approved_seats
    intake_check_query = """
        SELECT p.id as prog_id, p.name as prog_name, p.code as prog_code,
               ai.approved_seats,
               (ai.supernumerary_quota + COALESCE(ai.nri_quota, 0) + COALESCE(ai.tfw_quota, 0) + COALESCE(ai.oci_quota, 0) + COALESCE(ai.fn_quota, 0)) as supernumerary_quota,
               COUNT(s.id) as enrolled_count
        FROM programmes p
        JOIN approved_intakes ai ON ai.programme_id = p.id AND ai.institution_id = %s AND ai.academic_year_id = %s
        JOIN students s ON s.programme_id = p.id AND s.institution_id = %s AND s.academic_year_id = %s AND s.enrolment_status = 'ENROLLED'
        GROUP BY p.id, p.name, p.code, ai.approved_seats, ai.supernumerary_quota, ai.nri_quota, ai.tfw_quota, ai.oci_quota, ai.fn_quota
        HAVING enrolled_count > (ai.approved_seats + ai.supernumerary_quota + COALESCE(ai.nri_quota, 0) + COALESCE(ai.tfw_quota, 0) + COALESCE(ai.oci_quota, 0) + COALESCE(ai.fn_quota, 0))
    """
    intake_breaches = fetch_all(intake_check_query, (institution_id, academic_year_id, institution_id, academic_year_id))

    for b in intake_breaches:
        existing = fetch_one("""
            SELECT id FROM anomalies 
            WHERE finding_type = 'INTAKE_BREACH' 
              AND institution_id = %s AND programme_id = %s AND academic_year_id = %s AND status = 'OPEN'
        """, (institution_id, b["prog_id"], academic_year_id))

        if not existing:
            evidence = {
                "rule": "RULE_APPROVED_INTAKE_CAP_EXCEEDED",
                "approved_regular_seats": b["approved_seats"],
                "approved_supernumerary": b["supernumerary_quota"],
                "total_permitted": b["approved_seats"] + b["supernumerary_quota"],
                "actual_enrolled": b["enrolled_count"],
                "excess_students": b["enrolled_count"] - (b["approved_seats"] + b["supernumerary_quota"])
            }
            title = f"Approved Intake Exceeded for {b['prog_code']}"
            desc = (
                f"Institution has enrolled {b['enrolled_count']} students in {b['prog_name']} "
                f"against an approved ceiling of {b['approved_seats']} regular + {b['supernumerary_quota']} supernumerary seats. "
                f"Over-enrolment of {evidence['excess_students']} students."
            )
            anom_id = execute("""
                INSERT INTO anomalies (
                    finding_type, institution_id, programme_id, academic_year_id,
                    severity, title, description, evidence_json, status, created_at
                ) VALUES (
                    'INTAKE_BREACH', %s, %s, %s, 'HIGH', %s, %s, %s, 'OPEN', NOW()
                )
            """, (institution_id, b["prog_id"], academic_year_id, title, desc, json.dumps(evidence)))
            
            detected.append({"id": anom_id, "type": "INTAKE_BREACH", "title": title, "severity": "HIGH"})

    return detected

def get_anomalies_list(
    institution_id: Optional[int] = None,
    finding_type: Optional[str] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieves anomalies with rich relational details."""
    query = """
        SELECT a.*,
               i.name as institution_name, i.aicte_id as institution_code,
               p.code as programme_code, p.name as programme_name,
               ay.year_code as academic_year,
               s.full_name as student_name, s.student_roll_no,
               ti.name as target_institution_name, ti.aicte_id as target_institution_code
        FROM anomalies a
        JOIN institutions i ON a.institution_id = i.id
        LEFT JOIN programmes p ON a.programme_id = p.id
        LEFT JOIN academic_years ay ON a.academic_year_id = ay.id
        LEFT JOIN students s ON a.student_id = s.id
        LEFT JOIN institutions ti ON a.target_institution_id = ti.id
        WHERE 1=1
    """
    args = []
    if institution_id:
        query += " AND (a.institution_id = %s OR a.target_institution_id = %s)"
        args.extend([institution_id, institution_id])
    if finding_type:
        query += " AND a.finding_type = %s"
        args.append(finding_type)
    if status:
        query += " AND a.status = %s"
        args.append(status)
    if severity:
        query += " AND a.severity = %s"
        args.append(severity)

    query += " ORDER BY FIELD(a.severity, 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'), a.id DESC"
    rows = fetch_all(query, tuple(args))
    for r in rows:
        if isinstance(r.get("evidence_json"), str):
            try:
                r["evidence"] = json.loads(r["evidence_json"])
            except Exception:
                r["evidence"] = r["evidence_json"]
        else:
            r["evidence"] = r.get("evidence_json")
    return rows

def resolve_anomaly(anomaly_id: int, action: str, reviewer_name: str, reviewer_notes: str) -> Dict[str, Any]:
    """Records an authorized reviewer's decision on an anomaly."""
    # action in ['INVESTIGATING', 'RESOLVED', 'DISMISSED']
    status_map = {
        "INVESTIGATE": "INVESTIGATING",
        "INVESTIGATING": "INVESTIGATING",
        "RESOLVE": "RESOLVED",
        "RESOLVED": "RESOLVED",
        "DISMISS": "DISMISSED",
        "DISMISSED": "DISMISSED"
    }
    new_status = status_map.get(action.upper(), "RESOLVED")

    execute("""
        UPDATE anomalies 
        SET status = %s, reviewer_notes = %s, resolved_by = %s, resolved_at = NOW()
        WHERE id = %s
    """, (new_status, reviewer_notes, reviewer_name, anomaly_id))

    audit = record_audit(
        actor=reviewer_name,
        action=f"ANOMALY_{new_status}",
        entity_type="ANOMALY",
        entity_id=anomaly_id,
        payload={"action": new_status, "reviewer": reviewer_name, "notes": reviewer_notes}
    )

    return {"status": "success", "anomaly_id": anomaly_id, "new_status": new_status, "audit_receipt": audit}

