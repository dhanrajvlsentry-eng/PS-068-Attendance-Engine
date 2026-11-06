"""
Unit tests for the PS-068 Attendance Engine – Part 1
======================================================
Tests cover:
  - Model construction and helpers
  - CalendarManager date classification
  - AttendanceManager indexing and OD lookup
  - AttendanceCalculator business rules (R1–R9)
  - StudentManager and CourseManager
  - CommandParser dispatch

Run with:
  pytest tests/ -v
  pytest tests/ -v --cov=src --cov-report=term-missing
"""
from __future__ import annotations

import pytest
from datetime import date, time, timedelta
from typing import List

# ---------------------------------------------------------------------------
# Imports from src
# ---------------------------------------------------------------------------
from src.models import (
    AttendanceRecord,
    AttendanceStatus,
    CalendarEntry,
    CondonationApplication,
    Course,
    CourseAttendanceSummary,
    DayType,
    ODApproval,
    PlacementEvent,
    Student,
    TimetableSlot,
)
from src.student_manager import StudentManager
from src.course_manager import CourseManager
from src.calendar_manager import CalendarManager
from src.attendance_manager import AttendanceManager
from src.attendance_calculator import AttendanceCalculator, ELIGIBILITY_THRESHOLD
from src.command_parser import CommandParser, parse_bool_arg, parse_date_arg


# ===========================================================================
# Fixtures
# ===========================================================================

SEMESTER_START = date(2024, 7, 29)
SEMESTER_END   = date(2024, 11, 16)


def _make_student(sid="S001", section="A", is_lateral=False,
                  adm_date=None) -> Student:
    return Student(
        student_id=sid,
        name="Test Student",
        programme="B.Tech CSE",
        semester=3,
        section=section,
        admission_date=adm_date or SEMESTER_START,
        is_lateral=is_lateral,
    )


def _make_course(cid="CS101", is_lab=False, weekly=4) -> Course:
    return Course(
        course_id=cid,
        course_name="Test Course",
        weekly_periods=weekly,
        is_lab=is_lab,
    )


def _make_calendar(holidays=None, working_sats=None) -> dict:
    """Build a minimal calendar covering SEMESTER_START .. SEMESTER_END."""
    cal = {}
    hols = set(holidays or [])
    wsats = dict(working_sats or {})
    cur = SEMESTER_START
    while cur <= SEMESTER_END:
        wd = cur.weekday()
        if cur in hols:
            day_type, rep = DayType.HOLIDAY, None
        elif cur in wsats:
            day_type, rep = DayType.WORKING_SATURDAY, wsats[cur]
        elif wd == 6:
            day_type, rep = DayType.SUNDAY, None
        elif wd == 5:
            day_type, rep = DayType.SATURDAY, None
        else:
            day_type, rep = DayType.WORKING, None
        cal[cur] = CalendarEntry(calendar_date=cur, day_type=day_type,
                                  replaces_day=rep)
        cur += timedelta(days=1)
    return cal


def _make_record(rid, sid, cid, d, period, status, arrival=None,
                 faculty="FAC001") -> AttendanceRecord:
    return AttendanceRecord(
        record_id=rid,
        student_id=sid,
        course_id=cid,
        record_date=d,
        period_no=period,
        status=status,
        arrival_time=arrival,
        faculty_id=faculty,
    )


def _make_timetable_slot(section="A", day="MON", period=1, cid="CS101",
                          start=None, end=None) -> TimetableSlot:
    return TimetableSlot(
        section=section, day=day, period_no=period, course_id=cid,
        start_time=start or time(8, 0),
        end_time=end or time(8, 55),
    )


# ===========================================================================
# Model tests
# ===========================================================================

class TestModels:
    def test_student_construction(self):
        s = _make_student()
        assert s.student_id == "S001"
        assert s.section == "A"
        assert not s.is_lateral

    def test_student_hash_equality(self):
        s1 = _make_student("S001")
        s2 = _make_student("S001")
        s3 = _make_student("S002")
        assert s1 == s2
        assert s1 != s3
        assert hash(s1) == hash(s2)

    def test_course_period_weight_theory(self):
        c = _make_course(is_lab=False)
        assert c.period_weight() == 1

    def test_course_period_weight_lab(self):
        c = _make_course(is_lab=True)
        assert c.period_weight() == 2

    def test_calendar_entry_is_working(self):
        e = CalendarEntry(date(2024, 7, 29), DayType.WORKING)
        assert e.is_working()

    def test_calendar_entry_holiday_not_working(self):
        e = CalendarEntry(date(2024, 8, 15), DayType.HOLIDAY)
        assert not e.is_working()

    def test_calendar_entry_working_saturday(self):
        e = CalendarEntry(date(2024, 8, 10), DayType.WORKING_SATURDAY,
                          replaces_day="MON")
        assert e.is_working()
        assert e.timetable_day() == "MON"

    def test_attendance_record_string_status(self):
        r = AttendanceRecord(
            record_id="R001", student_id="S1", course_id="C1",
            record_date=date(2024, 8, 1), period_no=1,
            status="PRESENT",   # string input
        )
        assert r.status == AttendanceStatus.PRESENT

    def test_attendance_record_string_date(self):
        r = AttendanceRecord(
            record_id="R001", student_id="S1", course_id="C1",
            record_date="2024-08-01",   # string input
            period_no=1,
            status=AttendanceStatus.ABSENT,
        )
        assert r.record_date == date(2024, 8, 1)

    def test_od_approval_construction(self):
        od = ODApproval(
            od_id="OD001", student_id="S1", course_id="*",
            od_date="2024-09-01", reason="Placement", approved_by="HOD"
        )
        assert od.od_date == date(2024, 9, 1)
        assert od.course_id == "*"


# ===========================================================================
# StudentManager tests
# ===========================================================================

class TestStudentManager:
    def setup_method(self):
        students = {
            "S001": _make_student("S001", "A"),
            "S002": _make_student("S002", "A"),
            "S003": _make_student("S003", "B"),
        }
        self.mgr = StudentManager(students)

    def test_get_existing(self):
        assert self.mgr.get("S001") is not None

    def test_get_missing(self):
        assert self.mgr.get("XXXX") is None

    def test_by_section(self):
        assert len(self.mgr.by_section("A")) == 2
        assert len(self.mgr.by_section("B")) == 1
        assert len(self.mgr.by_section("C")) == 0

    def test_count(self):
        assert self.mgr.count() == 3

    def test_sections(self):
        sections = self.mgr.sections()
        assert "A" in sections
        assert "B" in sections

    def test_register_new(self):
        new_student = _make_student("S999", "C")
        self.mgr.register(new_student)
        assert self.mgr.get("S999") is not None
        assert len(self.mgr.by_section("C")) == 1


# ===========================================================================
# CourseManager tests
# ===========================================================================

class TestCourseManager:
    def setup_method(self):
        courses = {
            "CS101": Course("CS101", "Math", 4, False, ""),        # all sections
            "CS102": Course("CS102", "Physics", 3, False, "A"),    # section A only
            "CS191": Course("CS191", "Physics Lab", 2, True, "A"), # section A only
        }
        self.mgr = CourseManager(courses)

    def test_get_existing(self):
        assert self.mgr.get("CS101") is not None

    def test_for_section_includes_global(self):
        courses = self.mgr.for_section("A")
        ids = {c.course_id for c in courses}
        assert "CS101" in ids   # global
        assert "CS102" in ids   # section A
        assert "CS191" in ids   # section A lab

    def test_for_section_excludes_other_section(self):
        courses = self.mgr.for_section("B")
        ids = {c.course_id for c in courses}
        assert "CS101" in ids   # global
        assert "CS102" not in ids  # A only

    def test_period_weight_lab(self):
        assert self.mgr.period_weight("CS191") == 2

    def test_period_weight_theory(self):
        assert self.mgr.period_weight("CS101") == 1

    def test_is_lab(self):
        assert self.mgr.is_lab("CS191")
        assert not self.mgr.is_lab("CS101")


# ===========================================================================
# CalendarManager tests
# ===========================================================================

class TestCalendarManager:
    def setup_method(self):
        hol = date(2024, 8, 15)
        wsat = {date(2024, 8, 10): "MON"}
        cal = _make_calendar(holidays=[hol], working_sats=wsat)
        self.mgr = CalendarManager(cal, SEMESTER_START, SEMESTER_END)
        self.hol  = hol
        self.wsat = date(2024, 8, 10)

    def test_working_day(self):
        # First Monday of semester
        assert self.mgr.is_working_day(SEMESTER_START)

    def test_holiday_not_working(self):
        assert not self.mgr.is_working_day(self.hol)
        assert self.mgr.is_holiday(self.hol)

    def test_sunday_not_working(self):
        # Find first Sunday
        d = SEMESTER_START
        while d.weekday() != 6:
            d += timedelta(days=1)
        assert not self.mgr.is_working_day(d)

    def test_working_saturday_is_working(self):
        assert self.mgr.is_working_day(self.wsat)

    def test_working_saturday_timetable_day(self):
        assert self.mgr.timetable_day(self.wsat) == "MON"

    def test_holiday_timetable_day_none(self):
        assert self.mgr.timetable_day(self.hol) is None

    def test_working_days_count(self):
        wdays = self.mgr.working_days()
        assert len(wdays) > 60   # 16 weeks - holidays ≈ 75+

    def test_semester_weeks(self):
        assert self.mgr.semester_weeks() == 16


# ===========================================================================
# AttendanceManager tests
# ===========================================================================

class TestAttendanceManager:
    def setup_method(self):
        self.d1 = date(2024, 7, 29)   # Monday
        self.d2 = date(2024, 7, 30)

        records = [
            _make_record("R001", "S001", "CS101", self.d1, 1, AttendanceStatus.PRESENT),
            _make_record("R002", "S001", "CS101", self.d1, 2, AttendanceStatus.ABSENT),
            _make_record("R003", "S001", "CS101", self.d2, 1, AttendanceStatus.OD),
            _make_record("R004", "S002", "CS101", self.d1, 1, AttendanceStatus.PRESENT),
            # No faculty → not conducted
            _make_record("R005", "S001", "CS102", self.d1, 1, AttendanceStatus.PRESENT,
                         faculty=None),
        ]
        od = [
            ODApproval("OD01", "S001", "CS101", self.d2, "Placement", "HOD"),
        ]
        placement = [
            PlacementEvent("PE01", self.d1 + timedelta(weeks=5), "TCS", ["S002"]),
        ]
        self.mgr = AttendanceManager(records, od, placement)

    def test_records_for_student_course(self):
        recs = self.mgr.records_for_student_course("S001", "CS101")
        assert len(recs) == 3

    def test_records_for_date_course(self):
        recs = self.mgr.records_for_date_course(self.d1, "CS101")
        # S001 has 2 records on d1 (period 1 + period 2), S002 has 1 record → total 3
        assert len(recs) == 3

    def test_has_od_explicit(self):
        assert self.mgr.has_od("S001", self.d2, "CS101")

    def test_has_od_specific_course(self):
        # OD for CS101 should not apply to CS102
        assert not self.mgr.has_od("S001", self.d2, "CS102")

    def test_has_od_placement_wildcard(self):
        # Placement event generates * OD
        pe_date = self.d1 + timedelta(weeks=5)
        assert self.mgr.has_od("S002", pe_date, "CS101")
        assert self.mgr.has_od("S002", pe_date, "CS999")  # any course

    def test_no_od_when_not_approved(self):
        assert not self.mgr.has_od("S001", self.d1, "CS101")

    def test_periods_conducted_with_faculty(self):
        # R001 and R002 for d1,CS101 have faculty → 2 conducted periods
        assert self.mgr.periods_conducted("CS101", self.d1) == 2

    def test_periods_conducted_no_faculty(self):
        # R005 for CS102 has no faculty → 0 conducted
        assert self.mgr.periods_conducted("CS102", self.d1) == 0

    def test_all_course_ids_for_student(self):
        ids = self.mgr.all_course_ids_for_student("S001")
        assert "CS101" in ids
        assert "CS102" in ids

    def test_total_records(self):
        assert self.mgr.total_records() == 5


# ===========================================================================
# AttendanceCalculator – Business Rule Tests
# ===========================================================================

class TestAttendanceCalculator:
    """
    Uses a minimal synthetic dataset to verify each business rule in isolation.
    """

    def _build_calculator(self, records, od_approvals=None, placement=None,
                           holidays=None, working_sats=None,
                           student=None, course=None, slots=None):
        od_approvals = od_approvals or []
        placement    = placement    or []
        holidays     = holidays     or []
        working_sats = working_sats or {}
        student      = student or _make_student()
        course       = course  or _make_course()

        cal = _make_calendar(holidays, working_sats)
        cal_mgr  = CalendarManager(cal, SEMESTER_START, SEMESTER_END)
        stu_mgr  = StudentManager({student.student_id: student})
        crs_mgr  = CourseManager({course.course_id: course})
        att_mgr  = AttendanceManager(records, od_approvals, placement)
        calc     = AttendanceCalculator(att_mgr, cal_mgr, crs_mgr, stu_mgr)

        if slots:
            calc.set_timetable_index(slots)

        return calc, student, course

    # R1: Periods not conducted (no faculty_id) must not be counted

    def test_R1_unconducted_period_excluded(self):
        d = date(2024, 7, 29)   # Monday, working day
        records = [
            _make_record("R1", "S001", "CS101", d, 1,
                         AttendanceStatus.PRESENT, faculty=None),  # not conducted
        ]
        calc, student, _ = self._build_calculator(records)
        summary = calc.calculate_for_student(student)
        assert summary.total_conducted == 0
        assert summary.total_present == 0

    # R2: Records on holidays must be ignored

    def test_R2_holiday_record_ignored(self):
        hol = date(2024, 8, 15)
        d   = date(2024, 7, 29)
        records = [
            _make_record("R1", "S001", "CS101", hol, 1, AttendanceStatus.PRESENT),
            _make_record("R2", "S001", "CS101", d,   1, AttendanceStatus.PRESENT),
        ]
        calc, student, _ = self._build_calculator(records, holidays=[hol])
        summary = calc.calculate_for_student(student)
        # Only the non-holiday record counts
        assert summary.total_conducted == 1
        assert summary.total_present   == 1

    # R4: Lab period counts as 2

    def test_R4_lab_weight(self):
        d = date(2024, 7, 29)
        lab = _make_course("CS191", is_lab=True)
        records = [
            _make_record("R1", "S001", "CS191", d, 1, AttendanceStatus.PRESENT),
        ]
        calc, student, _ = self._build_calculator(records, course=lab)
        summary = calc.calculate_for_student(student)
        assert summary.total_conducted == 2   # weight=2
        assert summary.total_present   == 2

    # R5: LATE within grace → PRESENT

    def test_R5_late_within_grace_counts_as_present(self):
        d = date(2024, 7, 29)   # Monday
        slot_start = time(8, 0)
        arrival    = time(8, 7)   # 7 minutes late → within 10-min grace
        records = [
            _make_record("R1", "S001", "CS101", d, 1,
                         AttendanceStatus.LATE, arrival=arrival),
        ]
        slots = [_make_timetable_slot(day="MON", period=1, start=slot_start)]
        calc, student, _ = self._build_calculator(records, slots=slots)
        summary = calc.calculate_for_student(student)
        assert summary.total_present == 1

    # R5: LATE beyond grace → ABSENT

    def test_R5_late_beyond_grace_counts_as_absent(self):
        d = date(2024, 7, 29)
        slot_start = time(8, 0)
        arrival    = time(8, 15)   # 15 minutes → beyond 10-min grace
        records = [
            _make_record("R1", "S001", "CS101", d, 1,
                         AttendanceStatus.LATE, arrival=arrival),
        ]
        slots = [_make_timetable_slot(day="MON", period=1, start=slot_start)]
        calc, student, _ = self._build_calculator(records, slots=slots)
        summary = calc.calculate_for_student(student)
        assert summary.total_present == 0
        assert summary.total_conducted == 1

    # R6: OD without approval → ABSENT

    def test_R6_od_without_approval_is_absent(self):
        d = date(2024, 7, 29)
        records = [
            _make_record("R1", "S001", "CS101", d, 1, AttendanceStatus.OD),
        ]
        calc, student, _ = self._build_calculator(records, od_approvals=[])
        summary = calc.calculate_for_student(student)
        assert summary.total_present == 0

    # R6: OD with approval → PRESENT

    def test_R6_od_with_approval_is_present(self):
        d = date(2024, 7, 29)
        records = [
            _make_record("R1", "S001", "CS101", d, 1, AttendanceStatus.OD),
        ]
        od = [ODApproval("OD01", "S001", "CS101", d, "Placement", "HOD")]
        calc, student, _ = self._build_calculator(records, od_approvals=od)
        summary = calc.calculate_for_student(student)
        assert summary.total_present == 1

    # R6: Placement-event OD (wildcard course) → PRESENT

    def test_R6_placement_od_wildcard(self):
        d = date(2024, 7, 29)
        records = [
            _make_record("R1", "S001", "CS101", d, 1, AttendanceStatus.OD),
        ]
        pe = [PlacementEvent("PE01", d, "TCS", ["S001"])]
        calc, student, _ = self._build_calculator(records, placement=pe)
        summary = calc.calculate_for_student(student)
        assert summary.total_present == 1

    # R7: Lateral-entry student – records before admission_date excluded

    def test_R7_lateral_before_admission_excluded(self):
        # Student joins 2 weeks after semester start
        adm = SEMESTER_START + timedelta(weeks=2)
        student = _make_student(is_lateral=True, adm_date=adm)

        # One record before admission, one after
        d_before = SEMESTER_START + timedelta(days=1)   # working Monday
        d_after  = adm + timedelta(days=1)              # working day after admission

        records = [
            _make_record("R1", student.student_id, "CS101", d_before, 1,
                         AttendanceStatus.PRESENT),
            _make_record("R2", student.student_id, "CS101", d_after, 1,
                         AttendanceStatus.PRESENT),
        ]
        calc, _, _ = self._build_calculator(records, student=student)
        summary = calc.calculate_for_student(student)
        # Only d_after record should count
        assert summary.total_conducted == 1
        assert summary.total_present   == 1

    # R7: Regular student – all records from semester start count

    def test_R7_regular_student_all_records_count(self):
        student = _make_student(is_lateral=False)
        d = SEMESTER_START + timedelta(days=1)
        records = [
            _make_record("R1", student.student_id, "CS101", d, 1,
                         AttendanceStatus.PRESENT),
        ]
        calc, _, _ = self._build_calculator(records, student=student)
        summary = calc.calculate_for_student(student)
        assert summary.total_conducted == 1
        assert summary.total_present   == 1

    # R8: Percentage calculation

    def test_R8_percentage_calculation(self):
        d1 = date(2024, 7, 29)
        d2 = date(2024, 7, 30)
        d3 = date(2024, 7, 31)
        d4 = date(2024, 8, 1)
        records = [
            _make_record("R1", "S001", "CS101", d1, 1, AttendanceStatus.PRESENT),
            _make_record("R2", "S001", "CS101", d2, 1, AttendanceStatus.PRESENT),
            _make_record("R3", "S001", "CS101", d3, 1, AttendanceStatus.PRESENT),
            _make_record("R4", "S001", "CS101", d4, 1, AttendanceStatus.ABSENT),
        ]
        calc, student, _ = self._build_calculator(records)
        summary = calc.calculate_for_student(student)
        assert summary.total_conducted == 4
        assert summary.total_present   == 3
        assert abs(summary.overall_percentage - 75.0) < 0.01

    # R8: Display percentage rounded to 2 decimal places

    def test_R8_display_rounding(self):
        d1 = date(2024, 7, 29)
        d2 = date(2024, 7, 30)
        d3 = date(2024, 7, 31)
        records = [
            _make_record("R1", "S001", "CS101", d1, 1, AttendanceStatus.PRESENT),
            _make_record("R2", "S001", "CS101", d2, 1, AttendanceStatus.PRESENT),
            _make_record("R3", "S001", "CS101", d3, 1, AttendanceStatus.ABSENT),
        ]
        calc, student, _ = self._build_calculator(records)
        summary = calc.calculate_for_student(student)
        # 2/3 = 66.666... → display = "66.67"
        assert summary.display_percentage == "66.67"

    # R9: Eligibility threshold

    def test_R9_eligibility_at_75(self):
        d1 = date(2024, 7, 29)
        d2 = date(2024, 7, 30)
        d3 = date(2024, 7, 31)
        d4 = date(2024, 8, 1)
        records = [
            _make_record("R1", "S001", "CS101", d1, 1, AttendanceStatus.PRESENT),
            _make_record("R2", "S001", "CS101", d2, 1, AttendanceStatus.PRESENT),
            _make_record("R3", "S001", "CS101", d3, 1, AttendanceStatus.PRESENT),
            _make_record("R4", "S001", "CS101", d4, 1, AttendanceStatus.ABSENT),
        ]
        calc, student, _ = self._build_calculator(records)
        summary = calc.calculate_for_student(student)
        assert summary.is_eligible           # 75.0 >= 75.0

    def test_R9_ineligible_below_75(self):
        d1 = date(2024, 7, 29)
        d2 = date(2024, 7, 30)
        records = [
            _make_record("R1", "S001", "CS101", d1, 1, AttendanceStatus.PRESENT),
            _make_record("R2", "S001", "CS101", d2, 1, AttendanceStatus.ABSENT),
        ]
        calc, student, _ = self._build_calculator(records)
        summary = calc.calculate_for_student(student)
        assert not summary.is_eligible       # 50.0 < 75.0

    # Working Saturday timetable rule

    def test_working_saturday_uses_replaced_day_timetable(self):
        """
        A working Saturday labelled 'follows MON' should look up the MON
        slots in the timetable – i.e., CalendarEntry.timetable_day() == 'MON'.
        """
        wsat_date = date(2024, 8, 10)   # a Saturday
        entry = CalendarEntry(wsat_date, DayType.WORKING_SATURDAY, "MON")
        assert entry.timetable_day() == "MON"
        assert entry.is_working()

    # Zero conducted periods edge case

    def test_zero_conducted_percentage(self):
        # No records → 0 conducted, 0 present, pct = 0.0
        calc, student, _ = self._build_calculator([])
        summary = calc.calculate_for_student(student)
        assert summary.overall_percentage == 0.0
        assert summary.total_conducted    == 0

    # Multiple courses aggregation

    def test_overall_aggregates_multiple_courses(self):
        d = date(2024, 7, 29)
        c1 = Course("CS101", "Math",    4, False)
        c2 = Course("CS102", "Physics", 3, False)
        # 1 present in CS101, 0 present in CS102
        records = [
            _make_record("R1", "S001", "CS101", d, 1, AttendanceStatus.PRESENT),
            _make_record("R2", "S001", "CS102", d, 2, AttendanceStatus.ABSENT),
        ]
        courses = {c1.course_id: c1, c2.course_id: c2}
        student = _make_student()
        cal = _make_calendar()
        cal_mgr = CalendarManager(cal, SEMESTER_START, SEMESTER_END)
        stu_mgr = StudentManager({student.student_id: student})
        crs_mgr = CourseManager(courses)
        att_mgr = AttendanceManager(records, [], [])
        calc    = AttendanceCalculator(att_mgr, cal_mgr, crs_mgr, stu_mgr)

        summary = calc.calculate_for_student(student)
        assert summary.total_conducted == 2
        assert summary.total_present   == 1
        assert len(summary.course_summaries) == 2


# ===========================================================================
# CommandParser tests
# ===========================================================================

class TestCommandParser:
    def test_dispatch_registered_command(self):
        called = []
        parser = CommandParser()
        parser.register("LOAD_DATA", lambda args: called.append(("LOAD", args)))
        parser.run_line("LOAD_DATA ./data")
        assert len(called) == 1
        assert called[0] == ("LOAD", ["./data"])

    def test_unknown_command_no_exception(self):
        parser = CommandParser()
        # Should log warning but not raise
        parser.run_line("UNKNOWN_COMMAND arg1")

    def test_comment_lines_ignored(self):
        called = []
        parser = CommandParser()
        parser.register("CMD", lambda args: called.append(args))
        parser.run_line("# This is a comment")
        assert len(called) == 0

    def test_blank_lines_ignored(self):
        called = []
        parser = CommandParser()
        parser.register("CMD", lambda args: called.append(args))
        parser.run_line("   ")
        assert len(called) == 0

    def test_exit_stops_parsing(self):
        called = []
        parser = CommandParser()
        parser.register("CMD", lambda args: called.append(args))
        import io
        import tempfile
        content = "CMD a\nEXIT\nCMD b\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False, encoding="utf-8") as f:
            f.write(content)
            fname = f.name
        parser.run_file(fname)
        assert len(called) == 1   # only CMD a, not CMD b

    def test_multiple_args(self):
        received = []
        parser = CommandParser()
        parser.register("SHOW", lambda args: received.extend(args))
        parser.run_line("SHOW S001 2024-11-01")
        assert received == ["S001", "2024-11-01"]


# ===========================================================================
# Argument parsing helpers
# ===========================================================================

class TestArgHelpers:
    def test_parse_bool_true(self):
        for v in ("TRUE", "true", "1", "YES", "yes", "Y"):
            assert parse_bool_arg(v)

    def test_parse_bool_false(self):
        for v in ("FALSE", "false", "0", "NO", "no", "N"):
            assert not parse_bool_arg(v)

    def test_parse_date(self):
        assert parse_date_arg("2024-07-29") == date(2024, 7, 29)

    def test_parse_date_invalid(self):
        with pytest.raises(ValueError):
            parse_date_arg("29-07-2024")


# ===========================================================================
# Integration smoke test
# ===========================================================================

class TestIntegration:
    """
    End-to-end smoke test using synthetic data close to the real project scale.
    """

    def test_section_calculation_returns_all_students(self):
        from datetime import timedelta

        n = 10
        section = "A"
        students = {
            f"S{i:03d}": Student(
                student_id=f"S{i:03d}",
                name=f"Student {i}",
                programme="B.Tech CSE",
                semester=3,
                section=section,
                admission_date=SEMESTER_START,
            )
            for i in range(n)
        }
        course = Course("CS101", "Math", 4, False)
        d = SEMESTER_START + timedelta(days=1)

        records = []
        for sid in students:
            records.append(_make_record(f"R{sid}", sid, "CS101", d, 1,
                                        AttendanceStatus.PRESENT))

        cal     = _make_calendar()
        cal_mgr = CalendarManager(cal, SEMESTER_START, SEMESTER_END)
        stu_mgr = StudentManager(students)
        crs_mgr = CourseManager({"CS101": course})
        att_mgr = AttendanceManager(records, [], [])
        calc    = AttendanceCalculator(att_mgr, cal_mgr, crs_mgr, stu_mgr)

        summaries = calc.calculate_for_section(section)
        assert len(summaries) == n
        for s in summaries:
            assert s.total_present == 1
            assert s.total_conducted == 1
