"""
PS-068 Student Attendance and Condonation / Eligibility Engine — v2
====================================================================
Self-contained engine that processes the new sample data pack with pipe-delimited commands.

Usage:
    python engine_v2.py --data-dir <dir> --commands <commands.txt> --output-dir <dir>

Defaults:
    --data-dir   data_v2/
    --commands   data_v2/commands.txt
    --output-dir output/

Output files:
    course_attendance.csv
    overall_attendance.csv
    application_results.csv
    letters.csv
"""
from __future__ import annotations

import csv
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("engine_v2")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LATE_GRACE_MINUTES = 10
MEDICAL_CAP_PCT = Decimal("15")          # 15 % of conducted per course
DUTY_SEMESTER_CAP = 20                   # max duty days per semester
DUTY_COURSE_CAP_PCT = Decimal("10")     # 10 % of post-medical conducted per course
BEREAVEMENT_MAX_DAYS = 5                 # consecutive calendar days
BEREAVEMENT_SUBMIT_WINDOW = 3            # days after to_date + 1 (return date)
MEDICAL_SUBMIT_WINDOW = 7               # days after return
FINE_PER_DAY = Decimal("100")
FINE_MAX = Decimal("2000")
FEE_TIER1 = Decimal("500")              # 70–74.99 %
FEE_TIER2 = Decimal("1000")            # 65–69.99 %
ELIGIBLE_THRESHOLD = Decimal("75")
CONDONABLE_THRESHOLD = Decimal("65")
CONDONABLE_MAX_COURSES = 2
WARN1_THRESHOLD = Decimal("80")          # below 80 → FIRST_WARNING
WARN2_THRESHOLD = Decimal("75")          # below 75 → SECOND_WARNING / PARENT_MEETING


# ---------------------------------------------------------------------------
# Round-half-up helper
# ---------------------------------------------------------------------------
def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pct(attended: int, conducted: int) -> Decimal:
    if conducted == 0:
        return Decimal("0.00")
    return _round2(Decimal(attended) / Decimal(conducted) * Decimal("100"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Semester:
    start: date
    end: date
    exam_start: date
    freeze_date: date
    monthly_cutoffs: List[date]


@dataclass
class CalendarDay:
    date: date
    day_type: str                     # WORKING | HOLIDAY | WORKING_SATURDAY
    follows_timetable_of: str = ""   # MON..FRI for WORKING_SATURDAY


@dataclass
class Section:
    section_id: str
    programme: str
    semester: int
    final_year: bool


@dataclass
class Course:
    course_code: str
    course_name: str
    section_id: str
    weekly_periods: int
    is_lab: bool
    faculty_id: str

    def weight(self) -> int:
        return 2 if self.is_lab else 1


@dataclass
class TimetableSlot:
    section_id: str
    weekday: str           # MON..FRI
    period_no: int
    course_code: str
    start_time: str        # HH:MM
    end_time: str


@dataclass
class Student:
    register_no: str
    student_name: str
    section_id: str
    admission_date: date
    lateral_entry: bool
    parent_mobile: str


@dataclass
class PlacementDrive:
    drive_date: date
    company: str
    eligible_programmes: List[str]


@dataclass
class AttendanceRecord:
    date: date
    section_id: str
    period_no: int
    course_code: str
    register_no: str
    status: str            # PRESENT | ABSENT | LATE | OD
    arrival_minute: Optional[int]  # only for LATE


@dataclass
class Application:
    application_id: str
    register_no: str
    app_type: str          # MEDICAL | SPORTS | NCC_NSS | PLACEMENT | BEREAVEMENT
    from_date: date
    to_date: date
    submitted_date: date
    document_ref: str
    # Set by DECIDE_APPLICATION command:
    hod_decision: str = ""         # APPROVED | REJECTED
    decision_ts: str = ""
    # Final computed status (set during processing):
    final_status: str = ""         # APPROVED | REJECTED_LATE | REJECTED_FROZEN | REJECTED_DOCS | REJECTED_NOT_ELIGIBLE | CAPPED
    reason_code: str = ""
    days_counted: int = 0


# ---------------------------------------------------------------------------
# Audit log entry
# ---------------------------------------------------------------------------
@dataclass
class CorrectionAuditEntry:
    ts: str
    command: str
    date: date
    section_id: str
    period_no: int
    register_no: str
    old_status: str
    new_status: str
    allowed: bool
    reason: str


# ---------------------------------------------------------------------------
# Warning letter
# ---------------------------------------------------------------------------
@dataclass
class WarningLetter:
    cutoff_date: date
    register_no: str
    letter_type: str           # FIRST_WARNING | SECOND_WARNING | PARENT_MEETING
    overall_percentage: Decimal
    parent_copy: bool


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class AttendanceEngine:
    def __init__(self):
        self.semester: Optional[Semester] = None
        self.calendar: Dict[date, CalendarDay] = {}
        self.sections: Dict[str, Section] = {}
        self.courses: Dict[str, Course] = {}
        # (section_id, weekday, period_no) -> TimetableSlot
        self.timetable: Dict[Tuple[str, str, int], TimetableSlot] = {}
        self.students: Dict[str, Student] = {}
        self.placement_drives: List[PlacementDrive] = []
        # Attendance: (register_no, date, course_code, period_no) -> AttendanceRecord
        self.attendance: Dict[Tuple, AttendanceRecord] = {}
        # Grouped for fast lookup
        self._att_by_student_course: Dict[Tuple[str, str], List[AttendanceRecord]] = defaultdict(list)
        self.applications: Dict[str, Application] = {}
        # OD approvals: (register_no, date) -> approval_ref
        self.od_approvals: Dict[Tuple[str, date], str] = {}
        # Audit log
        self.audit_log: List[CorrectionAuditEntry] = []
        # Warning letter state
        self._warning_history: Dict[str, List[str]] = defaultdict(list)  # register_no -> [letter_types issued]
        self._monthly_warnings: Dict[Tuple[str, date], WarningLetter] = {}  # (register_no, cutoff) -> letter
        # Monthly cutoff results (processed cutoffs)
        self._processed_cutoffs: Set[date] = set()
        # Output accumulator
        self._course_att_rows: List[dict] = []
        self._overall_att_rows: List[dict] = []
        self._letter_rows: List[WarningLetter] = []
        # Deferred print queues (resolved after condonation processing)
        self._pending_registers: List[Tuple[str, date]] = []
        self._pending_eligibility: List[date] = []
        self._pending_shortage: List[date] = []
        self._pending_cutoffs: List[date] = []
        self._pending_letter_months: List[str] = []

    # -----------------------------------------------------------------------
    # Loaders
    # -----------------------------------------------------------------------
    def load_semester(self, path: Path):
        data = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                data[row["key"]] = row["value"]
        cutoffs = [date.fromisoformat(d) for d in data["monthly_cutoffs"].split(";")]
        self.semester = Semester(
            start=date.fromisoformat(data["semester_start"]),
            end=date.fromisoformat(data["semester_end"]),
            exam_start=date.fromisoformat(data["exam_start_date"]),
            freeze_date=date.fromisoformat(data["freeze_date"]),
            monthly_cutoffs=cutoffs,
        )
        log.info("Semester: %s to %s, freeze=%s", self.semester.start, self.semester.end, self.semester.freeze_date)

    def load_calendar(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = date.fromisoformat(row["date"])
                self.calendar[d] = CalendarDay(
                    date=d,
                    day_type=row["day_type"],
                    follows_timetable_of=row.get("follows_timetable_of", "") or "",
                )
        log.info("Calendar: %d days loaded", len(self.calendar))

    def load_sections(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                s = Section(
                    section_id=row["section_id"],
                    programme=row["programme"],
                    semester=int(row["semester"]),
                    final_year=row["final_year"].strip().lower() == "true",
                )
                self.sections[s.section_id] = s
        log.info("Sections: %d", len(self.sections))

    def load_courses(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                c = Course(
                    course_code=row["course_code"],
                    course_name=row["course_name"],
                    section_id=row["section_id"],
                    weekly_periods=int(row["weekly_periods"]),
                    is_lab=row["lab"].strip().lower() == "true",
                    faculty_id=row.get("faculty_id", ""),
                )
                self.courses[c.course_code] = c
        log.info("Courses: %d", len(self.courses))

    def load_timetable(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slot = TimetableSlot(
                    section_id=row["section_id"],
                    weekday=row["weekday"].upper(),
                    period_no=int(row["period_no"]),
                    course_code=row["course_code"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                )
                self.timetable[(slot.section_id, slot.weekday, slot.period_no)] = slot
        log.info("Timetable: %d slots", len(self.timetable))

    def load_students(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                s = Student(
                    register_no=row["register_no"],
                    student_name=row["student_name"],
                    section_id=row["section_id"],
                    admission_date=date.fromisoformat(row["admission_date"]),
                    lateral_entry=row["lateral_entry"].strip().lower() == "true",
                    parent_mobile=row.get("parent_mobile", ""),
                )
                self.students[s.register_no] = s
        log.info("Students: %d", len(self.students))

    def load_placement_drives(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                programmes = [p.strip() for p in row["eligible_programmes"].split(";")]
                self.placement_drives.append(PlacementDrive(
                    drive_date=date.fromisoformat(row["drive_date"]),
                    company=row["company"],
                    eligible_programmes=programmes,
                ))
        log.info("Placement drives: %d", len(self.placement_drives))

    def load_attendance(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                arr = row.get("arrival_minute", "").strip()
                rec = AttendanceRecord(
                    date=date.fromisoformat(row["date"]),
                    section_id=row["section_id"],
                    period_no=int(row["period_no"]),
                    course_code=row["course_code"],
                    register_no=row["register_no"],
                    status=row["status"].strip().upper(),
                    arrival_minute=int(arr) if arr else None,
                )
                key = (rec.register_no, rec.date, rec.course_code, rec.period_no)
                self.attendance[key] = rec
                self._att_by_student_course[(rec.register_no, rec.course_code)].append(rec)
        log.info("Attendance records: %d", len(self.attendance))

    def load_applications(self, path: Path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                app = Application(
                    application_id=row["application_id"],
                    register_no=row["register_no"],
                    app_type=row["app_type"].strip().upper(),
                    from_date=date.fromisoformat(row["from_date"]),
                    to_date=date.fromisoformat(row["to_date"]),
                    submitted_date=date.fromisoformat(row["submitted_date"]),
                    document_ref=row.get("document_ref", "").strip(),
                )
                self.applications[app.application_id] = app
        log.info("Applications: %d", len(self.applications))

    # -----------------------------------------------------------------------
    # Calendar helpers
    # -----------------------------------------------------------------------
    def is_holiday(self, d: date) -> bool:
        cal = self.calendar.get(d)
        if cal is None:
            return True   # dates not in calendar treated as non-working
        return cal.day_type == "HOLIDAY"

    def is_working(self, d: date) -> bool:
        cal = self.calendar.get(d)
        if cal is None:
            return False
        return cal.day_type in ("WORKING", "WORKING_SATURDAY")

    def timetable_weekday(self, d: date) -> str:
        """Return the effective weekday label for timetable lookup."""
        cal = self.calendar.get(d)
        if cal is None:
            return ""
        if cal.day_type == "WORKING_SATURDAY":
            return cal.follows_timetable_of.upper()
        days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        return days[d.weekday()]

    def _working_days_in_range(self, from_date: date, to_date: date) -> List[date]:
        """Return all working days in [from_date, to_date]."""
        result = []
        cur = from_date
        while cur <= to_date:
            if self.is_working(cur):
                result.append(cur)
            cur += timedelta(days=1)
        return result

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------
    def cmd_decide_application(self, args: List[str], ts: str):
        if len(args) < 3:
            log.error("DECIDE_APPLICATION: need app_id|decision|ts, got %s", args)
            return
        app_id, decision = args[0], args[1].upper()
        app = self.applications.get(app_id)
        if app is None:
            log.warning("DECIDE_APPLICATION: unknown app %s", app_id)
            return
        app.hod_decision = decision
        app.decision_ts = ts or args[2] if len(args) > 2 else ""

    def cmd_approve_od(self, args: List[str], ts: str):
        # APPROVE_OD|register_no|date|approval_ref|ts
        if len(args) < 3:
            log.error("APPROVE_OD: need register_no|date|approval_ref, got %s", args)
            return
        reg_no = args[0]
        d = date.fromisoformat(args[1])
        ref = args[2]
        self.od_approvals[(reg_no, d)] = ref
        log.info("OD approved: %s on %s (%s)", reg_no, d, ref)

    def cmd_correction(self, args: List[str], is_principal: bool, ts: str):
        # date|section_id|period_no|register_no|new_status|ts
        if len(args) < 5:
            log.error("CORRECTION: not enough args: %s", args)
            return
        d = date.fromisoformat(args[0])
        section_id = args[1]
        period_no = int(args[2])
        reg_no = args[3]
        new_status = args[4].upper()
        cmd_name = "PRINCIPAL_CORRECTION" if is_principal else "CORRECTION"

        # Determine if post-freeze: check command timestamp (ts), not the attendance date
        # The freeze locks the attendance register; commands submitted after freeze_date
        # require principal authorization regardless of which date they correct.
        after_freeze = False
        if ts and len(ts) >= 10:
            try:
                cmd_date = date.fromisoformat(ts[:10])
                after_freeze = (cmd_date >= self.semester.freeze_date)
            except ValueError:
                after_freeze = False

        allowed = True
        reason = "OK"
        if after_freeze and not is_principal:
            allowed = False
            reason = "POST_FREEZE_REQUIRES_PRINCIPAL"

        old_status = "UNKNOWN"
        if allowed:
            # Find the record
            # We need to find the course for this section/period on this date
            weekday = self.timetable_weekday(d)
            slot = self.timetable.get((section_id, weekday, period_no))
            if slot is None:
                log.warning("CORRECTION: no timetable slot for %s %s p%d on %s", section_id, weekday, period_no, d)
                return
            course_code = slot.course_code
            key = (reg_no, d, course_code, period_no)
            rec = self.attendance.get(key)
            if rec is None:
                log.warning("CORRECTION: no attendance record for %s", key)
                old_status = "NO_RECORD"
                # Insert a new record
                new_rec = AttendanceRecord(
                    date=d,
                    section_id=section_id,
                    period_no=period_no,
                    course_code=course_code,
                    register_no=reg_no,
                    status=new_status,
                    arrival_minute=None,
                )
                self.attendance[key] = new_rec
                self._att_by_student_course[(reg_no, course_code)].append(new_rec)
            else:
                old_status = rec.status
                rec.status = new_status

        self.audit_log.append(CorrectionAuditEntry(
            ts=ts,
            command=cmd_name,
            date=d,
            section_id=section_id,
            period_no=period_no,
            register_no=reg_no,
            old_status=old_status,
            new_status=new_status,
            allowed=allowed,
            reason=reason,
        ))
        if not allowed:
            log.warning("CORRECTION rejected post-freeze for %s on %s (not principal)", reg_no, d)

    def cmd_monthly_cutoff(self, args: List[str]):
        if not args:
            return
        cutoff = date.fromisoformat(args[0])
        log.info("Queuing MONTHLY_CUTOFF for %s", cutoff)
        self._pending_cutoffs.append(cutoff)

    def cmd_print_letters(self, args: List[str]):
        # month e.g. "2026-08"
        if not args:
            return
        month_str = args[0].strip()
        self._pending_letter_months.append(month_str)

    def cmd_print_register(self, args: List[str]):
        if len(args) < 2:
            log.error("PRINT_REGISTER: need section_id|as_of_date")
            return
        section_id = args[0]
        as_of = date.fromisoformat(args[1])
        # Queue for post-condonation processing
        self._pending_registers.append((section_id, as_of))

    def cmd_print_eligibility(self, args: List[str]):
        if not args:
            return
        as_of = date.fromisoformat(args[0])
        self._pending_eligibility.append(as_of)

    def cmd_print_shortage(self, args: List[str]):
        if not args:
            return
        as_of = date.fromisoformat(args[0])
        self._pending_shortage.append(as_of)

    # -----------------------------------------------------------------------
    # Core attendance calculation (after all condonations applied)
    # -----------------------------------------------------------------------
    def _get_effective_start(self, student: Student) -> date:
        """R7: admission-date rule.
        
        Lateral-entry rule (from brief EC-06):
        - Admitted within 30 days of semester_start: counted from admission_date.
        - Admitted on day 31 or later: counted from semester_start (prior periods = absent).
        """
        if student.lateral_entry:
            delta = (student.admission_date - self.semester.start).days
            if delta <= 30:
                return student.admission_date
            else:
                return self.semester.start
        return self.semester.start

    def _is_late_present(self, rec: AttendanceRecord) -> bool:
        """R5: LATE counts as PRESENT if arrival_minute <= LATE_GRACE_MINUTES."""
        if rec.arrival_minute is None:
            return False
        return rec.arrival_minute <= LATE_GRACE_MINUTES

    def _is_od_approved(self, reg_no: str, d: date) -> bool:
        """Check if OD is approved for student on date."""
        return (reg_no, d) in self.od_approvals

    def _get_app_working_days(self, app: Application) -> List[date]:
        """Return working days within application date range."""
        return self._working_days_in_range(app.from_date, app.to_date)

    # -----------------------------------------------------------------------
    # Application validation & processing
    # -----------------------------------------------------------------------
    def process_all_applications(self):
        """Validate and assign final_status to every application."""
        # Group apps by student for BEREAVEMENT once-per-semester check
        bereavement_by_student: Dict[str, List[Application]] = defaultdict(list)

        for app in self.applications.values():
            self._validate_application(app, bereavement_by_student)

    def _validate_application(self, app: Application, bereavement_by_student: Dict):
        """Validate an application and set final_status and reason_code."""
        # Only process HOD-approved applications through the rules
        if app.hod_decision != "APPROVED":
            # Treat HOD-rejected as REJECTED
            app.final_status = "REJECTED"
            app.reason_code = "HOD_REJECTED"
            app.days_counted = 0
            return

        student = self.students.get(app.register_no)
        if student is None:
            app.final_status = "REJECTED_NOT_ELIGIBLE"
            app.reason_code = "UNKNOWN_STUDENT"
            return

        # EC-08: Freeze check — submitted on or after freeze date
        if app.submitted_date >= self.semester.freeze_date:
            app.final_status = "REJECTED_FROZEN"
            app.reason_code = "POST_FREEZE"
            return

        # App-type specific validation
        if app.app_type == "MEDICAL":
            self._validate_medical(app, student)
        elif app.app_type in ("SPORTS", "NCC_NSS"):
            self._validate_duty(app, student)
        elif app.app_type == "PLACEMENT":
            self._validate_placement(app, student)
        elif app.app_type == "BEREAVEMENT":
            self._validate_bereavement(app, student, bereavement_by_student)
        else:
            app.final_status = "REJECTED"
            app.reason_code = "UNKNOWN_TYPE"

    def _validate_medical(self, app: Application, student: Student):
        # Submission window: within 7 days of return date (to_date + 1)
        return_date = app.to_date + timedelta(days=1)
        submit_deadline = return_date + timedelta(days=MEDICAL_SUBMIT_WINDOW - 1)
        if app.submitted_date > submit_deadline:
            app.final_status = "REJECTED_LATE"
            app.reason_code = "SUBMITTED_LATE"
            return
        # Document required
        if not app.document_ref:
            app.final_status = "REJECTED_DOCS"
            app.reason_code = "NO_DOCUMENT"
            return
        # Working days in range
        working_days = self._get_app_working_days(app)
        app.final_status = "APPROVED"
        app.reason_code = ""
        app.days_counted = len(working_days)

    def _validate_duty(self, app: Application, student: Student):
        # Document required for SPORTS / NCC_NSS
        if not app.document_ref:
            app.final_status = "REJECTED_DOCS"
            app.reason_code = "NO_DOCUMENT"
            return
        working_days = self._get_app_working_days(app)
        app.final_status = "APPROVED"
        app.reason_code = ""
        app.days_counted = len(working_days)

    def _validate_placement(self, app: Application, student: Student):
        section = self.sections.get(student.section_id)
        # Must be final year
        if section is None or not section.final_year:
            app.final_status = "REJECTED_NOT_ELIGIBLE"
            app.reason_code = "NOT_FINAL_YEAR"
            return
        # Date(s) must be in placement_drives for student's programme
        programme = section.programme
        cur = app.from_date
        valid_days = []
        while cur <= app.to_date:
            if self.is_working(cur):
                is_drive_day = any(
                    d.drive_date == cur and programme in d.eligible_programmes
                    for d in self.placement_drives
                )
                if is_drive_day:
                    valid_days.append(cur)
                else:
                    # Invalid placement date — reject entire application
                    app.final_status = "REJECTED_NOT_ELIGIBLE"
                    app.reason_code = "INVALID_PLACEMENT_DATE"
                    return
            cur += timedelta(days=1)
        app.final_status = "APPROVED"
        app.reason_code = ""
        app.days_counted = len(valid_days)

    def _validate_bereavement(self, app: Application, student: Student, bereavement_by_student: Dict):
        # Max 5 consecutive calendar days
        total_calendar_days = (app.to_date - app.from_date).days + 1
        if total_calendar_days > BEREAVEMENT_MAX_DAYS:
            app.final_status = "REJECTED_NOT_ELIGIBLE"
            app.reason_code = "EXCEEDS_5_DAYS"
            return
        # Submission within 3 days of return
        return_date = app.to_date + timedelta(days=1)
        submit_deadline = return_date + timedelta(days=BEREAVEMENT_SUBMIT_WINDOW - 1)
        if app.submitted_date > submit_deadline:
            app.final_status = "REJECTED_LATE"
            app.reason_code = "SUBMITTED_LATE"
            return
        # Once per semester
        prev = bereavement_by_student.get(app.register_no, [])
        already_approved = [a for a in prev if a.final_status == "APPROVED"]
        if already_approved:
            app.final_status = "REJECTED_NOT_ELIGIBLE"
            app.reason_code = "BEREAVEMENT_ALREADY_USED"
            return
        # No document required
        working_days = self._get_app_working_days(app)
        app.final_status = "APPROVED"
        app.reason_code = ""
        app.days_counted = len(working_days)
        bereavement_by_student[app.register_no].append(app)

    # -----------------------------------------------------------------------
    # Attendance calculation with condonation
    # -----------------------------------------------------------------------
    def _calc_student_course(
        self, student: Student, course_code: str, as_of: date
    ) -> dict:
        """
        Compute attendance for one student+course pair up to as_of date.
        Returns dict matching course_attendance.csv columns.
        """
        effective_start = self._get_effective_start(student)
        records = self._att_by_student_course.get((student.register_no, course_code), [])
        course = self.courses.get(course_code)
        if course is None:
            return {}
        weight = course.weight()

        # Pass 1: raw counts (pre-medical)
        raw_conducted = 0
        raw_attended = 0  # counting PRESENT + valid LATE + approved OD

        # Collect dates where this student had periods
        conducted_dates: Set[date] = set()

        for rec in records:
            d = rec.date
            if d < effective_start or d > as_of:
                continue
            if self.is_holiday(d):
                continue
            # Period is conducted (record exists means faculty marked it)
            raw_conducted += weight
            conducted_dates.add(d)

            status = rec.status
            if status == "PRESENT":
                raw_attended += weight
            elif status == "LATE":
                if self._is_late_present(rec):
                    raw_attended += weight
                # else ABSENT
            elif status == "OD":
                if self._is_od_approved(rec.register_no, d):
                    raw_attended += weight
                # else ABSENT
            # ABSENT → 0

        # Pass 2: Medical removal
        medical_removed = 0  # periods removed (both from conducted AND attended)
        cap_periods = int((Decimal(raw_conducted) * MEDICAL_CAP_PCT / Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
        # Actually, cap is floor according to typical interpretation, but README says "cap decides how many come off"
        # We'll use floor for the cap (as it limits upward)
        cap_periods_floor = int(Decimal(raw_conducted) * MEDICAL_CAP_PCT / Decimal("100"))

        for app in self.applications.values():
            if (app.register_no == student.register_no and
                    app.app_type == "MEDICAL" and
                    app.final_status == "APPROVED"):
                # Count periods for this course in medical range
                periods_in_range = sum(
                    weight
                    for rec in records
                    if app.from_date <= rec.date <= app.to_date
                    and rec.date >= effective_start
                    and not self.is_holiday(rec.date)
                )
                # Total medical removed so far
                already_removed = medical_removed
                # Cap: total medical_removed <= cap_periods_floor
                can_remove = max(0, cap_periods_floor - already_removed)
                actually_remove = min(periods_in_range, can_remove)
                medical_removed += actually_remove

        post_medical_conducted = raw_conducted - medical_removed
        post_medical_attended = raw_attended - medical_removed  # medical days were absent (removed from both)
        # Clamp
        post_medical_attended = max(0, post_medical_attended)

        # Pass 3: Duty additions (SPORTS, NCC_NSS, PLACEMENT, BEREAVEMENT)
        duty_added = 0
        # Semester-level cap: collect all approved duty apps for this student
        # We need to track semester-wide duty days for cap enforcement
        # (done per student, not per course — handled in _calc_student_overall)
        # For per-course: 10% of post_medical_conducted cap
        course_duty_cap = int(Decimal(post_medical_conducted) * DUTY_COURSE_CAP_PCT / Decimal("100"))

        for app in self.applications.values():
            if (app.register_no == student.register_no and
                    app.app_type in ("SPORTS", "NCC_NSS", "PLACEMENT", "BEREAVEMENT") and
                    app.final_status == "APPROVED"):
                # Count periods for this course in duty range that were ABSENT
                for rec in records:
                    if app.from_date <= rec.date <= app.to_date:
                        d = rec.date
                        if d < effective_start or d > as_of:
                            continue
                        if self.is_holiday(d):
                            continue
                        # Only add if was absent (or OD unapproved, etc.)
                        effective_status = self._effective_status(rec)
                        if effective_status == "ABSENT":
                            duty_added += weight

        # Apply per-course duty cap
        duty_added = min(duty_added, course_duty_cap)

        # NOTE: Semester-wide 20-day cap applied separately; here we just use per-course cap
        # (Semester cap enforcement done globally in _calc_student_overall)

        final_attended = post_medical_attended + duty_added
        final_conducted = post_medical_conducted
        pct = _pct(final_attended, final_conducted)

        return {
            "conducted": final_conducted,
            "medical_removed": medical_removed,
            "attended": final_attended,
            "duty_added": duty_added,
            "percentage": pct,
            "raw_conducted": raw_conducted,
            "raw_attended": raw_attended,
            "post_medical_conducted": post_medical_conducted,
        }

    def _effective_status(self, rec: AttendanceRecord) -> str:
        """Determine effective status (after LATE/OD rules)."""
        if rec.status == "PRESENT":
            return "PRESENT"
        elif rec.status == "LATE":
            return "PRESENT" if self._is_late_present(rec) else "ABSENT"
        elif rec.status == "OD":
            return "PRESENT" if self._is_od_approved(rec.register_no, rec.date) else "ABSENT"
        return "ABSENT"

    # -----------------------------------------------------------------------
    # Eligibility determination
    # -----------------------------------------------------------------------
    def _determine_course_status(self, pct: Decimal) -> str:
        if pct >= ELIGIBLE_THRESHOLD:
            return "ELIGIBLE"
        elif pct >= CONDONABLE_THRESHOLD:
            return "CONDONABLE"
        return "DETAINED"

    def _determine_overall_eligibility(
        self, course_results: List[dict]
    ) -> Tuple[str, List[dict]]:
        """
        Apply eligibility rules across all courses for a student.
        Returns (semester_status, updated_course_results)
        """
        statuses = [r["status"] for r in course_results]
        detained_count = statuses.count("DETAINED")
        condonable_count = statuses.count("CONDONABLE")

        # Rule: max 2 condonable courses; 3rd becomes DETAINED
        if condonable_count > CONDONABLE_MAX_COURSES:
            # Sort by percentage ascending to decide which get condoned (highest %)
            condonable_courses = sorted(
                [r for r in course_results if r["status"] == "CONDONABLE"],
                key=lambda x: x["percentage"],
                reverse=True,
            )
            # Top 2 get CONDONABLE (with fee), rest become DETAINED
            for i, r in enumerate(condonable_courses):
                if i >= CONDONABLE_MAX_COURSES:
                    r["status"] = "DETAINED"
                    detained_count += 1

        # Recalculate
        detained_count = sum(1 for r in course_results if r["status"] == "DETAINED")
        condonable_count = sum(1 for r in course_results if r["status"] == "CONDONABLE")

        # 3+ detained → whole semester DETAINED
        if detained_count >= 3:
            semester_status = "DETAINED"
        elif detained_count > 0:
            semester_status = "DETAINED"  # any detained course → semester DETAINED
        else:
            semester_status = "ELIGIBLE"

        # Assign CONDONED to condonable courses that are allowed
        for r in course_results:
            if r["status"] == "CONDONABLE":
                r["status"] = "CONDONED"

        return semester_status, course_results

    def _course_fee(self, pct: Decimal) -> Decimal:
        if pct >= Decimal("70") and pct < Decimal("75"):
            return FEE_TIER1
        elif pct >= CONDONABLE_THRESHOLD and pct < Decimal("70"):
            return FEE_TIER2
        return Decimal("0")

    # -----------------------------------------------------------------------
    # Absence fine calculation
    # -----------------------------------------------------------------------
    def _calc_absence_fine(self, student: Student, as_of: date) -> Decimal:
        """
        Find unbroken stretches of 3+ full-day absences without approved leave.
        A holiday does NOT break a streak (EC-07: treat holiday as non-working,
        so the stretch continues across it).
        """
        # Get all working days in semester up to as_of
        working_days = self._working_days_in_range(self.semester.start, as_of)
        effective_start = self._get_effective_start(student)

        # For each working day, determine if student was fully absent (all periods absent)
        # Collect approved leave dates
        approved_leave_dates: Set[date] = set()
        for app in self.applications.values():
            if app.register_no == student.register_no and app.final_status == "APPROVED":
                cur = app.from_date
                while cur <= app.to_date:
                    approved_leave_dates.add(cur)
                    cur += timedelta(days=1)

        fine = Decimal("0")
        streak = 0
        all_days = sorted(d for d in self.calendar if self.semester.start <= d <= as_of)

        for d in all_days:
            cal = self.calendar.get(d)
            if cal is None:
                continue
            if d < effective_start:
                continue
            if cal.day_type == "HOLIDAY":
                # Holidays don't break streaks (EC-07: streak continues across holiday)
                # But don't count the holiday itself
                continue
            # Working day
            if d in approved_leave_dates:
                streak = 0
                continue
            # Check if student was fully absent this day
            if self._is_fully_absent(student.register_no, d):
                streak += 1
                if streak >= 3:
                    fine += FINE_PER_DAY
            else:
                streak = 0

        return min(fine, FINE_MAX)

    def _is_fully_absent(self, register_no: str, d: date) -> bool:
        """True if the student had at least one period but was absent for all."""
        # Get all courses for student's section
        student = self.students.get(register_no)
        if student is None:
            return False
        # Find records for this student on this date
        day_records = [
            rec for rec in self.attendance.values()
            if rec.register_no == register_no and rec.date == d
        ]
        if not day_records:
            return False
        for rec in day_records:
            eff = self._effective_status(rec)
            if eff == "PRESENT":
                return False
        return True

    # -----------------------------------------------------------------------
    # Warning letter evaluation
    # -----------------------------------------------------------------------
    def _evaluate_warnings(self, cutoff: date):
        """Evaluate warning letters for all students at this cutoff."""
        for register_no, student in self.students.items():
            # Calculate overall percentage at this cutoff
            courses_for_student = [
                cc for cc, c in self.courses.items()
                if c.section_id == student.section_id
            ]
            total_conducted = 0
            total_attended = 0
            for cc in courses_for_student:
                result = self._calc_student_course(student, cc, cutoff)
                if result:
                    total_conducted += result["conducted"]
                    total_attended += result["attended"]
            overall_pct = _pct(total_attended, total_conducted)

            history = self._warning_history[register_no]

            if overall_pct < WARN2_THRESHOLD:
                # Check if already issued SECOND_WARNING
                if "SECOND_WARNING" in history:
                    # 3rd month → PARENT_MEETING (but only if not already issued)
                    months_below_75 = sum(
                        1 for lt in history if lt in ("SECOND_WARNING",)
                    )
                    # Actually: count how many cutoffs they were below 75
                    # We track via PARENT_MEETING logic:
                    # Issue PARENT_MEETING if below 75 for 3rd month
                    if "PARENT_MEETING" not in history:
                        letter = WarningLetter(
                            cutoff_date=cutoff,
                            register_no=register_no,
                            letter_type="PARENT_MEETING",
                            overall_percentage=overall_pct,
                            parent_copy=True,
                        )
                        self._letter_rows.append(letter)
                        history.append("PARENT_MEETING")
                elif "FIRST_WARNING" in history:
                    if "SECOND_WARNING" not in history:
                        letter = WarningLetter(
                            cutoff_date=cutoff,
                            register_no=register_no,
                            letter_type="SECOND_WARNING",
                            overall_percentage=overall_pct,
                            parent_copy=True,
                        )
                        self._letter_rows.append(letter)
                        history.append("SECOND_WARNING")
                else:
                    # First time below 75% — could also need FIRST_WARNING
                    # Issue SECOND_WARNING (since they're below 75)
                    # Actually: SECOND_WARNING requires prior FIRST_WARNING
                    # Issue FIRST_WARNING now if not issued
                    if "FIRST_WARNING" not in history:
                        letter = WarningLetter(
                            cutoff_date=cutoff,
                            register_no=register_no,
                            letter_type="FIRST_WARNING",
                            overall_percentage=overall_pct,
                            parent_copy=False,
                        )
                        self._letter_rows.append(letter)
                        history.append("FIRST_WARNING")
            elif overall_pct < WARN1_THRESHOLD:
                # Below 80% but >= 75%: FIRST_WARNING only
                if "FIRST_WARNING" not in history:
                    letter = WarningLetter(
                        cutoff_date=cutoff,
                        register_no=register_no,
                        letter_type="FIRST_WARNING",
                        overall_percentage=overall_pct,
                        parent_copy=False,
                    )
                    self._letter_rows.append(letter)
                    history.append("FIRST_WARNING")

    # -----------------------------------------------------------------------
    # Semester-level duty cap enforcement
    # -----------------------------------------------------------------------
    def _apply_semester_duty_cap(self, student: Student, as_of: date) -> Dict[str, int]:
        """
        Enforce the 20-day semester-wide duty cap for SPORTS/NCC_NSS.
        Returns a dict: course_code -> duty_days_allowed for this student.
        """
        # Collect all approved duty applications sorted by from_date
        duty_apps = sorted(
            [a for a in self.applications.values()
             if a.register_no == student.register_no
             and a.app_type in ("SPORTS", "NCC_NSS")
             and a.final_status == "APPROVED"],
            key=lambda x: x.from_date,
        )
        total_duty_days = 0
        for app in duty_apps:
            days = len(self._get_app_working_days(app))
            if total_duty_days + days > DUTY_SEMESTER_CAP:
                allowed = DUTY_SEMESTER_CAP - total_duty_days
                total_duty_days += allowed
                # Mark as CAPPED
                app.reason_code = f"CAPPED_AT_{DUTY_SEMESTER_CAP}"
            else:
                total_duty_days += days
        return total_duty_days

    # -----------------------------------------------------------------------
    # Report generation
    # -----------------------------------------------------------------------
    def _generate_course_attendance(self, section_id: str, as_of: date):
        """Generate course_attendance rows for a section."""
        section_students = [s for s in self.students.values() if s.section_id == section_id]
        section_courses = [c for c in self.courses.values() if c.section_id == section_id]

        for student in sorted(section_students, key=lambda s: s.register_no):
            course_results = []
            for course in sorted(section_courses, key=lambda c: c.course_code):
                result = self._calc_student_course(student, course.course_code, as_of)
                if not result:
                    continue
                pct = result["percentage"]
                status = self._determine_course_status(pct)
                fee = Decimal("0")
                if status == "CONDONABLE":
                    fee = self._course_fee(pct)
                course_results.append({
                    "as_of_date": as_of.isoformat(),
                    "register_no": student.register_no,
                    "course_code": course.course_code,
                    "conducted": result["conducted"],
                    "medical_removed": result["medical_removed"],
                    "attended": result["attended"],
                    "duty_added": result["duty_added"],
                    "percentage": str(pct),
                    "status": status,
                    "condonation_fee": str(fee),
                })
            # Apply eligibility rules to adjust statuses
            if course_results:
                _, course_results = self._determine_overall_eligibility(course_results)
                # Update fees for condoned courses
                for r in course_results:
                    if r["status"] in ("CONDONED", "CONDONABLE"):
                        pct_val = Decimal(r["percentage"])
                        r["condonation_fee"] = str(self._course_fee(pct_val))
                    elif r["status"] == "DETAINED":
                        r["condonation_fee"] = "0"
                self._course_att_rows.extend(course_results)

    def _generate_overall_attendance(self, as_of: date):
        """Generate overall_attendance rows for all students."""
        for student in sorted(self.students.values(), key=lambda s: s.register_no):
            section_courses = [
                c for c in self.courses.values()
                if c.section_id == student.section_id
            ]
            total_conducted = 0
            total_attended = 0
            course_results = []

            for course in sorted(section_courses, key=lambda c: c.course_code):
                result = self._calc_student_course(student, course.course_code, as_of)
                if not result:
                    continue
                pct = result["percentage"]
                status = self._determine_course_status(pct)
                total_conducted += result["conducted"]
                total_attended += result["attended"]
                course_results.append({
                    "status": status,
                    "percentage": pct,
                })

            overall_pct = _pct(total_attended, total_conducted)
            if course_results:
                _, updated_results = self._determine_overall_eligibility(course_results)
                detained_courses = sum(1 for r in updated_results if r["status"] == "DETAINED")
                semester_status = "DETAINED" if detained_courses > 0 else "ELIGIBLE"
            else:
                semester_status = "ELIGIBLE"

            fine = self._calc_absence_fine(student, as_of)

            # Avoid duplicate rows for same as_of_date + register_no
            existing = any(
                r["as_of_date"] == as_of.isoformat() and r["register_no"] == student.register_no
                for r in self._overall_att_rows
            )
            if not existing:
                self._overall_att_rows.append({
                    "as_of_date": as_of.isoformat(),
                    "register_no": student.register_no,
                    "conducted": total_conducted,
                    "attended": total_attended,
                    "percentage": str(overall_pct),
                    "semester_status": semester_status,
                    "fine": str(fine),
                })

    # -----------------------------------------------------------------------
    # Output writers
    # -----------------------------------------------------------------------
    def write_course_attendance(self, path: Path):
        fieldnames = [
            "as_of_date", "register_no", "course_code", "conducted",
            "medical_removed", "attended", "duty_added", "percentage",
            "status", "condonation_fee",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(self._course_att_rows)
        log.info("Written: %s (%d rows)", path, len(self._course_att_rows))

    def write_overall_attendance(self, path: Path):
        fieldnames = [
            "as_of_date", "register_no", "conducted", "attended",
            "percentage", "semester_status", "fine",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(self._overall_att_rows)
        log.info("Written: %s (%d rows)", path, len(self._overall_att_rows))

    def write_application_results(self, path: Path):
        fieldnames = ["application_id", "status", "reason_code", "days_counted"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for app in sorted(self.applications.values(), key=lambda a: a.application_id):
                w.writerow({
                    "application_id": app.application_id,
                    "status": app.final_status or "PENDING",
                    "reason_code": app.reason_code,
                    "days_counted": app.days_counted,
                })
        log.info("Written: %s (%d rows)", path, len(self.applications))

    def write_letters(self, path: Path):
        fieldnames = [
            "cutoff_date", "register_no", "letter_type",
            "overall_percentage", "parent_copy",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for ltr in self._letter_rows:
                w.writerow({
                    "cutoff_date": ltr.cutoff_date.isoformat(),
                    "register_no": ltr.register_no,
                    "letter_type": ltr.letter_type,
                    "overall_percentage": str(ltr.overall_percentage),
                    "parent_copy": str(ltr.parent_copy).lower(),
                })
        log.info("Written: %s (%d rows)", path, len(self._letter_rows))

    def write_audit_log(self, path: Path):
        fieldnames = [
            "ts", "command", "date", "section_id", "period_no",
            "register_no", "old_status", "new_status", "allowed", "reason",
        ]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for entry in self.audit_log:
                w.writerow({
                    "ts": entry.ts,
                    "command": entry.command,
                    "date": entry.date.isoformat(),
                    "section_id": entry.section_id,
                    "period_no": entry.period_no,
                    "register_no": entry.register_no,
                    "old_status": entry.old_status,
                    "new_status": entry.new_status,
                    "allowed": str(entry.allowed).lower(),
                    "reason": entry.reason,
                })
        log.info("Written audit log: %s (%d entries)", path, len(self.audit_log))

    # -----------------------------------------------------------------------
    # Command file processor
    # -----------------------------------------------------------------------
    def run_commands(self, commands_path: Path):
        with open(commands_path, encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                cmd = parts[0].strip().upper()
                args = [p.strip() for p in parts[1:]]
                ts = args[-1] if args else ""

                log.debug("Line %d: %s", line_no, cmd)

                try:
                    if cmd == "DECIDE_APPLICATION":
                        # args: app_id, decision, ts
                        self.cmd_decide_application(args, ts)
                    elif cmd == "APPROVE_OD":
                        self.cmd_approve_od(args, ts)
                    elif cmd == "PRINCIPAL_CORRECTION":
                        # args: date, section_id, period_no, register_no, new_status, ts
                        self.cmd_correction(args, is_principal=True, ts=ts)
                    elif cmd == "CORRECTION":
                        self.cmd_correction(args, is_principal=False, ts=ts)
                    elif cmd == "MONTHLY_CUTOFF":
                        self.cmd_monthly_cutoff(args)
                    elif cmd == "PRINT_LETTERS":
                        self.cmd_print_letters(args)
                    elif cmd == "PRINT_REGISTER":
                        self.cmd_print_register(args)
                    elif cmd == "PRINT_ELIGIBILITY":
                        self.cmd_print_eligibility(args)
                    elif cmd == "PRINT_SHORTAGE":
                        self.cmd_print_shortage(args)
                    else:
                        log.warning("Line %d: Unknown command '%s'", line_no, cmd)
                except Exception as e:
                    log.error("Line %d: Error processing '%s': %s", line_no, cmd, e)
                    raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="PS-068 Attendance Engine v2")
    parser.add_argument("--data-dir", default="data_v2", help="Directory containing CSV data files")
    parser.add_argument("--commands", default=None, help="Path to commands.txt")
    parser.add_argument("--output-dir", default="output", help="Output directory for CSV reports")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    commands_path = Path(args.commands) if args.commands else data_dir / "commands.txt"

    engine = AttendanceEngine()

    # Load data
    engine.load_semester(data_dir / "semester.csv")
    engine.load_calendar(data_dir / "calendar.csv")
    engine.load_sections(data_dir / "sections.csv")
    engine.load_courses(data_dir / "courses.csv")
    engine.load_timetable(data_dir / "timetable.csv")
    engine.load_students(data_dir / "students.csv")
    engine.load_placement_drives(data_dir / "placement_drives.csv")
    engine.load_attendance(data_dir / "attendance.csv")
    engine.load_applications(data_dir / "applications.csv")

    # Step 1: Process command file (sets hod_decision, OD approvals, corrections, queues prints)
    engine.run_commands(commands_path)

    # Step 2: Validate & finalize all application statuses (uses hod_decision set above)
    engine.process_all_applications()

    # Step 3: Process deferred MONTHLY_CUTOFF (warning letters need final attendance)
    for cutoff in engine._pending_cutoffs:
        log.info("Processing MONTHLY_CUTOFF for %s", cutoff)
        engine._evaluate_warnings(cutoff)
        engine._processed_cutoffs.add(cutoff)

    # Step 4: Process deferred PRINT_REGISTER requests
    seen_registers = set()
    for section_id, as_of in engine._pending_registers:
        key = (section_id, as_of)
        if key not in seen_registers:
            engine._generate_course_attendance(section_id, as_of)
            seen_registers.add(key)

    # Step 5: Process deferred PRINT_ELIGIBILITY requests
    seen_eligibility = set()
    for as_of in engine._pending_eligibility:
        if as_of not in seen_eligibility:
            for section_id in engine.sections:
                key = (section_id, as_of)
                if key not in seen_registers:
                    engine._generate_course_attendance(section_id, as_of)
                    seen_registers.add(key)
            engine._generate_overall_attendance(as_of)
            seen_eligibility.add(as_of)

    # Step 6: Process deferred PRINT_SHORTAGE requests
    seen_shortage = set()
    for as_of in engine._pending_shortage:
        if as_of not in seen_shortage:
            engine._generate_overall_attendance(as_of)
            seen_shortage.add(as_of)

    # Write outputs
    engine.write_course_attendance(output_dir / "course_attendance.csv")
    engine.write_overall_attendance(output_dir / "overall_attendance.csv")
    engine.write_application_results(output_dir / "application_results.csv")
    engine.write_letters(output_dir / "letters.csv")
    engine.write_audit_log(output_dir / "correction_audit.csv")

    log.info("Done. Outputs in: %s", output_dir)


if __name__ == "__main__":
    main()
