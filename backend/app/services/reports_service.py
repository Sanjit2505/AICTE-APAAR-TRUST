import io
import csv
from typing import Optional
from app.database import fetch_all
from app.services.student_service import sanitize_csv_cell
from app.services.intake_service import get_institution_intake_monitoring

def generate_intake_report_csv(institution_id: Optional[int] = None, academic_year_id: Optional[int] = None) -> str:
    """Generates intake and enrolment monitoring report CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Institution AICTE ID",
        "Institution Name",
        "Academic Year",
        "Programme Code",
        "Programme Name",
        "Degree Level",
        "Approved Regular Seats",
        "Approved Supernumerary",
        "Total Permitted",
        "Regular Enrolled",
        "Total Enrolled",
        "Remaining Regular Seats",
        "Utilization %",
        "Compliance Status"
    ])

    if institution_id:
        intake_rows = get_institution_intake_monitoring(institution_id, academic_year_id)
    else:
        # Fetch for all institutions
        insts = fetch_all("SELECT id FROM institutions")
        intake_rows = []
        for inst in insts:
            intake_rows.extend(get_institution_intake_monitoring(inst["id"], academic_year_id))

    for r in intake_rows:
        writer.writerow([
            sanitize_csv_cell(r["institution_code"]),
            sanitize_csv_cell(r["institution_name"]),
            sanitize_csv_cell(r["academic_year"]),
            sanitize_csv_cell(r["programme_code"]),
            sanitize_csv_cell(r["programme_name"]),
            sanitize_csv_cell(r["degree_level"]),
            r["approved_seats"],
            r["supernumerary_quota"],
            r["total_permitted_seats"],
            r["regular_enrolled"],
            r["total_enrolled"],
            r["remaining_regular_seats"],
            f"{r['utilization_percentage']}%",
            sanitize_csv_cell(r["status"])
        ])

    return output.getvalue()

def generate_validation_errors_csv(batch_id: int) -> str:
    """Exports row-by-row validation errors for an uploaded CSV batch."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Batch ID",
        "Row Number",
        "Student Roll No",
        "Field Name",
        "Error Code",
        "Error Message",
        "Timestamp"
    ])

    errors = fetch_all("""
        SELECT bvr.*, sb.batch_ref 
        FROM batch_validation_results bvr
        JOIN submission_batches sb ON bvr.batch_id = sb.id
        WHERE bvr.batch_id = %s
        ORDER BY bvr.row_num ASC
    """, (batch_id,))

    for e in errors:
        writer.writerow([
            sanitize_csv_cell(e.get("batch_ref")),
            e["row_num"],
            sanitize_csv_cell(e.get("student_roll_no") or "N/A"),
            sanitize_csv_cell(e["field_name"]),
            sanitize_csv_cell(e["error_code"]),
            sanitize_csv_cell(e["error_message"]),
            e["created_at"]
        ])

    return output.getvalue()

def generate_anomalies_report_csv(institution_id: Optional[int] = None) -> str:
    """Exports flagged anomalies and duplicate matches as a CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Anomaly ID",
        "Finding Type",
        "Severity",
        "Title",
        "Institution Name",
        "Target Institution",
        "Programme",
        "Status",
        "Reviewer",
        "Resolution Notes",
        "Logged At"
    ])

    query = """
        SELECT a.*, i.name as inst_name, ti.name as target_inst_name, p.code as prog_code
        FROM anomalies a
        JOIN institutions i ON a.institution_id = i.id
        LEFT JOIN institutions ti ON a.target_institution_id = ti.id
        LEFT JOIN programmes p ON a.programme_id = p.id
        WHERE 1=1
    """
    args = []
    if institution_id:
        query += " AND (a.institution_id = %s OR a.target_institution_id = %s)"
        args.extend([institution_id, institution_id])

    query += " ORDER BY a.id DESC"
    rows = fetch_all(query, tuple(args))

    for r in rows:
        writer.writerow([
            r["id"],
            sanitize_csv_cell(r["finding_type"]),
            sanitize_csv_cell(r["severity"]),
            sanitize_csv_cell(r["title"]),
            sanitize_csv_cell(r["inst_name"]),
            sanitize_csv_cell(r.get("target_inst_name") or "N/A"),
            sanitize_csv_cell(r.get("prog_code") or "N/A"),
            sanitize_csv_cell(r["status"]),
            sanitize_csv_cell(r.get("resolved_by") or "UNASSIGNED"),
            sanitize_csv_cell(r.get("reviewer_notes") or ""),
            r["created_at"]
        ])

    return output.getvalue()

