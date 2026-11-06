"""
PS-068 Data Loader
==================
Reads all CSV input files and returns typed domain-model objects.
Schema validation is strict so that hidden evaluation data surfaces
format errors early rather than producing silently wrong results.
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

    CSV schema (students.csv):
        student_id, name, programme, semester, section,
        admission_date (YYYY-MM-DD), is_lateral (TRUE/FALSE)
    """
    students: Dict[str, Student] = {}
    for row in _open_csv(data_dir / "students.csv"):
        s = Student(
            student_id=row["student_id"].strip(),
            name=row["name"].strip(),
            programme=row["programme"].strip(),
            semester=int(row["semester"]),
            section=row["section"].strip().upper(),
            admission_date=_parse_date(row["admission_date"]),
            is_lateral=_parse_bool(row.get("is_lateral", "FALSE")),
        )
        students[s.student_id] = s
    logger.info("Loaded %d students", len(students))
    return students


def load_courses(data_dir: Path) -> Dict[str, Course]:
    """
    Returns dict keyed by course_id.

    CSV schema (courses.csv):
        course_id, course_name, weekly_periods, is_lab (TRUE/FALSE),
        section (blank = all sections)
    """
    courses: Dict[str, Course] = {}
    for row in _open_csv(data_dir / "courses.csv"):
        c = Course(
            course_id=row["course_id"].strip(),
            course_name=row["course_name"].strip(),
            weekly_periods=int(row["weekly_periods"]),
            is_lab=_parse_bool(row.get("is_lab", "FALSE")),
            section=row.get("section", "").strip().upper(),
        )
        courses[c.course_id] = c
    logger.info("Loaded %d courses", len(courses))
    return courses


def load_academic_calendar(data_dir: Path) -> Dict[date, CalendarEntry]:
    """
    Returns dict keyed by date.

    CSV schema (academic_calendar.csv):
        calendar_date (YYYY-MM-DD), day_type (WORKING|HOLIDAY|SATURDAY|
        SUNDAY|EXAM|WORKING_SATURDAY), replaces_day (MON|TUE|…, blank if N/A)
    """
    calendar: Dict[date, CalendarEntry] = {}
    for row in _open_csv(data_dir / "academic_calendar.csv"):
        d = _parse_date(row["calendar_date"])
        entry = CalendarEntry(
            calendar_date=d,
            day_type=DayType(row["day_type"].strip().upper()),
            replaces_day=row.get("replaces_day", "").strip().upper() or None,
        )
        calendar[d] = entry
    logger.info("Loaded %d calendar entries", len(calendar))
    return calendar


def load_timetable(data_dir: Path) -> List[TimetableSlot]:
    """
    CSV schema (timetable.csv):
        section, day (MON|TUE|WED|THU|FRI|SAT),
        period_no, course_id, start_time (HH:MM), end_time (HH:MM)
    """
    slots: List[TimetableSlot] = []
    for row in _open_csv(data_dir / "timetable.csv"):
        slots.append(TimetableSlot(
            section=row["section"].strip().upper(),
            day=row["day"].strip().upper(),
            period_no=int(row["period_no"]),
            course_id=row["course_id"].strip(),
            start_time=_parse_time(row["start_time"]),
            end_time=_parse_time(row["end_time"]),
        ))
    logger.info("Loaded %d timetable slots", len(slots))
    return slots


def load_attendance(data_dir: Path) -> List[AttendanceRecord]:
    """
    CSV schema (attendance.csv):
        record_id, student_id, course_id, record_date (YYYY-MM-DD),
        period_no, status (PRESENT|ABSENT|LATE|OD),
        arrival_time (HH:MM, blank if N/A),
        faculty_id (blank = period not conducted)
    """
    records: List[AttendanceRecord] = []
    for row in _open_csv(data_dir / "attendance.csv"):
        records.append(AttendanceRecord(
            record_id=row["record_id"].strip(),
            student_id=row["student_id"].strip(),
            course_id=row["course_id"].strip(),
            record_date=_parse_date(row["record_date"]),
            period_no=int(row["period_no"]),
            status=AttendanceStatus(row["status"].strip().upper()),
            arrival_time=_parse_time(row.get("arrival_time", "")),
            faculty_id=row.get("faculty_id", "").strip() or None,
        ))
    logger.info("Loaded %d attendance records", len(records))
    return records


def load_od_approvals(data_dir: Path) -> List[ODApproval]:
    """
    CSV schema (od_approvals.csv):
        od_id, student_id, course_id (* = all courses),
        od_date (YYYY-MM-DD), reason, approved_by
    """
    approvals: List[ODApproval] = []
    for row in _open_csv(data_dir / "od_approvals.csv"):
        approvals.append(ODApproval(
            od_id=row["od_id"].strip(),
            student_id=row["student_id"].strip(),
            course_id=row["course_id"].strip(),
            od_date=_parse_date(row["od_date"]),
            reason=row["reason"].strip(),
            approved_by=row["approved_by"].strip(),
        ))
    logger.info("Loaded %d OD approvals", len(approvals))
    return approvals


def load_placement_calendar(data_dir: Path) -> List[PlacementEvent]:
    """
    CSV schema (placement_calendar.csv):
        event_id, event_date (YYYY-MM-DD), company,
        student_ids (pipe-separated list)
    """
    events: List[PlacementEvent] = []
    for row in _open_csv(data_dir / "placement_calendar.csv"):
        ids = [s.strip() for s in row["student_ids"].split("|") if s.strip()]
        events.append(PlacementEvent(
            event_id=row["event_id"].strip(),
            event_date=_parse_date(row["event_date"]),
            company=row["company"].strip(),
            student_ids=ids,
        ))
    logger.info("Loaded %d placement events", len(events))
    return events


def load_condonation_applications(data_dir: Path) -> List[CondonationApplication]:
    """
    CSV schema (condonation_applications.csv):
        application_id, student_id, course_id,
        reason (MEDICAL|SPORTS|NCC_NSS|PLACEMENT|BEREAVEMENT),
        start_date (YYYY-MM-DD), end_date (YYYY-MM-DD),
        description, status (PENDING|APPROVED|REJECTED|REJECTED_LATE),
        approved_by (blank if not yet), approved_on (YYYY-MM-DD, blank if not yet),
        medical_cert_ref (blank if N/A),
        submitted_on (YYYY-MM-DD, blank if unknown)
    """
    apps: List[CondonationApplication] = []
    for row in _open_csv(data_dir / "condonation_applications.csv"):
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


# ---------------------------------------------------------------------------
# Convenience: load everything at once
# ---------------------------------------------------------------------------

def load_all(data_dir: str | Path):
    """Load every input file and return a dict of named collections."""
    data_dir = Path(data_dir)
    return {
        "students":    load_students(data_dir),
        "courses":     load_courses(data_dir),
        "calendar":    load_academic_calendar(data_dir),
        "timetable":   load_timetable(data_dir),
        "attendance":  load_attendance(data_dir),
        "od_approvals": load_od_approvals(data_dir),
        "placement":   load_placement_calendar(data_dir),
        "condonation": load_condonation_applications(data_dir),
    }

