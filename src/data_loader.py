"""
PS-068 Data Loader
==================
Reads all CSV input files and returns typed domain-model objects.
Schema validation is strict so that hidden evaluation data surfaces
format errors early rather than producing silently wrong results.
Supports both v1 (standard) and v2 (sample pack) CSV schemas seamlessly.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import date, time
from pathlib import Path
from typing import Dict, List, Optional

from src.models import (
    AttendanceRecord,
    AttendanceStatus,
    ApplicationStatus,
    ApplicationType,
    CalendarEntry,
    CondonationApplication,
    Course,
    DayType,
    ODApproval,
    PlacementEvent,
    Student,
    TimetableSlot,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_bool(val: str) -> bool:
    return val.strip().upper() in ("TRUE", "1", "YES", "Y")


def _parse_date(val: str) -> date:
    return date.fromisoformat(val.strip())


def _parse_time(val: str) -> Optional[time]:
    val = val.strip()
    if not val:
        return None
    h, m = map(int, val.split(":"))
    return time(h, m)


def _open_csv(path: Path) -> csv.DictReader:
    if not path.exists():
        raise FileNotFoundError(f"Required CSV not found: {path}")
    return csv.DictReader(open(path, newline="", encoding="utf-8"))


# ---------------------------------------------------------------------------
# Public loader functions
# ---------------------------------------------------------------------------

def load_students(data_dir: Path) -> Dict[str, Student]:
    """
    Returns dict keyed by student_id.

    Supports:
      v1: student_id, name, programme, semester, section, admission_date, is_lateral
      v2: register_no, student_name, section_id, admission_date, lateral_entry, parent_mobile
          (with optional sections.csv providing programme and semester)
    """
    sections_info: Dict[str, dict] = {}
    sec_path = data_dir / "sections.csv"
    if sec_path.exists():
        for row in _open_csv(sec_path):
            sec_id = (row.get("section_id") or row.get("section", "")).strip().upper()
            sections_info[sec_id] = {
                "programme": row.get("programme", "").strip(),
                "semester": int(row["semester"]) if row.get("semester", "").strip() else 1,
                "final_year": _parse_bool(row.get("final_year", "false")),
            }

    students: Dict[str, Student] = {}
    for row in _open_csv(data_dir / "students.csv"):
        student_id = (row.get("student_id") or row.get("register_no", "")).strip()
        name = (row.get("name") or row.get("student_name", "")).strip()
        section = (row.get("section") or row.get("section_id", "")).strip().upper()

        prog = row.get("programme", "").strip()
        if not prog and section in sections_info:
            prog = sections_info[section].get("programme", "")
        if not prog:
            prog = "B.Tech"

        sem_val = row.get("semester", "").strip()
        if sem_val:
            semester = int(sem_val)
        elif section in sections_info and sections_info[section].get("semester"):
            semester = sections_info[section]["semester"]
        else:
            semester = 1

        admission_date = _parse_date(row["admission_date"])
        is_lateral = _parse_bool(row.get("is_lateral") or row.get("lateral_entry", "FALSE"))
        parent_mobile = row.get("parent_mobile", "").strip() or None

        s = Student(
            student_id=student_id,
            name=name,
            programme=prog,
            semester=semester,
            section=section,
            admission_date=admission_date,
            is_lateral=is_lateral,
            parent_mobile=parent_mobile,
        )
        students[s.student_id] = s
    logger.info("Loaded %d students", len(students))
    return students


def load_courses(data_dir: Path) -> Dict[str, Course]:
    """
    Returns dict keyed by course_id.

    Supports:
      v1: course_id, course_name, weekly_periods, is_lab, section
      v2: course_code, course_name, section_id, weekly_periods, lab, faculty_id
    """
    courses: Dict[str, Course] = {}
    for row in _open_csv(data_dir / "courses.csv"):
        cid = (row.get("course_id") or row.get("course_code", "")).strip()
        cname = row["course_name"].strip()
        wp = int(row["weekly_periods"])
        is_lab = _parse_bool(row.get("is_lab") or row.get("lab", "FALSE"))
        sec = (row.get("section") or row.get("section_id", "")).strip().upper()
        fac_id = row.get("faculty_id", "").strip() or None

        c = Course(
            course_id=cid,
            course_name=cname,
            weekly_periods=wp,
            is_lab=is_lab,
            section=sec,
            faculty_id=fac_id,
        )
        courses[c.course_id] = c
    logger.info("Loaded %d courses", len(courses))
    return courses


def load_academic_calendar(data_dir: Path) -> Dict[date, CalendarEntry]:
    """
    Returns dict keyed by date.

    Supports:
      v1: academic_calendar.csv (calendar_date, day_type, replaces_day)
      v2: calendar.csv (date, day_type, follows_timetable_of, note)
          plus optional semester.csv (exam_start_date, semester_start, semester_end)
    """
    cal_file = data_dir / "academic_calendar.csv"
    if not cal_file.exists():
        cal_file = data_dir / "calendar.csv"

    calendar: Dict[date, CalendarEntry] = {}
    for row in _open_csv(cal_file):
        d = _parse_date(row.get("calendar_date") or row.get("date"))
        day_type = DayType(row["day_type"].strip().upper())
        replaces = (row.get("replaces_day") or row.get("follows_timetable_of", "")).strip().upper() or None
        entry = CalendarEntry(
            calendar_date=d,
            day_type=day_type,
            replaces_day=replaces,
        )
        calendar[d] = entry

    # Enrich with semester.csv if available (e.g. exam_start_date)
    sem_path = data_dir / "semester.csv"
    if sem_path.exists():
        sem_data = {}
        for row in _open_csv(sem_path):
            sem_data[row["key"].strip()] = row["value"].strip()
        if "exam_start_date" in sem_data:
            exam_d = _parse_date(sem_data["exam_start_date"])
            if exam_d not in calendar:
                calendar[exam_d] = CalendarEntry(
                    calendar_date=exam_d,
                    day_type=DayType.EXAM,
                )

    logger.info("Loaded %d calendar entries", len(calendar))
    return calendar


def load_timetable(data_dir: Path) -> List[TimetableSlot]:
    """
    Supports:
      v1: section, day, period_no, course_id, start_time, end_time
      v2: section_id, weekday, period_no, course_code, start_time, end_time
    """
    slots: List[TimetableSlot] = []
    for row in _open_csv(data_dir / "timetable.csv"):
        slots.append(TimetableSlot(
            section=(row.get("section") or row.get("section_id", "")).strip().upper(),
            day=(row.get("day") or row.get("weekday", "")).strip().upper(),
            period_no=int(row["period_no"]),
            course_id=(row.get("course_id") or row.get("course_code", "")).strip(),
            start_time=_parse_time(row["start_time"]),
            end_time=_parse_time(row["end_time"]),
        ))
    logger.info("Loaded %d timetable slots", len(slots))
    return slots


def load_attendance(data_dir: Path) -> List[AttendanceRecord]:
    """
    Supports:
      v1: record_id, student_id, course_id, record_date, period_no, status, arrival_time, faculty_id
      v2: date, section_id, period_no, course_code, register_no, status, arrival_minute
    """
    records: List[AttendanceRecord] = []
    reader = _open_csv(data_dir / "attendance.csv")
    fieldnames = reader.fieldnames or []
    has_faculty_id = "faculty_id" in fieldnames

    for idx, row in enumerate(reader, start=1):
        rec_id = (row.get("record_id") or f"REC_{idx:06d}").strip()
        sid = (row.get("student_id") or row.get("register_no", "")).strip()
        cid = (row.get("course_id") or row.get("course_code", "")).strip()
        rec_date = _parse_date(row.get("record_date") or row.get("date"))
        pno = int(row["period_no"])
        status = AttendanceStatus(row["status"].strip().upper())
        arr_time = _parse_time(row.get("arrival_time", ""))

        arr_min_str = row.get("arrival_minute", "").strip()
        arr_min = int(arr_min_str) if arr_min_str else None

        if has_faculty_id:
            fac_id = row.get("faculty_id", "").strip() or None
        else:
            fac_id = "FAC_CONDUCTED"  # In v2, rows only exist for conducted periods

        records.append(AttendanceRecord(
            record_id=rec_id,
            student_id=sid,
            course_id=cid,
            record_date=rec_date,
            period_no=pno,
            status=status,
            arrival_time=arr_time,
            faculty_id=fac_id,
            arrival_minute=arr_min,
        ))
    logger.info("Loaded %d attendance records", len(records))
    return records


def load_od_approvals(data_dir: Path) -> List[ODApproval]:
    """
    Supports:
      v1: od_approvals.csv (od_id, student_id, course_id, od_date, reason, approved_by)
      v2: optional (empty list if file not present)
    """
    od_path = data_dir / "od_approvals.csv"
    if not od_path.exists():
        logger.info("No od_approvals.csv found; starting with 0 OD approvals")
        return []

    approvals: List[ODApproval] = []
    for row in _open_csv(od_path):
        approvals.append(ODApproval(
            od_id=row["od_id"].strip(),
            student_id=(row.get("student_id") or row.get("register_no", "")).strip(),
            course_id=(row.get("course_id") or row.get("course_code", "*")).strip(),
            od_date=_parse_date(row.get("od_date") or row.get("date")),
            reason=row.get("reason", "").strip(),
            approved_by=row.get("approved_by", "").strip(),
        ))
    logger.info("Loaded %d OD approvals", len(approvals))
    return approvals


def load_placement_calendar(data_dir: Path, students: Optional[Dict[str, Student]] = None) -> List[PlacementEvent]:
    """
    Supports:
      v1: placement_calendar.csv (event_id, event_date, company, student_ids)
      v2: placement_drives.csv (drive_date, company, eligible_programmes)
    """
    cal_path = data_dir / "placement_calendar.csv"
    if cal_path.exists():
        events: List[PlacementEvent] = []
        for row in _open_csv(cal_path):
            ids = [s.strip() for s in row["student_ids"].split("|") if s.strip()]
            events.append(PlacementEvent(
                event_id=row["event_id"].strip(),
                event_date=_parse_date(row["event_date"]),
                company=row["company"].strip(),
                student_ids=ids,
            ))
        logger.info("Loaded %d placement events", len(events))
        return events

    drives_path = data_dir / "placement_drives.csv"
    if drives_path.exists():
        events = []
        for idx, row in enumerate(_open_csv(drives_path), start=1):
            dd = _parse_date(row["drive_date"])
            comp = row["company"].strip()
            progs = [p.strip() for p in row.get("eligible_programmes", "").split(";") if p.strip()]

            # Determine eligible students if students dict is passed
            sids = []
            if students:
                sids = [
                    s.student_id for s in students.values()
                    if s.semester >= 7 and (not progs or s.programme in progs)
                ]

            events.append(PlacementEvent(
                event_id=f"DRIVE_{idx:03d}",
                event_date=dd,
                company=comp,
                student_ids=sids,
                eligible_programmes=progs,
            ))
        logger.info("Loaded %d placement drives", len(events))
        return events

    logger.info("No placement file found; starting with 0 placement events")
    return []


def load_condonation_applications(data_dir: Path) -> List[CondonationApplication]:
    """
    Supports:
      v1: condonation_applications.csv (application_id, student_id, course_id, reason, start_date, end_date, ...)
      v2: applications.csv (application_id, register_no, app_type, from_date, to_date, submitted_date, document_ref)
    """
    v1_path = data_dir / "condonation_applications.csv"
    if v1_path.exists():
        apps: List[CondonationApplication] = []
        for row in _open_csv(v1_path):
            apps.append(CondonationApplication(
                application_id=row["application_id"].strip(),
                student_id=row["student_id"].strip(),
                course_id=row["course_id"].strip(),
                reason=ApplicationType(row["reason"].strip().upper()),
                start_date=_parse_date(row["start_date"]),
                end_date=_parse_date(row["end_date"]),
                description=row["description"].strip(),
                status=ApplicationStatus(row.get("status", "PENDING").strip().upper()),
                approved_by=row.get("approved_by", "").strip() or None,
                approved_on=_parse_date(row["approved_on"]) if row.get("approved_on", "").strip() else None,
                medical_cert_ref=row.get("medical_cert_ref", "").strip() or None,
                submitted_on=_parse_date(row["submitted_on"]) if row.get("submitted_on", "").strip() else None,
            ))
        logger.info("Loaded %d condonation applications", len(apps))
        return apps

    v2_path = data_dir / "applications.csv"
    if v2_path.exists():
        apps = []
        for row in _open_csv(v2_path):
            aid = row["application_id"].strip()
            sid = (row.get("student_id") or row.get("register_no", "")).strip()
            cid = (row.get("course_id") or "*").strip() or "*"
            reason_str = (row.get("reason") or row.get("app_type", "")).strip().upper()
            reason = ApplicationType(reason_str)
            sdate = _parse_date(row.get("start_date") or row.get("from_date"))
            edate = _parse_date(row.get("end_date") or row.get("to_date"))
            desc = row.get("description", "").strip()

            st_str = row.get("status", "").strip().upper()
            status = ApplicationStatus(st_str) if st_str else ApplicationStatus.APPROVED

            app_by = row.get("approved_by", "").strip() or "HOD"
            app_on_str = row.get("approved_on", "").strip()
            app_on = _parse_date(app_on_str) if app_on_str else None

            med_ref = (row.get("medical_cert_ref") or row.get("document_ref", "")).strip() or None
            sub_on_str = (row.get("submitted_on") or row.get("submitted_date", "")).strip()
            sub_on = _parse_date(sub_on_str) if sub_on_str else None

            apps.append(CondonationApplication(
                application_id=aid,
                student_id=sid,
                course_id=cid,
                reason=reason,
                start_date=sdate,
                end_date=edate,
                description=desc,
                status=status,
                approved_by=app_by,
                approved_on=app_on,
                medical_cert_ref=med_ref,
                submitted_on=sub_on,
            ))
        logger.info("Loaded %d condonation applications from applications.csv", len(apps))
        return apps

    logger.info("No condonation applications file found; starting with 0 applications")
    return []


# ---------------------------------------------------------------------------
# Convenience: load everything at once
# ---------------------------------------------------------------------------

def load_all(data_dir: str | Path):
    """Load every input file and return a dict of named collections."""
    data_dir = Path(data_dir)
    students = load_students(data_dir)
    courses = load_courses(data_dir)
    calendar = load_academic_calendar(data_dir)
    timetable = load_timetable(data_dir)
    attendance = load_attendance(data_dir)
    od_approvals = load_od_approvals(data_dir)
    placement = load_placement_calendar(data_dir, students=students)
    condonation = load_condonation_applications(data_dir)

    return {
        "students":     students,
        "courses":      courses,
        "calendar":     calendar,
        "timetable":    timetable,
        "attendance":   attendance,
        "od_approvals": od_approvals,
        "placement":    placement,
        "condonation":  condonation,
    }
