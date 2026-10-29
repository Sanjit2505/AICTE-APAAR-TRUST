import os
import pymysql
import hashlib
from app.config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
from app.database import get_db, execute, fetch_one, fetch_all

def apply_student_schema():
    print("Applying student schema to MySQL...")
    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4"
    )
    cursor = conn.cursor()
    
    schema_path = os.path.join(os.path.dirname(__file__), "student_schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql_content = f.read()

    # Split into statements
    statements = sql_content.split(";")
    for stmt in statements:
        cleaned = stmt.strip()
        if cleaned and not cleaned.startswith("--") and not cleaned.upper().startswith("USE "):
            try:
                cursor.execute(cleaned)
            except Exception as e:
                print(f"Notice on stmt: {cleaned[:40]}... -> {e}")

    conn.commit()
    cursor.close()
    conn.close()
    print("Student schema tables created successfully.")

def seed_student_foundations():
    print("Seeding foundation data for APAAR & Student Management...")
    
    # 1. Academic Years
    years = [
        ("2023-2024", 2023, 2024, 0),
        ("2024-2025", 2024, 2025, 1),
        ("2025-2026", 2025, 2026, 1),
    ]
    for code, s_yr, e_yr, active in years:
        existing = fetch_one("SELECT id FROM academic_years WHERE year_code = %s", (code,))
        if not existing:
            execute(
                "INSERT INTO academic_years (year_code, start_year, end_year, is_active) VALUES (%s, %s, %s, %s)",
                (code, s_yr, e_yr, active)
            )

    # 2. Standard AICTE Engineering & Technology Programmes
    programmes = [
        ("BTECH-CSE", "B.Tech in Computer Science & Engineering", "UG", 4, "Computer Science & Engineering"),
        ("BTECH-AI-DS", "B.Tech in Artificial Intelligence & Data Science", "UG", 4, "Computer Science & Engineering"),
        ("BTECH-ECE", "B.Tech in Electronics & Communication Engineering", "UG", 4, "Electronics Engineering"),
        ("BTECH-MECH", "B.Tech in Mechanical Engineering", "UG", 4, "Mechanical Engineering"),
        ("BTECH-EE", "B.Tech in Electrical Engineering", "UG", 4, "Electrical Engineering"),
        ("BTECH-IT", "B.Tech in Information Technology", "UG", 4, "Information Technology"),
        ("MTECH-CSE", "M.Tech in Advanced Computer Science", "PG", 2, "Computer Science & Engineering"),
        ("MCA", "Master of Computer Applications", "PG", 2, "Computer Applications")
    ]
    for code, name, degree, duration, dept in programmes:
        existing = fetch_one("SELECT id FROM programmes WHERE code = %s", (code,))
        if not existing:
            execute(
                "INSERT INTO programmes (code, name, degree_level, duration_years, department) VALUES (%s, %s, %s, %s, %s)",
                (code, name, degree, duration, dept)
            )

    # 3. Ensure AICTE Admin, Institution Admin, and Authorized Verifier Users exist
    # Note: Using PBKDF2/SHA256 for passwords
    from app.services.auth_service import hash_password

    # AICTE Administrator
    aicte_admin = fetch_one("SELECT id FROM users WHERE email = 'admin@aicte.gov.in'")
    if not aicte_admin:
        pwd_hash = hash_password("aicteAdmin2026!")
        execute("""
            INSERT INTO users (name, email, password_hash, role, institution_id, is_active, created_at)
            VALUES ('AICTE Central Director', 'admin@aicte.gov.in', %s, 'ADMIN', NULL, 1, NOW())
        """, (pwd_hash,))
        print("Provisioned AICTE Central Administrator (admin@aicte.gov.in / aicteAdmin2026!)")

    # Ensure Institution exists
    inst = fetch_one("SELECT id FROM institutions WHERE aicte_id = '1-1097241811'")
    if not inst:
        inst_id = execute("""
            INSERT INTO institutions (aicte_id, name, trust_name, address, state, district, trust_score, created_at)
            VALUES ('1-1097241811', 'Delhi Technological Institute', 'National Education Trust', 'Bawana Road, Delhi 110042', 'Delhi', 'North West Delhi', 92, NOW())
        """)
    else:
        inst_id = inst["id"]

    # Institution 2 for cross-institution duplicate simulation
    inst2 = fetch_one("SELECT id FROM institutions WHERE aicte_id = '1-9988776655'")
    if not inst2:
        inst2_id = execute("""
            INSERT INTO institutions (aicte_id, name, trust_name, address, state, district, trust_score, created_at)
            VALUES ('1-9988776655', 'Mumbai Institute of Advanced Technology', 'Western Shikshan Sanstha', 'Andheri East, Mumbai 400069', 'Maharashtra', 'Mumbai Suburban', 88, NOW())
        """)
    else:
        inst2_id = inst2["id"]

    # Institution Admin User
    inst_user = fetch_one("SELECT id FROM users WHERE email = 'admin@dti.edu.in'")
    if not inst_user:
        pwd_hash = hash_password("dtiAdmin2026!")
        execute("""
            INSERT INTO users (name, email, password_hash, role, institution_id, is_active, created_at)
            VALUES ('DTI Registrar Admin', 'admin@dti.edu.in', %s, 'COLLEGE', %s, 1, NOW())
        """, (pwd_hash, inst_id))
        print("Provisioned Institution Administrator (admin@dti.edu.in / dtiAdmin2026!)")

    # Authorized Verifier User
    verifier_user = fetch_one("SELECT id FROM users WHERE email = 'verifier@tcs.com'")
    if not verifier_user:
        pwd_hash = hash_password("verifier2026!")
        execute("""
            INSERT INTO users (name, email, password_hash, role, institution_id, is_active, created_at)
            VALUES ('TCS Background Verification Services', 'verifier@tcs.com', %s, 'AUDITOR', NULL, 1, NOW())
        """, (pwd_hash,))
        print("Provisioned Authorized Verifier (verifier@tcs.com / verifier2026!)")

    # 4. Seed Approved Intakes for Delhi Technological Institute (inst_id)
    ay_2024 = fetch_one("SELECT id FROM academic_years WHERE year_code = '2024-2025'")
    ay_2025 = fetch_one("SELECT id FROM academic_years WHERE year_code = '2025-2026'")
    
    if ay_2024:
        prog_cse = fetch_one("SELECT id FROM programmes WHERE code = 'BTECH-CSE'")
        prog_aids = fetch_one("SELECT id FROM programmes WHERE code = 'BTECH-AI-DS'")
        prog_ece = fetch_one("SELECT id FROM programmes WHERE code = 'BTECH-ECE'")
        prog_mech = fetch_one("SELECT id FROM programmes WHERE code = 'BTECH-MECH'")
        
        intake_configs = [
            (inst_id, prog_cse["id"], ay_2024["id"], 120, 12, 6, 6),
            (inst_id, prog_aids["id"], ay_2024["id"], 60, 6, 3, 3),
            (inst_id, prog_ece["id"], ay_2024["id"], 90, 9, 4, 4),
            (inst_id, prog_mech["id"], ay_2024["id"], 60, 6, 3, 3),
            # inst2 configs
            (inst2_id, prog_cse["id"], ay_2024["id"], 120, 12, 6, 6),
            (inst2_id, prog_ece["id"], ay_2024["id"], 60, 6, 3, 3),
        ]
        
        for i_id, p_id, a_id, seats, supernum, nri, tfw in intake_configs:
            existing = fetch_one("""
                SELECT id FROM approved_intakes 
                WHERE institution_id = %s AND programme_id = %s AND academic_year_id = %s
            """, (i_id, p_id, a_id))
            if not existing:
                execute("""
                    INSERT INTO approved_intakes 
                    (institution_id, programme_id, academic_year_id, approved_seats, supernumerary_quota, nri_quota, tfw_quota)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (i_id, p_id, a_id, seats, supernum, nri, tfw))

    print("Foundational seed data created successfully.")

if __name__ == "__main__":
    apply_student_schema()
    seed_student_foundations()

