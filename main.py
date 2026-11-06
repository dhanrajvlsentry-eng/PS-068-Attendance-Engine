"""
PS-068 – Student Attendance and Condonation / Eligibility Engine
================================================================
Entry point: reads command.txt (or a file passed via --commands) and
executes every instruction in order.

Usage
-----
  python main.py                          # uses ./command.txt
  python main.py --commands path/cmd.txt  # custom command file
  python main.py --generate-data          # only generate sample data
  python main.py --help
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, date
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging setup (before any imports that use logger)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ps068")

# ---------------------------------------------------------------------------
# Project src imports
# ---------------------------------------------------------------------------

from src.models import (
    ApplicationStatus,
    ApplicationType,
    AttendanceCorrectionLog,
    AttendanceRecord,
    AttendanceStatus,
    CalendarEntry,
    CondonationApplication,
    Course,
    DayType,
    EligibilityStatus,
    ODApproval,
    ParentMeetingFlag,
    PlacementEvent,
    Student,
    TimetableSlot,
    WarningLevel,
    WarningLetter,
)
from src.data_loader import (
    load_all,
    load_academic_calendar,
    load_timetable,
    load_attendance,
)
from src.student_manager import StudentManager
from src.course_manager import CourseManager
from src.calendar_manager import CalendarManager
from src.attendance_manager import AttendanceManager
from src.attendance_calculator import AttendanceCalculator, ELIGIBILITY_THRESHOLD
from src.condonation_engine import CondonationEngine
from src.warning_manager import WarningManager
from src.report_generator import ReportGenerator
from src.command_parser import (
    CommandParser,
    parse_bool_arg,
    parse_date_arg,
    parse_optional_date,
    require_args,
)

# ---------------------------------------------------------------------------
# Application state (mutable, shared across all command handlers)
# ---------------------------------------------------------------------------

class AppState:
    """Central mutable state container threaded through all handlers."""

    def __init__(self):
        self.students:    Dict[str, Student]  = {}
        self.courses:     Dict[str, Course]   = {}
        self.calendar:    Dict[date, CalendarEntry] = {}
        self.timetable:   List[TimetableSlot] = []
        self.att_records: List[AttendanceRecord] = []
        self.od_approvals: List[ODApproval]   = []
        self.placement:   List[PlacementEvent] = []
        self.condonation: List[CondonationApplication] = []

        self.semester_start: Optional[date] = None
        self.semester_end:   Optional[date] = None
        self.exam_start:     Optional[date] = None

        self.audit_logs: List[AttendanceCorrectionLog] = []

        # Managers – rebuilt on demand when data changes
        self._dirty = True
        self._student_mgr:    Optional[StudentManager]    = None
        self._course_mgr:     Optional[CourseManager]     = None
        self._calendar_mgr:   Optional[CalendarManager]   = None
        self._att_mgr:        Optional[AttendanceManager] = None
        self._calculator:     Optional[AttendanceCalculator] = None
        self._cond_engine:    Optional[CondonationEngine] = None
        self._warning_mgr:    Optional[WarningManager]    = None
        self._report_gen:     Optional[ReportGenerator]   = None

    def mark_dirty(self):
        self._dirty = True

    def _rebuild(self):
        if not self._dirty:
            return
        if not self.semester_start or not self.semester_end:
            raise RuntimeError(
                "Semester dates not set. Use SET_SEMESTER, SET_CALENDAR, or LOAD_DATA first."
            )
        self._student_mgr  = StudentManager(self.students)
        self._course_mgr   = CourseManager(self.courses)
        self._calendar_mgr = CalendarManager(
            self.calendar, self.semester_start, self.semester_end, self.exam_start
        )
        self._att_mgr = AttendanceManager(
            self.att_records, self.od_approvals, self.placement
        )
        self._calculator = AttendanceCalculator(
            self._att_mgr,
            self._calendar_mgr,
            self._course_mgr,
            self._student_mgr,
        )
        self._calculator.set_timetable_index(self.timetable)
        self._cond_engine = CondonationEngine(
            calculator=self._calculator,
            att_mgr=self._att_mgr,
            calendar_mgr=self._calendar_mgr,
            course_mgr=self._course_mgr,
            student_mgr=self._student_mgr,
            applications=self.condonation,
            placement_events=self.placement,
        )
        if self._warning_mgr is None:
            self._warning_mgr = WarningManager(self._calculator, self._student_mgr)
        else:
            self._warning_mgr._calc = self._calculator
            self._warning_mgr._stu  = self._student_mgr

        self._report_gen = ReportGenerator(
            calculator=self._calculator,
            cond_engine=self._cond_engine,
            warning_mgr=self._warning_mgr,
            audit_logs=self.audit_logs,
        )
        self._dirty = False

    @property
    def student_mgr(self) -> StudentManager:
        self._rebuild()
        return self._student_mgr

    @property
    def course_mgr(self) -> CourseManager:
        self._rebuild()
        return self._course_mgr

    @property
    def calendar_mgr(self) -> CalendarManager:
        self._rebuild()
        return self._calendar_mgr

    @property
    def att_mgr(self) -> AttendanceManager:
        self._rebuild()
        return self._att_mgr

    @property
    def calculator(self) -> AttendanceCalculator:
        self._rebuild()
        return self._calculator

    @property
    def cond_engine(self) -> CondonationEngine:
        self._rebuild()
        return self._cond_engine

    @property
    def warning_mgr(self) -> WarningManager:
        self._rebuild()
        return self._warning_mgr

    @property
    def report_gen(self) -> ReportGenerator:
        self._rebuild()
        return self._report_gen


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _fmt_pct(pct: float) -> str:
    return f"{pct:.2f}%"


def _eligibility_tag(pct: float) -> str:
    if pct >= 75.0:
        return "ELIGIBLE"
    elif pct >= 65.0:
        return "CONDONABLE"
    else:
        return "INELIGIBLE"


def _print_divider(char="─", width=80):
    print(char * width)


def _print_student_report(summary, verbose: bool = True):
    """Pretty-print an OverallAttendanceSummary."""
    _print_divider()
    print(f"Student : {summary.student_id}  |  {summary.student_name}")
    print(f"Section : {summary.section}")
    print(f"Overall : {_fmt_pct(summary.overall_percentage)}  "
          f"({summary.total_present}/{summary.total_conducted} periods)  "
          f"[{_eligibility_tag(summary.overall_percentage)}]")

    if verbose and summary.course_summaries:
        print()
        # Header
        hdr = f"{'Course ID':<12} {'Course Name':<30} {'Type':<6} {'Pres':>5} {'Cond':>5} {'%':>7} {'Status':<11}"
        print(hdr)
        print("─" * len(hdr))
        for cs in sorted(summary.course_summaries, key=lambda x: x.course_id):
            ctype = "Lab" if cs.is_lab else "Theory"
            tag   = _eligibility_tag(cs.percentage)
            print(
                f"{cs.course_id:<12} {cs.course_name:<30} {ctype:<6} "
                f"{cs.present_periods:>5} {cs.conducted_periods:>5} "
                f"{_fmt_pct(cs.percentage):>8}  {tag:<11}"
            )
    _print_divider()


def _write_csv_report(summaries, output_path: Path):
    """Write a CSV attendance report."""
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "student_id", "student_name", "section",
            "total_present", "total_conducted", "overall_pct",
            "eligibility",
            "course_id", "course_name", "is_lab",
            "course_present", "course_conducted", "course_pct", "course_eligible",
        ])
        for s in summaries:
            if not s.course_summaries:
                writer.writerow([
                    s.student_id, s.student_name, s.section,
                    s.total_present, s.total_conducted,
                    f"{s.overall_percentage:.2f}",
                    _eligibility_tag(s.overall_percentage),
                    "", "", "", "", "", "", "",
                ])
            else:
                for cs in sorted(s.course_summaries, key=lambda x: x.course_id):
                    writer.writerow([
                        s.student_id, s.student_name, s.section,
                        s.total_present, s.total_conducted,
                        f"{s.overall_percentage:.2f}",
                        _eligibility_tag(s.overall_percentage),
                        cs.course_id, cs.course_name, cs.is_lab,
                        cs.present_periods, cs.conducted_periods,
                        f"{cs.percentage:.2f}",
                        "YES" if cs.eligible else "NO",
                    ])
    print(f"  → Report written: {output_path}")


def _print_condonation_report(res):
    """Pretty-print a SemesterEligibilityResult."""
    _print_divider("=")
    print(f"Student   : {res.student_id}  |  {res.student_name}  (Section {res.section})")
    print(f"Overall   : {_fmt_pct(res.overall_percentage)}  "
          f"({res.total_present}/{res.total_conducted} periods)  "
          f"Status: [{res.semester_status.value}]")
    print(f"Courses   : Eligible: {res.eligible_courses} | "
          f"Condonable: {res.condonable_courses} | "
          f"Detained: {res.detained_courses}")
    print(f"Fees/Fine : Condonation Fee: ₹{res.total_condonation_fee:,} | "
          f"Absence Fine: ₹{res.absence_fine.total_fine:,}")

    if res.absence_fine.streak_details:
        print("  Absence streaks fined:")
        for st in res.absence_fine.streak_details:
            print(f"    - {st['start']} to {st['end']} ({st['days']} working days) → ₹{st['fine']}")

    if res.course_details:
        print()
        hdr = f"{'Course ID':<10} {'Course Name':<28} {'Raw %':>7} {'PostMed%':>9} {'Final %':>8} {'Status':<11} {'Fee (₹)':>8}"
        print(hdr)
        print("─" * len(hdr))
        for cd in sorted(res.course_details, key=lambda x: x.course_id):
            fee_str = f"₹{cd.condonation_fee}" if cd.condonation_fee > 0 else "-"
            print(
                f"{cd.course_id:<10} {cd.course_name:<28} "
                f"{_fmt_pct(cd.raw_percentage):>7} "
                f"{_fmt_pct(cd.post_medical_percentage):>9} "
                f"{_fmt_pct(cd.final_percentage):>8} "
                f"{cd.course_status.value:<11} "
                f"{fee_str:>8}"
            )
    _print_divider("=")


def _write_condonation_csv(results, output_path: Path):
    """Write a detailed CSV condonation report."""
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "student_id", "student_name", "section",
            "overall_pct", "semester_status",
            "total_condonation_fee", "absence_fine",
            "eligible_courses", "condonable_courses", "detained_courses",
            "course_id", "course_name", "is_lab",
            "raw_conducted", "raw_present", "raw_pct",
            "medical_removed", "post_med_conducted", "post_med_present", "post_med_pct",
            "duty_added", "final_conducted", "final_present", "final_pct",
            "course_status", "condonation_fee", "applied_types"
        ])
        for r in results:
            if not r.course_details:
                writer.writerow([
                    r.student_id, r.student_name, r.section,
                    f"{r.overall_percentage:.2f}", r.semester_status.value,
                    r.total_condonation_fee, r.absence_fine.total_fine,
                    r.eligible_courses, r.condonable_courses, r.detained_courses,
                    "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""
                ])
            else:
                for cd in sorted(r.course_details, key=lambda x: x.course_id):
                    applied = "|".join(t.value if hasattr(t, "value") else str(t) for t in cd.applied_types)
                    writer.writerow([
                        r.student_id, r.student_name, r.section,
                        f"{r.overall_percentage:.2f}", r.semester_status.value,
                        r.total_condonation_fee, r.absence_fine.total_fine,
                        r.eligible_courses, r.condonable_courses, r.detained_courses,
                        cd.course_id, cd.course_name, cd.is_lab,
                        cd.raw_conducted, cd.raw_present, f"{cd.raw_percentage:.2f}",
                        cd.medical_periods_removed, cd.post_medical_conducted, cd.post_medical_present, f"{cd.post_medical_percentage:.2f}",
                        cd.duty_periods_added, cd.final_conducted, cd.final_present, f"{cd.final_percentage:.2f}",
                        cd.course_status.value, cd.condonation_fee, applied
                    ])
    print(f"  → Condonation report written: {output_path}")


def _parse_flags(args: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """Parse a list of command arguments into positional tokens and flag dict.
    Supports:
      --section A
      --section=A
      section=A
      --flag
    """
    pos = []
    flags = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if "=" in arg and not arg.startswith("http"):
            parts = arg.split("=", 1)
            k = parts[0].lstrip("-").lower().replace("-", "_")
            v = parts[1].strip()
            flags[k] = v
            i += 1
        elif arg.startswith("--"):
            key = arg[2:].lower().replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--") and "=" not in args[i + 1]:
                flags[key] = args[i + 1]
                i += 2
            else:
                flags[key] = "true"
                i += 1
        else:
            pos.append(arg)
            i += 1
    return pos, flags


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def make_handlers(state: AppState) -> dict:
    """Return a dict mapping command keywords → handler callables."""

    # --- LOAD_DATA --------------------------------------------------------
    def handle_load_data(args):
        require_args(args, 1, "LOAD_DATA")
        data_dir = Path(args[0])
        data = load_all(data_dir)
        state.students   = data["students"]
        state.courses    = data["courses"]
        state.calendar   = data["calendar"]
        state.timetable  = data["timetable"]
        state.att_records = data["attendance"]
        state.od_approvals = data["od_approvals"]
        state.placement  = data["placement"]
        state.condonation = data["condonation"]

        # Derive semester bounds from calendar
        working = [
            d for d, e in state.calendar.items()
            if e.day_type in (DayType.WORKING, DayType.WORKING_SATURDAY)
        ]
        if working:
            state.semester_start = min(working)
            state.semester_end   = max(working)
        exam_days = [
            d for d, e in state.calendar.items()
            if e.day_type == DayType.EXAM
        ]
        if exam_days:
            state.exam_start = min(exam_days)

        state.mark_dirty()
        print(f"Data loaded from '{data_dir}':")
        print(f"  Students  : {len(state.students)}")
        print(f"  Courses   : {len(state.courses)}")
        print(f"  Calendar  : {len(state.calendar)} entries")
        print(f"  Timetable : {len(state.timetable)} slots")
        print(f"  Attendance: {len(state.att_records)} records")
        print(f"  OD approvals: {len(state.od_approvals)}")
        print(f"  Placement events: {len(state.placement)}")
        print(f"  Condonation apps: {len(state.condonation)}")

    # --- SET_SEMESTER -----------------------------------------------------
    def handle_set_semester(args):
        require_args(args, 2, "SET_SEMESTER")
        state.semester_start = parse_date_arg(args[0])
        state.semester_end   = parse_date_arg(args[1])
        state.exam_start     = parse_date_arg(args[2]) if len(args) > 2 else None
        state.mark_dirty()
        print(f"Semester set: {state.semester_start} → {state.semester_end}"
              + (f"  (exams from {state.exam_start})" if state.exam_start else ""))

    # --- REGISTER_STUDENT -------------------------------------------------
    def handle_register_student(args):
        require_args(args, 7, "REGISTER_STUDENT")
        s = Student(
            student_id=args[0],
            name=" ".join(args[1:-5]),   # name may contain spaces
            programme=args[-5],
            semester=int(args[-4]),
            section=args[-3].upper(),
            admission_date=parse_date_arg(args[-2]),
            is_lateral=parse_bool_arg(args[-1]),
        )
        state.students[s.student_id] = s
        state.mark_dirty()
        print(f"Registered student: {s.student_id} – {s.name}")

    # --- REGISTER_COURSE --------------------------------------------------
    def handle_register_course(args):
        require_args(args, 4, "REGISTER_COURSE")
        c = Course(
            course_id=args[0],
            course_name=" ".join(args[1:-3]),
            weekly_periods=int(args[-3]),
            is_lab=parse_bool_arg(args[-2]),
            section=args[-1].upper() if len(args) > 4 else "",
        )
        state.courses[c.course_id] = c
        state.mark_dirty()
        print(f"Registered course: {c.course_id} – {c.course_name}")

    # --- MARK_HOLIDAY -----------------------------------------------------
    def handle_mark_holiday(args):
        require_args(args, 1, "MARK_HOLIDAY")
        d = parse_date_arg(args[0])
        desc = " ".join(args[1:]) if len(args) > 1 else ""
        state.calendar[d] = CalendarEntry(calendar_date=d, day_type=DayType.HOLIDAY)
        state.mark_dirty()
        print(f"Marked holiday: {d}  ({desc})")

    # --- MARK_WORKING_SAT -------------------------------------------------
    def handle_mark_working_sat(args):
        require_args(args, 2, "MARK_WORKING_SAT")
        d = parse_date_arg(args[0])
        replaces = args[1].upper()
        state.calendar[d] = CalendarEntry(
            calendar_date=d, day_type=DayType.WORKING_SATURDAY, replaces_day=replaces
        )
        state.mark_dirty()
        print(f"Marked working Saturday: {d}  (follows {replaces} timetable)")

    # --- RECORD_ATTENDANCE ------------------------------------------------
    def handle_record_attendance(args):
        require_args(args, 5, "RECORD_ATTENDANCE")
        rec = AttendanceRecord(
            record_id=f"R{len(state.att_records)+1:06d}",
            student_id=args[0],
            course_id=args[1],
            record_date=parse_date_arg(args[2]),
            period_no=int(args[3]),
            status=AttendanceStatus(args[4].upper()),
            arrival_time=None,
            faculty_id=args[6] if len(args) > 6 else None,
        )
        if len(args) > 5 and args[5]:
            from src.data_loader import _parse_time
            rec.arrival_time = _parse_time(args[5])
        state.att_records.append(rec)
        state.mark_dirty()

    # --- APPROVE_OD -------------------------------------------------------
    def handle_approve_od(args):
        require_args(args, 6, "APPROVE_OD")
        od = ODApproval(
            od_id=args[0],
            student_id=args[1],
            course_id=args[2],
            od_date=parse_date_arg(args[3]),
            reason=args[4],
            approved_by=args[5],
        )
        state.od_approvals.append(od)
        state.mark_dirty()
        print(f"OD approved: {od.od_id}  student={od.student_id}  date={od.od_date}")

    # --- APPROVE_CONDONATION ----------------------------------------------
    def handle_approve_condonation(args):
        require_args(args, 3, "APPROVE_CONDONATION")
        app_id = args[0]
        for app in state.condonation:
            if app.application_id == app_id:
                app.status      = ApplicationStatus.APPROVED
                app.approved_by = args[1]
                app.approved_on = parse_date_arg(args[2])
                state.mark_dirty()
                print(f"Condonation approved: {app_id}")
                return
        logger.warning("APPROVE_CONDONATION: application_id '%s' not found", app_id)

    # --- REJECT_CONDONATION -----------------------------------------------
    def handle_reject_condonation(args):
        require_args(args, 1, "REJECT_CONDONATION")
        app_id = args[0]
        for app in state.condonation:
            if app.application_id == app_id:
                app.status = ApplicationStatus.REJECTED
                state.mark_dirty()
                print(f"Condonation rejected: {app_id}")
                return
        logger.warning("REJECT_CONDONATION: application_id '%s' not found", app_id)

    # --- SHOW_ATTENDANCE --------------------------------------------------
    def handle_show_attendance(args):
        require_args(args, 1, "SHOW_ATTENDANCE")
        sid      = args[0]
        up_to    = parse_optional_date(args, 1)
        student  = state.students.get(sid)
        if student is None:
            print(f"ERROR: Student '{sid}' not found.")
            return
        summary = state.calculator.calculate_for_student(student, up_to)
        _print_student_report(summary)

    # --- SHOW_SECTION_ATTENDANCE ------------------------------------------
    def handle_show_section_attendance(args):
        require_args(args, 1, "SHOW_SECTION_ATTENDANCE")
        section = args[0].upper()
        up_to   = parse_optional_date(args, 1)
        summaries = state.calculator.calculate_for_section(section, up_to)
        if not summaries:
            print(f"No students found in section {section}.")
            return
        print(f"\n{'='*80}")
        print(f"SECTION {section} ATTENDANCE REPORT"
              + (f"  (up to {up_to})" if up_to else ""))
        print(f"{'='*80}")
        for s in sorted(summaries, key=lambda x: x.student_id):
            print(
                f"  {s.student_id:<12} {s.student_name:<30} "
                f"{_fmt_pct(s.overall_percentage):>8}  "
                f"[{_eligibility_tag(s.overall_percentage)}]"
            )
        eligible = sum(1 for s in summaries if s.is_eligible)
        print(f"\n  Total: {len(summaries)} students | "
              f"Eligible: {eligible} | "
              f"Not eligible: {len(summaries)-eligible}")
        print(f"{'='*80}\n")

    # --- SHOW_INELIGIBLE --------------------------------------------------
    def handle_show_ineligible(args):
        up_to = parse_optional_date(args, 0)
        ineligible = state.calculator.ineligible_students(up_to)
        label = f"up to {up_to}" if up_to else "full semester"
        print(f"\nINELIGIBLE STUDENTS ({label}) — below {ELIGIBILITY_THRESHOLD}%")
        _print_divider("=")
        if not ineligible:
            print("  None. All students meet the attendance threshold.")
        else:
            for s in sorted(ineligible, key=lambda x: x.overall_percentage):
                print(f"  {s.student_id:<12} {s.student_name:<30} "
                      f"{_fmt_pct(s.overall_percentage):>8}  "
                      f"[{_eligibility_tag(s.overall_percentage)}]")
        _print_divider("=")
        print()

    # --- GENERATE_REPORT --------------------------------------------------
    def handle_generate_report(args):
        require_args(args, 1, "GENERATE_REPORT")
        report_type = args[0].upper()
        section  = args[1].upper() if len(args) > 1 and args[1].upper() not in ("NONE", "") else None
        up_to    = parse_optional_date(args, 2)
        out_file = Path(args[3]) if len(args) > 3 else None

        if report_type == "SECTION" and section:
            summaries = state.calculator.calculate_for_section(section, up_to)
        elif report_type == "ALL":
            summaries = state.calculator.calculate_all(up_to)
        elif report_type == "INELIGIBLE":
            summaries = state.calculator.ineligible_students(up_to)
        elif report_type == "CONDONABLE":
            summaries = [
                s for s in state.calculator.calculate_all(up_to)
                if 65.0 <= s.overall_percentage < 75.0
            ]
        else:
            print(f"Unknown report type: {report_type}. "
                  "Use ALL | SECTION | INELIGIBLE | CONDONABLE")
            return

        if out_file:
            out_file.parent.mkdir(parents=True, exist_ok=True)
            _write_csv_report(summaries, out_file)
        else:
            # Print summary table to console
            print(f"\nREPORT: {report_type}"
                  + (f" / Section {section}" if section else "")
                  + (f"  up to {up_to}" if up_to else ""))
            _print_divider("=")
            for s in sorted(summaries, key=lambda x: x.student_id):
                print(f"  {s.student_id:<12} {s.student_name:<30} "
                      f"{_fmt_pct(s.overall_percentage):>8}  "
                      f"[{_eligibility_tag(s.overall_percentage)}]")
            _print_divider("=")
            print()

    # --- CALENDAR_SUMMARY -------------------------------------------------
    def handle_calendar_summary(args):
        summary = state.calendar_mgr.summary()
        print("\nCALENDAR SUMMARY")
        for k, v in summary.items():
            print(f"  {k:<25}: {v}")
        print()

    # --- SHOW_CONDONATION -------------------------------------------------
    def handle_show_condonation(args):
        require_args(args, 1, "SHOW_CONDONATION")
        sid = args[0]
        up_to = parse_optional_date(args, 1)
        student = state.students.get(sid)
        if student is None:
            print(f"ERROR: Student '{sid}' not found.")
            return
        result = state.cond_engine.process_student(student, up_to)
        _print_condonation_report(result)

    # --- SHOW_CONDONATION_SECTION -----------------------------------------
    def handle_show_condonation_section(args):
        require_args(args, 1, "SHOW_CONDONATION_SECTION")
        section = args[0].upper()
        up_to = parse_optional_date(args, 1)
        results = state.cond_engine.process_section(section, up_to)
        if not results:
            print(f"No students found in section {section}.")
            return
        print(f"\n{'='*80}")
        print(f"SECTION {section} CONDONATION & ELIGIBILITY REPORT"
              + (f"  (up to {up_to})" if up_to else ""))
        print(f"{'='*80}")
        for r in sorted(results, key=lambda x: x.student_id):
            print(
                f"  {r.student_id:<12} {r.student_name:<28} "
                f"{_fmt_pct(r.overall_percentage):>8}  "
                f"[{r.semester_status.value:<10}]  "
                f"Fee: ₹{r.total_condonation_fee:>4}  Fine: ₹{r.absence_fine.total_fine:>4}"
            )
        el = sum(1 for r in results if r.semester_status == EligibilityStatus.ELIGIBLE)
        co = sum(1 for r in results if r.semester_status == EligibilityStatus.CONDONABLE)
        de = sum(1 for r in results if r.semester_status == EligibilityStatus.DETAINED)
        print(f"\n  Total: {len(results)} | Eligible: {el} | Condonable: {co} | Detained: {de}")
        print(f"{'='*80}\n")

    # --- GENERATE_CONDONATION_REPORT --------------------------------------
    def handle_generate_condonation_report(args):
        require_args(args, 1, "GENERATE_CONDONATION_REPORT")
        target = args[0].upper()
        up_to = None
        out_file = None
        if len(args) > 1:
            if args[1].endswith(".csv") or "/" in args[1] or "\\" in args[1]:
                out_file = Path(args[1])
            else:
                up_to = parse_optional_date(args, 1)
                if len(args) > 2:
                    out_file = Path(args[2])

        if target == "ALL":
            results = state.cond_engine.process_all(up_to)
        else:
            results = state.cond_engine.process_section(target, up_to)

        if out_file:
            out_file.parent.mkdir(parents=True, exist_ok=True)
            _write_condonation_csv(results, out_file)
        else:
            print(f"\nCONDONATION REPORT: {target}" + (f"  up to {up_to}" if up_to else ""))
            _print_divider("=")
            for r in sorted(results, key=lambda x: x.student_id):
                print(f"  {r.student_id:<12} {r.student_name:<28} "
                      f"{_fmt_pct(r.overall_percentage):>8}  "
                      f"[{r.semester_status.value:<10}]  "
                      f"Fee: ₹{r.total_condonation_fee}  Fine: ₹{r.absence_fine.total_fine}")
            _print_divider("=")

    # --- SET_CALENDAR -----------------------------------------------------
    def handle_set_calendar(args):
        require_args(args, 1, "SET_CALENDAR")
        first = args[0]
        # Check if first argument is a file path
        if first.endswith(".csv") or Path(first).is_file():
            cal_path = Path(first)
            data_dir = cal_path.parent
            state.calendar = load_academic_calendar(data_dir)
            working = [
                d for d, e in state.calendar.items()
                if e.day_type in (DayType.WORKING, DayType.WORKING_SATURDAY)
            ]
            if working:
                state.semester_start = min(working)
                state.semester_end   = max(working)
            exam_days = [
                d for d, e in state.calendar.items()
                if e.day_type == DayType.EXAM
            ]
            if exam_days:
                state.exam_start = min(exam_days)
            state.mark_dirty()
            print(f"Calendar loaded from {cal_path}: {len(state.calendar)} entries (Semester: {state.semester_start} -> {state.semester_end})")
        else:
            require_args(args, 2, "SET_CALENDAR")
            state.semester_start = parse_date_arg(args[0])
            state.semester_end   = parse_date_arg(args[1])
            state.exam_start     = parse_date_arg(args[2]) if len(args) > 2 else None
            state.mark_dirty()
            print(f"Semester calendar configured: {state.semester_start} -> {state.semester_end}"
                  + (f" (exams: {state.exam_start})" if state.exam_start else ""))

    # --- LOAD_TIMETABLE ---------------------------------------------------
    def handle_load_timetable(args):
        require_args(args, 1, "LOAD_TIMETABLE")
        tt_path = Path(args[0])
        state.timetable = load_timetable(tt_path.parent)
        state.mark_dirty()
        print(f"Loaded {len(state.timetable)} timetable slots from {tt_path}")

    # --- LOAD_ATTENDANCE --------------------------------------------------
    def handle_load_attendance(args):
        require_args(args, 1, "LOAD_ATTENDANCE")
        att_path = Path(args[0])
        records = load_attendance(att_path.parent)
        state.att_records = records
        state.mark_dirty()
        print(f"Loaded {len(state.att_records)} attendance records from {att_path}")

    # --- APPLY_CONDONATION ------------------------------------------------
    def handle_apply_condonation(args):
        require_args(args, 7, "APPLY_CONDONATION")
        app = CondonationApplication(
            application_id=args[0],
            student_id=args[1],
            course_id=args[2],
            reason=ApplicationType(args[3].upper()),
            start_date=parse_date_arg(args[4]),
            end_date=parse_date_arg(args[5]),
            description=args[6],
            status=ApplicationStatus.PENDING,
            approved_by=None,
            approved_on=None,
            medical_cert_ref=args[7] if len(args) > 7 and args[7] not in ("", "NONE", "-") else None,
            submitted_on=parse_date_arg(args[8]) if len(args) > 8 and args[8] not in ("", "NONE", "-") else None,
        )
        state.condonation.append(app)
        state.mark_dirty()
        print(f"Applied condonation: {app.application_id} for student {app.student_id} ({app.reason.value})")

    # --- PRINCIPAL_CORRECTION ---------------------------------------------
    def handle_principal_correction(args):
        require_args(args, 7, "PRINCIPAL_CORRECTION")
        sid = args[0]
        cid = args[1]
        rec_d = parse_date_arg(args[2])
        pno = int(args[3])
        new_status = args[4].upper()
        auth = args[5]
        reason = " ".join(args[6:])

        # Check freeze status
        is_post_freeze = state.calendar_mgr.is_frozen(rec_d)
        if is_post_freeze and "PRINCIPAL" not in auth.upper():
            err_msg = f"Post-freeze attendance correction requires Principal authorization; rejected for '{auth}'"
            print(f"ERROR: {err_msg}")
            raise PermissionError(err_msg)

        # Locate existing record or create new one
        matched = False
        old_status = None
        for r in state.att_records:
            if r.student_id == sid and r.course_id == cid and r.record_date == rec_d and r.period_no == pno:
                old_status = r.status.value
                r.status = AttendanceStatus(new_status)
                matched = True
                break

        if not matched:
            rid = f"CORR_REC_{len(state.att_records)+1:06d}"
            new_rec = AttendanceRecord(
                record_id=rid,
                student_id=sid,
                course_id=cid,
                record_date=rec_d,
                period_no=pno,
                status=AttendanceStatus(new_status),
                faculty_id="CORRECTED_FAC",
            )
            state.att_records.append(new_rec)

        # Log to audit trail
        log_entry = AttendanceCorrectionLog(
            log_id=f"LOG-{len(state.audit_logs)+1:04d}",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            student_id=sid,
            course_id=cid,
            record_date=rec_d,
            period_no=pno,
            old_status=old_status,
            new_status=new_status,
            authorized_by=auth,
            reason=reason,
            is_post_freeze=is_post_freeze,
        )
        state.audit_logs.append(log_entry)
        state.mark_dirty()
        print(f"Principal Correction Applied: {sid} {cid} on {rec_d} (P{pno}) -> {new_status} [Auth: {auth}]")

    # --- GENERATE_WARNING_LETTERS -----------------------------------------
    def handle_generate_warning_letters(args):
        require_args(args, 1, "GENERATE_WARNING_LETTERS")
        pos, flags = _parse_flags(args)
        run_date = parse_date_arg(pos[0])
        month_no = int(pos[1]) if len(pos) > 1 else 1
        out_dir = Path(pos[2]) if len(pos) > 2 else Path(flags.get("out", f"reports/warning_run_{month_no}"))
        sec = flags.get("section")

        letters, flags_raised = state.warning_mgr.evaluate_run(run_date, month_no, out_dir, section=sec)
        print(f"\n{'='*76}")
        print(f"WARNING LETTER RUN {month_no} (As of {run_date})")
        print(f"{'='*76}")
        print(f"  Letters issued in this run: {len(letters)}")
        first_w = sum(1 for l in letters if l.warning_level == WarningLevel.FIRST_WARNING)
        sec_w = sum(1 for l in letters if l.warning_level == WarningLevel.SECOND_WARNING)
        print(f"    - First Warnings (<80%): {first_w}")
        print(f"    - Second Warnings (<75% with parent copy): {sec_w}")
        print(f"  Parent Meeting Flags: {len(state.warning_mgr.parent_meeting_flags)} total")
        print(f"  Output directory: {out_dir}")
        print(f"{'='*76}\n")

    # --- GENERATE_ATTENDANCE_REPORT ---------------------------------------
    def handle_generate_attendance_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/attendance_register.csv"))
        sec = flags.get("section", pos[1] if len(pos) > 1 else None)
        sid = flags.get("student")
        cid = flags.get("course")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        sem = int(flags["semester"]) if "semester" in flags else None

        count = state.report_gen.generate_attendance_register(
            output_path=out_path,
            student_id=sid,
            section=sec,
            course_id=cid,
            up_to_date=date_cutoff,
            semester=sem,
        )
        print(f"Generated Attendance Register: {out_path} ({count} records)")

    # --- GENERATE_ELIGIBILITY_REPORT --------------------------------------
    def handle_generate_eligibility_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/eligibility_report.csv"))
        sec = flags.get("section", pos[1] if len(pos) > 1 else None)
        sid = flags.get("student")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        sem = int(flags["semester"]) if "semester" in flags else None

        count = state.report_gen.generate_eligibility_list(
            output_path=out_path,
            student_id=sid,
            section=sec,
            up_to_date=date_cutoff,
            semester=sem,
        )
        print(f"Generated Eligibility Report: {out_path} ({count} students)")

    # --- GENERATE_SHORTAGE_REPORT -----------------------------------------
    def handle_generate_shortage_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/shortage_report.csv"))
        sec = flags.get("section", pos[1] if len(pos) > 1 else None)
        sid = flags.get("student")
        cid = flags.get("course")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        sem = int(flags["semester"]) if "semester" in flags else None

        count = state.report_gen.generate_shortage_list(
            output_path=out_path,
            student_id=sid,
            section=sec,
            course_id=cid,
            up_to_date=date_cutoff,
            semester=sem,
        )
        print(f"Generated Shortage List: {out_path} ({count} course shortages)")

    # --- GENERATE_AUDIT_LOG -----------------------------------------------
    def handle_generate_audit_log(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/audit_log.csv"))
        count = state.report_gen.generate_audit_log_report(out_path)
        print(f"Generated Audit Log: {out_path} ({count} correction logs)")

    # --- GENERATE_FEE_REPORT ----------------------------------------------
    def handle_generate_fee_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/condonation_fee_report.csv"))
        sec = flags.get("section")
        sid = flags.get("student")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        count = state.report_gen.generate_fee_report(out_path, student_id=sid, section=sec, up_to_date=date_cutoff)
        print(f"Generated Condonation Fee Report: {out_path} ({count} fee entries)")

    # --- GENERATE_FINE_REPORT ---------------------------------------------
    def handle_generate_fine_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/absence_fine_report.csv"))
        sec = flags.get("section")
        sid = flags.get("student")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        count = state.report_gen.generate_fine_report(out_path, student_id=sid, section=sec, up_to_date=date_cutoff)
        print(f"Generated Absence Fine Report: {out_path} ({count} fine entries)")

    # --- GENERATE_PARENT_MEETING_REPORT -----------------------------------
    def handle_generate_parent_meeting_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/parent_meeting_flags.csv"))
        sec = flags.get("section")
        count = state.report_gen.generate_parent_meeting_report(out_path, section=sec)
        print(f"Generated Parent Meeting Report: {out_path} ({count} flagged students)")

    # --- GENERATE_COURSE_WISE_REPORT --------------------------------------
    def handle_generate_course_wise_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/course_wise_attendance.csv"))
        sec = flags.get("section", pos[1] if len(pos) > 1 else None)
        sid = flags.get("student")
        cid = flags.get("course")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        sem = int(flags["semester"]) if "semester" in flags else None

        count = state.report_gen.generate_course_wise_report(
            output_path=out_path,
            student_id=sid,
            section=sec,
            course_id=cid,
            up_to_date=date_cutoff,
            semester=sem,
        )
        print(f"Generated Course-Wise Report: {out_path} ({count} entries)")

    # --- GENERATE_OVERALL_REPORT ------------------------------------------
    def handle_generate_overall_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/overall_attendance.csv"))
        sec = flags.get("section", pos[1] if len(pos) > 1 else None)
        sid = flags.get("student")
        date_cutoff = parse_date_arg(flags["date"]) if "date" in flags else None
        sem = int(flags["semester"]) if "semester" in flags else None

        count = state.report_gen.generate_overall_attendance_report(
            output_path=out_path,
            student_id=sid,
            section=sec,
            up_to_date=date_cutoff,
            semester=sem,
        )
        print(f"Generated Overall Attendance Report: {out_path} ({count} entries)")

    # --- GENERATE_CONDONATION_STATUS_REPORT --------------------------------
    def handle_generate_condonation_status_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/condonation_status_report.csv"))
        sec = flags.get("section")
        sid = flags.get("student")
        status_filter = flags.get("status")

        count = state.report_gen.generate_condonation_status_report(
            output_path=out_path,
            student_id=sid,
            section=sec,
            status_filter=status_filter,
        )
        print(f"Generated Condonation Status Report: {out_path} ({count} applications)")

    # --- GENERATE_WARNING_LETTERS_REPORT ----------------------------------
    def handle_generate_warning_letters_report(args):
        pos, flags = _parse_flags(args)
        out_path = Path(flags.get("out", pos[0] if pos else "reports/warning_letters_summary.csv"))
        sec = flags.get("section")
        sid = flags.get("student")

        count = state.report_gen.generate_warning_letters_report(
            output_path=out_path,
            student_id=sid,
            section=sec,
        )
        print(f"Generated Warning Letters Report: {out_path} ({count} letters)")

    # --- Return handler map -----------------------------------------------
    return {
        "LOAD_DATA":                      handle_load_data,
        "SET_SEMESTER":                   handle_set_semester,
        "SET_CALENDAR":                   handle_set_calendar,
        "LOAD_TIMETABLE":                 handle_load_timetable,
        "LOAD_ATTENDANCE":                handle_load_attendance,
        "REGISTER_STUDENT":               handle_register_student,
        "REGISTER_COURSE":                handle_register_course,
        "MARK_HOLIDAY":                   handle_mark_holiday,
        "MARK_WORKING_SAT":               handle_mark_working_sat,
        "RECORD_ATTENDANCE":              handle_record_attendance,
        "APPLY_CONDONATION":              handle_apply_condonation,
        "APPROVE_OD":                     handle_approve_od,
        "APPROVE_CONDONATION":            handle_approve_condonation,
        "REJECT_CONDONATION":             handle_reject_condonation,
        "PRINCIPAL_CORRECTION":           handle_principal_correction,
        "SHOW_ATTENDANCE":                handle_show_attendance,
        "SHOW_SECTION_ATTENDANCE":        handle_show_section_attendance,
        "SHOW_INELIGIBLE":                handle_show_ineligible,
        "GENERATE_REPORT":                handle_generate_report,
        "CALENDAR_SUMMARY":               handle_calendar_summary,
        "SHOW_CONDONATION":               handle_show_condonation,
        "SHOW_CONDONATION_SECTION":       handle_show_condonation_section,
        "GENERATE_CONDONATION_REPORT":    handle_generate_condonation_report,
        "GENERATE_WARNING_LETTERS":       handle_generate_warning_letters,
        "GENERATE_ATTENDANCE_REPORT":     handle_generate_attendance_report,
        "GENERATE_COURSE_WISE_REPORT":    handle_generate_course_wise_report,
        "GENERATE_OVERALL_REPORT":        handle_generate_overall_report,
        "GENERATE_ELIGIBILITY_REPORT":    handle_generate_eligibility_report,
        "GENERATE_SHORTAGE_REPORT":       handle_generate_shortage_report,
        "GENERATE_CONDONATION_STATUS_REPORT": handle_generate_condonation_status_report,
        "GENERATE_WARNING_LETTERS_REPORT":handle_generate_warning_letters_report,
        "GENERATE_AUDIT_LOG":             handle_generate_audit_log,
        "GENERATE_FEE_REPORT":            handle_generate_fee_report,
        "GENERATE_FINE_REPORT":           handle_generate_fine_report,
        "GENERATE_PARENT_MEETING_REPORT": handle_generate_parent_meeting_report,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="PS-068 Student Attendance and Condonation Engine"
    )
    ap.add_argument(
        "--commands", default="command.txt",
        help="Path to the command file (default: command.txt)"
    )
    ap.add_argument(
        "--generate-data", action="store_true",
        help="Run the sample-data generator and exit"
    )
    ap.add_argument(
        "--data-dir", default="data",
        help="Data directory (used with --generate-data, default: data)"
    )
    ap.add_argument(
        "--verbose", action="store_true",
        help="Enable DEBUG-level logging"
    )
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.generate_data:
        from generate_data import generate_all
        generate_all(Path(args.data_dir))
        return

    # Normal command-file execution
    state   = AppState()
    handlers = make_handlers(state)

    parser = CommandParser()
    for cmd, handler in handlers.items():
        parser.register(cmd, handler)

    print("PS-068 Attendance Engine  –  starting")
    print(f"Command file: {args.commands}\n")

    try:
        parser.run_file(args.commands)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.exception("Fatal error during command execution")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()

