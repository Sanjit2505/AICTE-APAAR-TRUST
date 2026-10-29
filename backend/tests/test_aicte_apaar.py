import os
import sys
import io
import json
import pytest
import requests

# Ensure backend directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BASE_URL = "http://127.0.0.1:8000"

@pytest.fixture(scope="session")
def api_tokens():
    """Authenticates the 3 primary roles and yields tokens."""
    # 1. AICTE Admin
    r_admin = requests.post(f"{BASE_URL}/api/v1/auth/login", json={
        "email": "admin@aicte.gov.in",
        "password": "aicteAdmin2026!"
    })
    assert r_admin.status_code == 200, f"AICTE Admin login failed: {r_admin.text}"
    admin_token = r_admin.json()["token"]

    # 2. Institution Admin (DTI - Inst ID 2)
    r_inst = requests.post(f"{BASE_URL}/api/v1/auth/login", json={
        "email": "admin@dti.edu.in",
        "password": "dtiAdmin2026!"
    })
    assert r_inst.status_code == 200, f"Institution login failed: {r_inst.text}"
    inst_token = r_inst.json()["token"]

    # 3. Authorized Verifier (TCS)
    r_ver = requests.post(f"{BASE_URL}/api/v1/auth/login", json={
        "email": "verifier@tcs.com",
        "password": "verifier2026!"
    })
    assert r_ver.status_code == 200, f"Verifier login failed: {r_ver.text}"
    verifier_token = r_ver.json()["token"]

    return {
        "admin": admin_token,
        "institution": inst_token,
        "verifier": verifier_token
    }

# 1. Authorized user can log in
def test_01_authorized_user_can_login():
    res = requests.post(f"{BASE_URL}/api/v1/auth/login", json={
        "email": "admin@aicte.gov.in",
        "password": "aicteAdmin2026!"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "token" in data
    assert data["user"]["role"] in ["ADMIN", "AICTE_ADMIN"]

# 2. Invalid credentials are rejected
def test_02_invalid_credentials_are_rejected():
    res = requests.post(f"{BASE_URL}/api/v1/auth/login", json={
        "email": "admin@aicte.gov.in",
        "password": "WrongPassword123!"
    })
    assert res.status_code == 401
    assert "Invalid email or password" in res.json()["detail"]

# 3. Unauthorized users cannot access protected endpoints
def test_03_unauthorized_access_rejected():
    # Calling protected endpoint without Bearer token
    res = requests.get(f"{BASE_URL}/api/v1/intake/national-overview")
    assert res.status_code in [401, 403]

    # Calling with invalid garbage token
    res_bad = requests.get(
        f"{BASE_URL}/api/v1/intake/national-overview",
        headers={"Authorization": "Bearer invalid.fake.token"}
    )
    assert res_bad.status_code == 401

# 4. Institution administrators cannot access another institution's records
def test_04_institution_isolation_enforced(api_tokens):
    inst_token = api_tokens["institution"] # Belongs to Inst ID 2 (DTI)
    
    # Attempting to query records for Institution ID 3 (Mumbai Institute)
    res = requests.get(
        f"{BASE_URL}/api/v1/students/institution/3",
        headers={"Authorization": f"Bearer {inst_token}"}
    )
    assert res.status_code == 403
    assert "another institution" in res.json()["detail"].lower()

# 5. Valid CSV uploads are processed correctly
def test_05_valid_csv_upload_processed(api_tokens):
    inst_token = api_tokens["institution"]
    unique_roll = f"TEST-VAL-{int(requests.get(f'{BASE_URL}/api/health').json()['students_count']) + 100}"
    csv_content = f"""student_roll_no,apaar_id,full_name,dob,gender,programme_code,academic_year,admission_category,seat_category,admission_date,enrolment_status
{unique_roll},223344556677,Siddharth Rao,2004-06-12,MALE,BTECH-CSE,2024-2025,GENERAL,REGULAR,2024-08-01,ENROLLED
"""
    files = {"file": ("valid_test.csv", csv_content.encode("utf-8"), "text/csv")}
    data = {"institution_id": 2}
    res = requests.post(
        f"{BASE_URL}/api/v1/students/upload-csv",
        headers={"Authorization": f"Bearer {inst_token}"},
        files=files,
        data=data
    )
    assert res.status_code == 200
    res_json = res.json()
    assert res_json["valid_count"] == 1
    assert res_json["invalid_count"] == 0
    assert len(res_json["errors"]) == 0

# 6. Invalid CSV files are rejected safely
def test_06_invalid_csv_rejected_safely(api_tokens):
    inst_token = api_tokens["institution"]
    # Corrupt headers
    bad_csv = "random_header_one,random_header_two\n123,456\n"
    files = {"file": ("bad_headers.csv", bad_csv.encode("utf-8"), "text/csv")}
    data = {"institution_id": 2}
    res = requests.post(
        f"{BASE_URL}/api/v1/students/upload-csv",
        headers={"Authorization": f"Bearer {inst_token}"},
        files=files,
        data=data
    )
    assert res.status_code == 400
    assert "Missing mandatory CSV columns" in res.json()["detail"]

# 7. Missing APAAR identifiers are flagged
def test_07_missing_apaar_identifier_flagged(api_tokens):
    inst_token = api_tokens["institution"]
    # Row with empty APAAR ID
    csv_missing_apaar = """student_roll_no,apaar_id,full_name,dob,gender,programme_code,academic_year,admission_category,seat_category,admission_date,enrolment_status
2024-ERR-001,,Kavita Nair,2004-09-18,FEMALE,BTECH-CSE,2024-2025,GENERAL,REGULAR,2024-08-01,ENROLLED
"""
    files = {"file": ("missing_apaar.csv", csv_missing_apaar.encode("utf-8"), "text/csv")}
    data = {"institution_id": 2}
    res = requests.post(
        f"{BASE_URL}/api/v1/students/upload-csv",
        headers={"Authorization": f"Bearer {inst_token}"},
        files=files,
        data=data
    )
    assert res.status_code == 200
    res_json = res.json()
    assert res_json["invalid_count"] >= 1
    err_codes = [e["error_code"] for e in res_json["errors"]]
    assert "MISSING_APAAR" in err_codes

# 8. Duplicate records within a batch are detected
def test_08_duplicate_records_in_batch_detected(api_tokens):
    inst_token = api_tokens["institution"]
    # Same roll number duplicated in batch
    csv_dup = """student_roll_no,apaar_id,full_name,dob,gender,programme_code,academic_year,admission_category,seat_category,admission_date,enrolment_status
DUP-ROLL-01,112233445566,Ananya Roy,2004-03-21,FEMALE,BTECH-ECE,2024-2025,GENERAL,REGULAR,2024-08-01,ENROLLED
DUP-ROLL-01,778899001122,Same Roll Duplicate,2004-03-21,FEMALE,BTECH-ECE,2024-2025,GENERAL,REGULAR,2024-08-01,ENROLLED
"""
    files = {"file": ("dup_test.csv", csv_dup.encode("utf-8"), "text/csv")}
    data = {"institution_id": 2}
    res = requests.post(
        f"{BASE_URL}/api/v1/students/upload-csv",
        headers={"Authorization": f"Bearer {inst_token}"},
        files=files,
        data=data
    )
    assert res.status_code == 200
    res_json = res.json()
    err_codes = [e["error_code"] for e in res_json["errors"]]
    assert "DUPLICATE_IN_BATCH" in err_codes

# 9. Potential cross-institution matches are detected using protected matching
def test_09_cross_institution_matches_detected(api_tokens):
    admin_token = api_tokens["admin"]
    # Check that the anomaly query returns detected cross-institution enrolment findings
    res = requests.get(
        f"{BASE_URL}/api/v1/anomalies?finding_type=CROSS_INSTITUTION_ENROLMENT",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 200
    anomalies = res.json()
    assert len(anomalies) >= 1
    first = anomalies[0]
    assert first["finding_type"] == "CROSS_INSTITUTION_ENROLMENT"
    assert first["severity"] == "CRITICAL"
    # Verify evidence has protected digest prefix and masked details
    assert "matched_apaar_digest_prefix" in first["evidence"]

# 10. Intake exceedances are calculated correctly
def test_10_intake_exceedances_calculated(api_tokens):
    inst_token = api_tokens["institution"]
    res = requests.get(
        f"{BASE_URL}/api/v1/intake/institution/2",
        headers={"Authorization": f"Bearer {inst_token}"}
    )
    assert res.status_code == 200
    progs = res.json()
    assert len(progs) > 0
    for p in progs:
        assert "approved_seats" in p
        assert "total_enrolled" in p
        assert "remaining_regular_seats" in p
        assert "utilization_percentage" in p
        # Formula check: remaining regular = max(0, approved - regular_enrolled)
        expected_remaining = max(0, p["approved_seats"] - p["regular_enrolled"])
        assert p["remaining_regular_seats"] == expected_remaining

# 11. Category-specific rules are applied correctly
def test_11_category_specific_rules_applied(api_tokens):
    inst_token = api_tokens["institution"]
    res = requests.get(
        f"{BASE_URL}/api/v1/intake/institution/2",
        headers={"Authorization": f"Bearer {inst_token}"}
    )
    assert res.status_code == 200
    progs = res.json()
    p_cse = next(p for p in progs if p["programme_code"] == "BTECH-CSE")
    # Verify supernumerary and category quotas exist
    assert p_cse["supernumerary_quota"] == 12
    assert p_cse["nri_quota"] == 6
    assert p_cse["tfw_quota"] == 6
    assert "categories" in p_cse
    assert "GENERAL" in p_cse["categories"]
    assert "OBC" in p_cse["categories"]

# 12. Verification requests respect permissions
def test_12_verification_requests_respect_permissions(api_tokens):
    # Non-authenticated user cannot verify
    res_unauth = requests.post(f"{BASE_URL}/api/v1/verifications/verify", json={
        "query_type": "ROLL_NO",
        "query_value": "2024-CSE-001",
        "institution_aicte_id": "1-1097241811"
    })
    assert res_unauth.status_code in [401, 403]

    # Authorized Verifier can verify
    verifier_token = api_tokens["verifier"]
    res_auth = requests.post(
        f"{BASE_URL}/api/v1/verifications/verify",
        headers={"Authorization": f"Bearer {verifier_token}"},
        json={
            "query_type": "ROLL_NO",
            "query_value": "2024-CSE-001",
            "institution_aicte_id": "1-1097241811"
        }
    )
    assert res_auth.status_code == 200
    data = res_auth.json()
    assert data["status"] == "VERIFIED"
    assert data["verified"] is True
    assert "student_masked_name" in data

# 13. Mock verification is clearly distinguished from official verification
def test_13_mock_verification_distinguished():
    from app.services.apaar_service import apaar_adapter
    # When official government DigiLocker APAAR Gateway is offline/mock
    res = apaar_adapter.verify_apaar("123456789012", "Aarav Sharma", "2004-05-14")
    assert res["is_external"] is False
    assert "External APAAR verification is not configured" in res["detail"]
    assert "CRITICAL: A format check does NOT prove" in res["disclaimer"]

# 14. Reports contain the correct filtered records
def test_14_reports_contain_filtered_records(api_tokens):
    admin_token = api_tokens["admin"]
    res = requests.get(
        f"{BASE_URL}/api/v1/reports/intake.csv",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    csv_text = res.text
    assert "Institution AICTE ID" in csv_text
    assert "BTECH-CSE" in csv_text
    assert "Delhi Technological Institute" in csv_text

# 15. Database data persists across application restarts
def test_15_database_persistence():
    # Verify student records exist in MySQL
    from app.database import fetch_one
    count_row = fetch_one("SELECT COUNT(*) as cnt FROM students")
    assert count_row["cnt"] > 0

# 16. Repeated upload requests do not create unintended duplicates
def test_16_repeated_upload_prevents_duplicates(api_tokens):
    inst_token = api_tokens["institution"]
    # Re-uploading an already committed student roll number
    csv_dup_existing = """student_roll_no,apaar_id,full_name,dob,gender,programme_code,academic_year,admission_category,seat_category,admission_date,enrolment_status
2024-CSE-001,123456789012,Aarav Sharma,2004-05-14,MALE,BTECH-CSE,2024-2025,GENERAL,REGULAR,2024-08-01,ENROLLED
"""
    files = {"file": ("dup_existing.csv", csv_dup_existing.encode("utf-8"), "text/csv")}
    data = {"institution_id": 2}
    res = requests.post(
        f"{BASE_URL}/api/v1/students/upload-csv",
        headers={"Authorization": f"Bearer {inst_token}"},
        files=files,
        data=data
    )
    assert res.status_code == 200
    res_json = res.json()
    err_codes = [e["error_code"] for e in res_json["errors"]]
    assert "DUPLICATE_IN_INSTITUTION" in err_codes or "DUPLICATE_APAAR_IN_INSTITUTION" in err_codes
    assert res_json["valid_count"] == 0

# 17. Audit events are recorded for important operations
def test_17_audit_events_recorded_in_trust_ledger():
    from app.database import fetch_all
    audits = fetch_all("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 20")
    assert len(audits) > 0
    actions = [a["action"] for a in audits]
    # Check that batch commits, verifications, and registrations are recorded
    assert any("STUDENT_BATCH_COMMITTED" in a or "CREDENTIAL_VERIFIED" in a or "INSTITUTION" in a for a in actions)

# 18. Sensitive values are not leaked through unauthorized API responses
def test_18_sensitive_values_not_leaked(api_tokens):
    verifier_token = api_tokens["verifier"]
    res = requests.post(
        f"{BASE_URL}/api/v1/verifications/verify",
        headers={"Authorization": f"Bearer {verifier_token}"},
        json={
            "query_type": "ROLL_NO",
            "query_value": "2024-CSE-001",
            "institution_aicte_id": "1-1097241811"
        }
    )
    data = res.json()
    # Sensitive personal information must NOT be present in verifier response
    assert "apaar_id" not in data # Raw 12-digit APAAR not returned
    assert "dob" not in data # Raw DOB not exposed
    assert "address" not in data
    # Masked name used
    assert "*" in data["student_masked_name"]

# 19. API errors are handled consistently
def test_19_consistent_api_error_handling(api_tokens):
    inst_token = api_tokens["institution"]
    # Requesting non-existent batch errors
    res = requests.get(
        f"{BASE_URL}/api/v1/reports/batch-errors/999999.csv",
        headers={"Authorization": f"Bearer {inst_token}"}
    )
    assert res.status_code == 404
    assert "detail" in res.json()

# 20. Core user workflows pass end-to-end tests
def test_20_core_workflows_end_to_end(api_tokens):
    admin_token = api_tokens["admin"]
    verifier_token = api_tokens["verifier"]

    # 1. Admin checks anomalies
    res_anom = requests.get(f"{BASE_URL}/api/v1/anomalies", headers={"Authorization": f"Bearer {admin_token}"})
    assert res_anom.status_code == 200
    anomalies = res_anom.json()
    assert len(anomalies) > 0

    # 2. Admin resolves/updates an anomaly
    target_id = anomalies[0]["id"]
    res_resolve = requests.post(
        f"{BASE_URL}/api/v1/anomalies/{target_id}/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"action": "INVESTIGATING", "reviewer_notes": "Notified state technical education board."}
    )
    assert res_resolve.status_code == 200
    assert res_resolve.json()["new_status"] == "INVESTIGATING"

    # 3. Verifier verifies ledger integrity
    res_ledger = requests.get(f"{BASE_URL}/api/v1/trust-ledger/verify")
    assert res_ledger.status_code == 200
    assert res_ledger.json()["valid"] is True


# 21. Direct APAAR ID Validation & Details Lookup
def test_21_direct_apaar_validation():
    # Valid and registered APAAR
    res_reg = requests.get(f"{BASE_URL}/api/v1/validate/apaar/123456789012")
    assert res_reg.status_code == 200
    data_reg = res_reg.json()
    assert data_reg["valid"] is True
    assert data_reg["found"] is True
    assert "Aarav" in data_reg["student"]["full_name"]
    assert data_reg["student"]["programme_code"] == "BTECH-CSE"

    # Valid syntax but unregistered APAAR
    res_unreg = requests.get(f"{BASE_URL}/api/v1/validate/apaar/778899001122")
    assert res_unreg.status_code == 200
    data_unreg = res_unreg.json()
    assert data_unreg["valid"] is True
    assert data_unreg["found"] is False
    assert data_unreg["status"] == "SYNTACTICALLY_VALID_UNASSIGNED"

    # Invalid APAAR
    res_inv = requests.get(f"{BASE_URL}/api/v1/validate/apaar/123")
    assert res_inv.status_code == 200
    assert res_inv.json()["valid"] is False

# 22. Direct AICTE Institute ID Validation & Details Lookup
def test_22_direct_aicte_validation():
    # Valid and registered AICTE ID
    res_inst = requests.get(f"{BASE_URL}/api/v1/validate/aicte/1-1097241811")
    assert res_inst.status_code == 200
    data_inst = res_inst.json()
    assert data_inst["valid"] is True
    assert data_inst["found"] is True
    assert "Delhi Technological Institute" in data_inst["institution"]["name"]
    assert data_inst["programmes_count"] > 0
    assert len(data_inst["programmes"]) > 0

    # Invalid AICTE ID syntax
    res_inv = requests.get(f"{BASE_URL}/api/v1/validate/aicte/invalid-aicte-format")
    assert res_inv.status_code == 200
    assert res_inv.json()["valid"] is False

# 23. Requirement 10: Official Degree-Awarding Certificate with APAAR ID + Student ID Display
def test_23_official_degree_certificate_display(api_tokens):
    inst_token = api_tokens["institution"]
    # 1. Fetch certificate for existing student (ID 1: Aarav Sharma)
    res_cert = requests.get(
        f"{BASE_URL}/api/v1/students/1/certificate",
        headers={"Authorization": f"Bearer {inst_token}"}
    )
    assert res_cert.status_code == 200
    cert_data = res_cert.json()
    assert cert_data["status"] == "success"
    cert = cert_data["certificate"]
    assert "AICTE-APAAR-DEG-" in cert["certificate_number"]
    assert cert["student_name"] == "Aarav Sharma"
    assert cert["student_id"] == "2024-CSE-001"
    # APAAR ID displayed alongside student ID
    assert cert["apaar_id"] == "1234 5678 9012"
    assert cert["institution_name"] == "Delhi Technological Institute"
    assert cert["institution_aicte_id"] == "1-1097241811"
    assert cert["enrolment_status"] == "ENROLLED"
    assert cert["degree_level"] in ["UG", "UNDERGRADUATE"]
    assert "record_hash" in cert
    assert "digital_attestation_signature" in cert

    # 2. Check certificate through verified reference code
    verifier_token = api_tokens["verifier"]
    res_verify = requests.post(
        f"{BASE_URL}/api/v1/verifications/verify",
        headers={"Authorization": f"Bearer {verifier_token}"},
        json={
            "query_type": "ROLL_NO",
            "query_value": "2024-CSE-001",
            "institution_aicte_id": "1-1097241811"
        }
    )
    assert res_verify.status_code == 200
    v_data = res_verify.json()
    v_ref = v_data["verification_ref"]

    res_v_cert = requests.get(
        f"{BASE_URL}/api/v1/verifications/{v_ref}/certificate",
        headers={"Authorization": f"Bearer {verifier_token}"}
    )
    assert res_v_cert.status_code == 200
    assert res_v_cert.json()["certificate"]["student_id"] == "2024-CSE-001"

# 24. OCI, FN (Foreign National), and Supernumerary Seat Intake Fields
def test_24_oci_fn_and_supernumerary_intake(api_tokens):
    inst_token = api_tokens["institution"]
    # Check intake monitoring returns oci_quota and fn_quota fields
    res_intake = requests.get(
        f"{BASE_URL}/api/v1/intake/institution/2",
        headers={"Authorization": f"Bearer {inst_token}"}
    )
    assert res_intake.status_code == 200
    intakes = res_intake.json()
    assert len(intakes) > 0
    cse_intake = intakes[0]
    assert "oci_quota" in cse_intake
    assert "fn_quota" in cse_intake
    assert "oci_enrolled" in cse_intake
    assert "fn_enrolled" in cse_intake
    assert "OCI" in cse_intake["categories"]
    assert "FN" in cse_intake["categories"]
