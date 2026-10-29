-- AICTE APAAR Student Data Management & Verification Schema
-- MySQL 8.0 & Relational DB Compatible

USE aicte_trust_layer;

-- 1. Academic Years
CREATE TABLE IF NOT EXISTS academic_years (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    year_code VARCHAR(32) NOT NULL UNIQUE,
    start_year INT NOT NULL,
    end_year INT NOT NULL,
    is_active TINYINT(1) DEFAULT 1,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. Programmes
CREATE TABLE IF NOT EXISTS programmes (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    degree_level ENUM('UG', 'PG', 'DIPLOMA', 'DOCTORAL') NOT NULL DEFAULT 'UG',
    duration_years INT NOT NULL DEFAULT 4,
    department VARCHAR(255) NOT NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Approved Intakes per Institution, Programme & Academic Year
CREATE TABLE IF NOT EXISTS approved_intakes (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    institution_id BIGINT UNSIGNED NOT NULL,
    programme_id BIGINT UNSIGNED NOT NULL,
    academic_year_id BIGINT UNSIGNED NOT NULL,
    approved_seats INT NOT NULL DEFAULT 60,
    supernumerary_quota INT NOT NULL DEFAULT 6,
    nri_quota INT NOT NULL DEFAULT 3,
    oci_quota INT NOT NULL DEFAULT 2,
    fn_quota INT NOT NULL DEFAULT 2,
    tfw_quota INT NOT NULL DEFAULT 3,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_inst_prog_year (institution_id, programme_id, academic_year_id),
    FOREIGN KEY (institution_id) REFERENCES institutions(id) ON DELETE CASCADE,
    FOREIGN KEY (programme_id) REFERENCES programmes(id) ON DELETE CASCADE,
    FOREIGN KEY (academic_year_id) REFERENCES academic_years(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. Submission Batches
CREATE TABLE IF NOT EXISTS submission_batches (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    batch_ref VARCHAR(64) NOT NULL UNIQUE,
    institution_id BIGINT UNSIGNED NOT NULL,
    academic_year_id BIGINT UNSIGNED NOT NULL,
    filename VARCHAR(255) NOT NULL,
    total_rows INT DEFAULT 0,
    valid_rows INT DEFAULT 0,
    invalid_rows INT DEFAULT 0,
    status ENUM('PENDING_VALIDATION', 'VALIDATED', 'COMMITTED', 'REJECTED') DEFAULT 'PENDING_VALIDATION',
    uploaded_by VARCHAR(255) NOT NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (institution_id) REFERENCES institutions(id) ON DELETE CASCADE,
    FOREIGN KEY (academic_year_id) REFERENCES academic_years(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. Batch Validation Results (Line-by-line validation errors)
CREATE TABLE IF NOT EXISTS batch_validation_results (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    batch_id BIGINT UNSIGNED NOT NULL,
    row_num INT NOT NULL,
    student_roll_no VARCHAR(64) NULL,
    field_name VARCHAR(64) NOT NULL,
    error_code VARCHAR(64) NOT NULL,
    error_message TEXT NOT NULL,
    raw_data_json JSON NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (batch_id) REFERENCES submission_batches(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6. Students
CREATE TABLE IF NOT EXISTS students (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    institution_id BIGINT UNSIGNED NOT NULL,
    programme_id BIGINT UNSIGNED NOT NULL,
    academic_year_id BIGINT UNSIGNED NOT NULL,
    submission_batch_id BIGINT UNSIGNED NULL,
    student_roll_no VARCHAR(64) NOT NULL,
    apaar_id VARCHAR(32) NULL,
    apaar_digest CHAR(64) NULL,
    full_name VARCHAR(255) NOT NULL,
    dob DATE NOT NULL,
    gender ENUM('MALE', 'FEMALE', 'OTHER') NOT NULL,
    admission_category ENUM('GENERAL', 'OBC', 'SC', 'ST', 'EWS', 'NRI', 'TFW') NOT NULL DEFAULT 'GENERAL',
    seat_category ENUM('REGULAR', 'SUPERNUMERARY', 'NRI', 'TFW', 'OCI', 'FN') NOT NULL DEFAULT 'REGULAR',
    oci_status TINYINT(1) DEFAULT 0,
    fn_status TINYINT(1) DEFAULT 0,
    admission_date DATE NOT NULL,
    enrolment_status ENUM('ENROLLED', 'COMPLETED', 'DROPPED', 'SUSPENDED') NOT NULL DEFAULT 'ENROLLED',
    verification_status ENUM('UNVERIFIED', 'LOCAL_ATTESTED', 'APAAR_VERIFIED', 'FLAGGED') NOT NULL DEFAULT 'LOCAL_ATTESTED',
    version INT NOT NULL DEFAULT 1,
    record_hash CHAR(64) NOT NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_inst_roll (institution_id, student_roll_no),
    INDEX idx_apaar_digest (apaar_digest),
    INDEX idx_student_prog_year (programme_id, academic_year_id),
    FOREIGN KEY (institution_id) REFERENCES institutions(id) ON DELETE CASCADE,
    FOREIGN KEY (programme_id) REFERENCES programmes(id) ON DELETE RESTRICT,
    FOREIGN KEY (academic_year_id) REFERENCES academic_years(id) ON DELETE RESTRICT,
    FOREIGN KEY (submission_batch_id) REFERENCES submission_batches(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 7. Student Record Versions (Auditable history)
CREATE TABLE IF NOT EXISTS student_record_versions (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    student_id BIGINT UNSIGNED NOT NULL,
    version_no INT NOT NULL,
    changed_by VARCHAR(255) NOT NULL,
    change_reason TEXT NOT NULL,
    snapshot_json JSON NOT NULL,
    record_hash CHAR(64) NOT NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 8. Anomalies & Duplicate Match Findings
CREATE TABLE IF NOT EXISTS anomalies (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    finding_type ENUM('DUPLICATE_APAAR', 'CROSS_INSTITUTION_ENROLMENT', 'INTAKE_BREACH', 'INVALID_APAAR_FORMAT', 'CONFLICTING_RECORD') NOT NULL,
    institution_id BIGINT UNSIGNED NOT NULL,
    programme_id BIGINT UNSIGNED NULL,
    academic_year_id BIGINT UNSIGNED NULL,
    student_id BIGINT UNSIGNED NULL,
    target_institution_id BIGINT UNSIGNED NULL,
    target_student_id BIGINT UNSIGNED NULL,
    severity ENUM('LOW', 'MEDIUM', 'HIGH', 'CRITICAL') NOT NULL DEFAULT 'MEDIUM',
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    evidence_json JSON NOT NULL,
    status ENUM('OPEN', 'INVESTIGATING', 'RESOLVED', 'DISMISSED') NOT NULL DEFAULT 'OPEN',
    reviewer_notes TEXT NULL,
    resolved_by VARCHAR(255) NULL,
    resolved_at DATETIME(6) NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (institution_id) REFERENCES institutions(id) ON DELETE CASCADE,
    FOREIGN KEY (programme_id) REFERENCES programmes(id) ON DELETE SET NULL,
    FOREIGN KEY (academic_year_id) REFERENCES academic_years(id) ON DELETE SET NULL,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE SET NULL,
    FOREIGN KEY (target_institution_id) REFERENCES institutions(id) ON DELETE SET NULL,
    FOREIGN KEY (target_student_id) REFERENCES students(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 9. Verification Requests & Auditable Verifications
CREATE TABLE IF NOT EXISTS verification_requests (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    verifier_id BIGINT UNSIGNED NULL,
    verifier_name VARCHAR(255) NOT NULL,
    verifier_org VARCHAR(255) NOT NULL,
    query_type ENUM('ROLL_NO', 'APAAR_ID', 'VERIFICATION_REF') NOT NULL,
    query_value VARCHAR(128) NOT NULL,
    institution_id BIGINT UNSIGNED NULL,
    student_id BIGINT UNSIGNED NULL,
    status ENUM('VERIFIED', 'NOT_FOUND', 'DISCREPANCY', 'RATE_LIMITED') NOT NULL,
    response_scope_json JSON NOT NULL,
    verification_ref VARCHAR(64) NOT NULL UNIQUE,
    verification_hash CHAR(64) NOT NULL,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (verifier_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (institution_id) REFERENCES institutions(id) ON DELETE SET NULL,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

