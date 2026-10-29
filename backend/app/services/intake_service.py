from typing import List, Dict, Any, Optional
from app.database import fetch_all, fetch_one

def get_institution_intake_monitoring(
    institution_id: int,
    academic_year_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Computes exact enrolment vs approved intake metrics per programme for an institution.
    Evaluates regular capacity against supernumerary, NRI, and TFW quotas.
    """
    query = """
        SELECT ai.id as intake_id, ai.institution_id, ai.programme_id, ai.academic_year_id,
               ai.approved_seats, ai.supernumerary_quota, ai.nri_quota, ai.tfw_quota,
               COALESCE(ai.oci_quota, 0) as oci_quota, COALESCE(ai.fn_quota, 0) as fn_quota,
               p.code as programme_code, p.name as programme_name, p.degree_level, p.department,
               ay.year_code as academic_year,
               i.name as institution_name, i.aicte_id as institution_code,
               -- Enrolment counts by seat category
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.seat_category = 'REGULAR' THEN 1 ELSE 0 END) as regular_enrolled,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.seat_category = 'SUPERNUMERARY' THEN 1 ELSE 0 END) as supernum_enrolled,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.seat_category = 'NRI' THEN 1 ELSE 0 END) as nri_enrolled,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.seat_category = 'TFW' THEN 1 ELSE 0 END) as tfw_enrolled,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND (s.oci_status = 1 OR s.seat_category = 'OCI') THEN 1 ELSE 0 END) as oci_enrolled,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND (s.fn_status = 1 OR s.seat_category = 'FN') THEN 1 ELSE 0 END) as fn_enrolled,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' THEN 1 ELSE 0 END) as total_enrolled,
               -- Category breakdown
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.admission_category = 'GENERAL' THEN 1 ELSE 0 END) as cat_general,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.admission_category = 'OBC' THEN 1 ELSE 0 END) as cat_obc,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.admission_category = 'SC' THEN 1 ELSE 0 END) as cat_sc,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.admission_category = 'ST' THEN 1 ELSE 0 END) as cat_st,
               SUM(CASE WHEN s.enrolment_status = 'ENROLLED' AND s.admission_category = 'EWS' THEN 1 ELSE 0 END) as cat_ews
        FROM approved_intakes ai
        JOIN programmes p ON ai.programme_id = p.id
        JOIN academic_years ay ON ai.academic_year_id = ay.id
        JOIN institutions i ON ai.institution_id = i.id
        LEFT JOIN students s ON s.institution_id = ai.institution_id 
                            AND s.programme_id = ai.programme_id 
                            AND s.academic_year_id = ai.academic_year_id
        WHERE ai.institution_id = %s
    """
    args = [institution_id]
    if academic_year_id:
        query += " AND ai.academic_year_id = %s"
        args.append(academic_year_id)

    query += " GROUP BY ai.id, p.id, ay.id, i.id ORDER BY p.code ASC"
    rows = fetch_all(query, tuple(args))

    results = []
    for r in rows:
        approved = int(r["approved_seats"] or 0)
        supernum = int(r["supernumerary_quota"] or 0)
        nri = int(r["nri_quota"] or 0)
        tfw = int(r["tfw_quota"] or 0)
        oci_q = int(r["oci_quota"] or 0)
        fn_q = int(r["fn_quota"] or 0)
        total_permitted = approved + supernum + nri + tfw + oci_q + fn_q

        reg_enrolled = int(r["regular_enrolled"] or 0)
        total_enrolled = int(r["total_enrolled"] or 0)

        remaining_regular = max(0, approved - reg_enrolled)
        remaining_total = max(0, total_permitted - total_enrolled)
        
        utilization = round((total_enrolled / total_permitted * 100), 1) if total_permitted > 0 else 0.0
        regular_utilization = round((reg_enrolled / approved * 100), 1) if approved > 0 else 0.0

        is_breached = (reg_enrolled > approved) or (total_enrolled > total_permitted)

        results.append({
            "intake_id": r["intake_id"],
            "institution_id": r["institution_id"],
            "institution_name": r["institution_name"],
            "institution_code": r["institution_code"],
            "programme_id": r["programme_id"],
            "programme_code": r["programme_code"],
            "programme_name": r["programme_name"],
            "degree_level": r["degree_level"],
            "department": r["department"],
            "academic_year": r["academic_year"],
            # Quotas
            "approved_seats": approved,
            "supernumerary_quota": supernum,
            "nri_quota": nri,
            "tfw_quota": tfw,
            "oci_quota": oci_q,
            "fn_quota": fn_q,
            "total_permitted_seats": total_permitted,
            # Actual enrolments
            "regular_enrolled": reg_enrolled,
            "supernum_enrolled": int(r["supernum_enrolled"] or 0),
            "nri_enrolled": int(r["nri_enrolled"] or 0),
            "tfw_enrolled": int(r["tfw_enrolled"] or 0),
            "oci_enrolled": int(r["oci_enrolled"] or 0),
            "fn_enrolled": int(r["fn_enrolled"] or 0),
            "total_enrolled": total_enrolled,
            # Balances
            "remaining_regular_seats": remaining_regular,
            "remaining_total_seats": remaining_total,
            "utilization_percentage": utilization,
            "regular_utilization_percentage": regular_utilization,
            "is_breached": is_breached,
            "status": "EXCEEDED_APPROVED_CAP" if is_breached else ("AT_CAPACITY" if remaining_regular == 0 else "SEATS_AVAILABLE"),
            # Category breakdown
            "categories": {
                "GENERAL": int(r["cat_general"] or 0),
                "OBC": int(r["cat_obc"] or 0),
                "SC": int(r["cat_sc"] or 0),
                "ST": int(r["cat_st"] or 0),
                "EWS": int(r["cat_ews"] or 0),
                "NRI": int(r["nri_enrolled"] or 0),
                "TFW": int(r["tfw_enrolled"] or 0),
                "OCI": int(r["oci_enrolled"] or 0),
                "FN": int(r["fn_enrolled"] or 0)
            }
        })

    return results

def get_national_intake_overview(academic_year_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Aggregates national level intake and enrolment statistics across all registered institutions for AICTE Admin.
    """
    where_clause = "WHERE 1=1"
    args = []
    if academic_year_id:
        where_clause += " AND ai.academic_year_id = %s"
        args.append(academic_year_id)

    query = f"""
        SELECT 
            COUNT(DISTINCT ai.institution_id) as total_institutions,
            COUNT(DISTINCT ai.programme_id) as total_programmes_offered,
            SUM(ai.approved_seats) as total_approved_seats,
            SUM(ai.supernumerary_quota + ai.nri_quota + ai.tfw_quota + COALESCE(ai.oci_quota, 0) + COALESCE(ai.fn_quota, 0)) as total_supernumerary_quota,
            (SELECT COUNT(*) FROM students s 
             WHERE s.enrolment_status = 'ENROLLED' 
             {'AND s.academic_year_id = %s' if academic_year_id else ''}
            ) as total_active_students,
            (SELECT COUNT(*) FROM anomalies WHERE status = 'OPEN') as open_anomalies_count,
            (SELECT COUNT(*) FROM submission_batches) as total_batches_processed
        FROM approved_intakes ai
        {where_clause}
    """
    total_args = list(args)
    if academic_year_id:
        total_args.insert(0, academic_year_id)

    stats = fetch_one(query, tuple(total_args)) or {}
    total_approved = int(stats.get("total_approved_seats") or 0)
    total_students = int(stats.get("total_active_students") or 0)
    nat_utilization = round((total_students / total_approved * 100), 1) if total_approved > 0 else 0.0

    return {
        "total_institutions": int(stats.get("total_institutions") or 0),
        "total_programmes_offered": int(stats.get("total_programmes_offered") or 0),
        "total_approved_capacity": total_approved,
        "total_supernumerary_capacity": int(stats.get("total_supernumerary_quota") or 0),
        "total_active_enrolment": total_students,
        "national_enrolment_rate": nat_utilization,
        "open_anomalies_count": int(stats.get("open_anomalies_count") or 0),
        "total_batches_processed": int(stats.get("total_batches_processed") or 0)
    }

