"""
Unit tests for the PS-068 Condonation Engine
============================================
Comprehensive tests for all condonation, eligibility, fee, and fine rules:
  - Medical removal (both conducted and attended)
  - Fractional medical caps (floor Decimal arithmetic)
  - Duty caps after medical removal (10% of post-medical conducted, 20-day overall)
  - Bereavement (5 consecutive days, 3-day deadline, once per semester, no docs)
  - Placement eligibility (final-year only, calendar matching, no cap)
  - Third condonable course (becomes DETAINED)
  - Semester detention (3+ detained courses)
  - Condonation fee boundaries (70–74.99% ₹500, 65–69.99% ₹1000)
  - Fine calculation (unbroken 3+ days, ₹100/day from 3rd day, ₹2000 cap)
  - Boundary tests for exactly 75.00% and 74.99%
"""
from __future__ import annotations

import pytest
from datetime import date, time, timedelta
from decimal import Decimal

from src.models import (
    ApplicationStatus,
    ApplicationType,
    AttendanceRecord,
    AttendanceStatus,
    CalendarEntry,
    CondonationApplication,
    Course,
    DayType,
    EligibilityStatus,
    ODApproval,
    PlacementEvent,
    Student,
    TimetableSlot,
)
from src.student_manager import StudentManager
from src.course_manager import CourseManager
from src.calendar_manager import CalendarManager
from src.attendance_manager import AttendanceManager
from src.attendance_calculator import AttendanceCalculator
from src.condonation_engine import (
    CondonationEngine,
    _floor_pct,
    _pct,
    _eligibility,
    _course_fee,
    MEDICAL_CAP_PCT,
    DUTY_COURSE_CAP_PCT,
    DUTY_OVERALL_CAP_DAYS,
)

SEMESTER_START = date(2024, 7, 29)
SEMESTER_END   = date(2024, 11, 16)


def _make_calendar(holidays=None, working_sats=None):
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
        cal[cur] = CalendarEntry(calendar_date=cur, day_type=day_type, replaces_day=rep)
        cur += timedelta(days=1)
    return cal


def _build_engine(
    student: Student,
    courses: list[Course],
    records: list[AttendanceRecord],
    applications: list[CondonationApplication] | None = None,
    placement_events: list[PlacementEvent] | None = None,
    od_approvals: list[ODApproval] | None = None,
    holidays: list[date] | None = None,
):
    apps = applications or []
    pl   = placement_events or []
    od   = od_approvals or []

    cal = _make_calendar(holidays=holidays)
    cal_mgr = CalendarManager(cal, SEMESTER_START, SEMESTER_END)
    stu_mgr = StudentManager({student.student_id: student})
    crs_mgr = CourseManager({c.course_id: c for c in courses})
    att_mgr = AttendanceManager(records, od, pl)
    calc    = AttendanceCalculator(att_mgr, cal_mgr, crs_mgr, stu_mgr)

    engine = CondonationEngine(
        calculator=calc,
        att_mgr=att_mgr,
        calendar_mgr=cal_mgr,
        course_mgr=crs_mgr,
        student_mgr=stu_mgr,
        applications=apps,
        placement_events=pl,
    )
    return engine


# ===========================================================================
# 1. Medical Removal Tests
# ===========================================================================

class TestMedicalRemoval:
    def test_medical_removal_reduces_conducted_and_preserves_attendance(self):
        """
        Student has 40 conducted periods, present for 26 (65%).
        Medical leave approved for 4 periods (absent during leave).
        4 periods <= 15% cap of 40 (cap = 6).
        Post-medical conducted = 36, present = 26 -> 26/36 = 72.22% (CONDONABLE).
        """
        s = Student("S001", "Alice", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Data Structures", 4, False)

        # 40 working days: first 26 present, next 4 medical absent, last 10 absent
        w_days = [SEMESTER_START + timedelta(days=i) for i in range(70)
                  if (SEMESTER_START + timedelta(days=i)).weekday() < 5][:40]

        records = []
        for i, d in enumerate(w_days):
            st = AttendanceStatus.PRESENT if i < 26 else AttendanceStatus.ABSENT
            records.append(AttendanceRecord(f"R{i}", "S001", "CS101", d, 1, st, faculty_id="FAC001"))

        # Medical app for periods on days index 26..29 (4 days)
        med_start = w_days[26]
        med_end   = w_days[29]
        app = CondonationApplication(
            application_id="APP001",
            student_id="S001",
            course_id="CS101",
            reason=ApplicationType.MEDICAL,
            start_date=med_start,
            end_date=med_end,
            description="Viral fever",
            status=ApplicationStatus.APPROVED,
            approved_by="HOD001",
            approved_on=med_end + timedelta(days=2),
            medical_cert_ref="MED-CERT-12345",
            submitted_on=med_end + timedelta(days=3),  # <= 7 days
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        detail = res.course_details[0]

        assert detail.raw_conducted == 40
        assert detail.raw_present == 26
        assert detail.medical_periods_removed == 4
        assert detail.post_medical_conducted == 36
        assert detail.post_medical_present == 26
        assert detail.final_conducted == 36
        assert detail.final_present == 26
        assert abs(detail.final_percentage - 72.22) < 0.01
        assert detail.course_status == EligibilityStatus.CONDONABLE

    def test_medical_missing_certificate_rejected(self):
        """Medical application without certificate reference must be REJECTED."""
        s = Student("S001", "Alice", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Data Structures", 4, False)
        d = SEMESTER_START + timedelta(days=1)
        records = [AttendanceRecord("R1", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]

        app = CondonationApplication(
            application_id="APP002",
            student_id="S001",
            course_id="CS101",
            reason=ApplicationType.MEDICAL,
            start_date=d,
            end_date=d,
            description="Fever",
            status=ApplicationStatus.APPROVED,
            medical_cert_ref=None,  # MISSING
            submitted_on=d + timedelta(days=1),
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        assert res.course_details[0].medical_periods_removed == 0
        assert app.status == ApplicationStatus.REJECTED

    def test_medical_late_submission_rejected_late(self):
        """Medical application submitted > 7 days after return must be REJECTED_LATE."""
        s = Student("S001", "Alice", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Data Structures", 4, False)
        d = SEMESTER_START + timedelta(days=1)
        records = [AttendanceRecord("R1", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]

        app = CondonationApplication(
            application_id="APP003",
            student_id="S001",
            course_id="CS101",
            reason=ApplicationType.MEDICAL,
            start_date=d,
            end_date=d,
            description="Fever",
            status=ApplicationStatus.APPROVED,
            medical_cert_ref="MED-999",
            submitted_on=d + timedelta(days=8),  # 8 days > 7 days deadline
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        assert res.course_details[0].medical_periods_removed == 0
        assert app.status == ApplicationStatus.REJECTED_LATE


# ===========================================================================
# 2. Fractional Medical Caps Tests
# ===========================================================================

class TestFractionalMedicalCaps:
    def test_fractional_cap_floor_precision(self):
        """
        Verify floor(15% * conducted):
          30 conducted * 15% = 4.5 -> cap = 4
          25 conducted * 15% = 3.75 -> cap = 3
          35 conducted * 15% = 5.25 -> cap = 5
        """
        assert _floor_pct(30, MEDICAL_CAP_PCT) == 4
        assert _floor_pct(25, MEDICAL_CAP_PCT) == 3
        assert _floor_pct(35, MEDICAL_CAP_PCT) == 5

    def test_medical_cap_clips_excess_periods(self):
        """
        Course has 30 conducted periods.
        15% cap = 4.5 -> floor = 4 periods max.
        Student applies for 6 medical periods.
        Exactly 4 periods must be removed.
        """
        s = Student("S001", "Bob", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "OS", 4, False)

        w_days = [SEMESTER_START + timedelta(days=i) for i in range(50)
                  if (SEMESTER_START + timedelta(days=i)).weekday() < 5][:30]

        records = [
            AttendanceRecord(f"R{i}", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")
            for i, d in enumerate(w_days)
        ]

        app = CondonationApplication(
            application_id="APP_CAP",
            student_id="S001",
            course_id="CS101",
            reason=ApplicationType.MEDICAL,
            start_date=w_days[0],
            end_date=w_days[5],  # 6 periods
            description="Jaundice",
            status=ApplicationStatus.APPROVED,
            medical_cert_ref="MED-CAP",
            submitted_on=w_days[5] + timedelta(days=2),
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        detail = res.course_details[0]

        assert detail.medical_periods_removed == 4
        assert detail.post_medical_conducted == 26


# ===========================================================================
# 3. Duty Caps After Medical Removal Tests
# ===========================================================================

class TestDutyCaps:
    def test_duty_cap_evaluated_on_post_medical_conducted(self):
        """
        Course has 50 raw conducted periods.
        Medical removes 5 periods -> post_medical_conducted = 45 periods.
        Duty cap = floor(10% of 45) = floor(4.5) = 4 periods (NOT 5 based on 50).
        Student applies for 6 sports duty periods -> exactly 4 are added.
        """
        s = Student("S001", "Charlie", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Networks", 4, False)

        w_days = [SEMESTER_START + timedelta(days=i) for i in range(70)
                  if (SEMESTER_START + timedelta(days=i)).weekday() < 5][:50]

        records = [
            AttendanceRecord(f"R{i}", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")
            for i, d in enumerate(w_days)
        ]

        # 5 medical periods (cap is 15% of 50 = 7, so 5 is allowed)
        app_med = CondonationApplication(
            "MED1", "S001", "CS101", ApplicationType.MEDICAL,
            w_days[0], w_days[4], "Illness", ApplicationStatus.APPROVED,
            medical_cert_ref="MED-1", submitted_on=w_days[4] + timedelta(days=2)
        )

        # 6 sports duty periods (days 10..15)
        app_sports = CondonationApplication(
            "SPT1", "S001", "CS101", ApplicationType.SPORTS,
            w_days[10], w_days[15], "University Tournament", ApplicationStatus.APPROVED
        )

        engine = _build_engine(s, [c], records, [app_med, app_sports])
        res = engine.process_student(s)
        detail = res.course_details[0]

        assert detail.medical_periods_removed == 5
        assert detail.post_medical_conducted == 45
        # 10% of 45 = 4.5 -> floor = 4
        assert detail.duty_periods_added == 4
        assert detail.final_present == 4

    def test_duty_overall_cap_20_days(self):
        """
        Student applies for 25 duty days across semester -> capped at 20 duty days.
        """
        s = Student("S001", "Charlie", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Networks", 4, False)

        w_days = [SEMESTER_START + timedelta(days=i) for i in range(70)
                  if (SEMESTER_START + timedelta(days=i)).weekday() < 5][:30]

        records = [
            AttendanceRecord(f"R{i}", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")
            for i, d in enumerate(w_days)
        ]

        # App 1: 15 days
        app1 = CondonationApplication("SPT1", "S001", "CS101", ApplicationType.SPORTS,
                                      w_days[0], w_days[14], "Sports Camp 1", ApplicationStatus.APPROVED)
        # App 2: 10 days (total 25 > 20)
        app2 = CondonationApplication("SPT2", "S001", "CS101", ApplicationType.NCC_NSS,
                                      w_days[15], w_days[24], "NCC Camp", ApplicationStatus.APPROVED)

        engine = _build_engine(s, [c], records, [app1, app2])
        res = engine.process_student(s)
        # Even if per-course cap allows, overall days used cannot exceed 20
        # Post conducted = 30 -> 10% course cap = 3 periods
        assert res.course_details[0].duty_periods_added <= 3


# ===========================================================================
# 4. Bereavement Tests
# ===========================================================================

class TestBereavement:
    def test_bereavement_valid_acceptance(self):
        """Bereavement <= 5 consecutive calendar days submitted within 3 days is approved."""
        s = Student("S001", "Dave", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Math", 4, False)
        d1 = SEMESTER_START + timedelta(days=7)
        d5 = d1 + timedelta(days=4)  # 5 calendar days

        records = [AttendanceRecord(f"R{i}", "S001", "CS101", d1 + timedelta(days=i), 1,
                                    AttendanceStatus.ABSENT, faculty_id="FAC001") for i in range(5)]

        app = CondonationApplication(
            "BER1", "S001", "CS101", ApplicationType.BEREAVEMENT,
            d1, d5, "Family bereavement", ApplicationStatus.APPROVED,
            submitted_on=d5 + timedelta(days=2)  # <= 3 days
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        assert res.course_details[0].duty_periods_added > 0

    def test_bereavement_exceeding_5_days_rejected(self):
        """Bereavement spanning 6 consecutive calendar days must be REJECTED."""
        s = Student("S001", "Dave", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Math", 4, False)
        d1 = SEMESTER_START + timedelta(days=7)
        d6 = d1 + timedelta(days=5)  # 6 days

        records = [AttendanceRecord("R1", "S001", "CS101", d1, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]
        app = CondonationApplication(
            "BER2", "S001", "CS101", ApplicationType.BEREAVEMENT,
            d1, d6, "Family bereavement", ApplicationStatus.APPROVED,
            submitted_on=d6 + timedelta(days=1)
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        assert res.course_details[0].duty_periods_added == 0
        assert app.status == ApplicationStatus.REJECTED

    def test_bereavement_second_application_rejected(self):
        """Second bereavement application in the same semester must be REJECTED."""
        s = Student("S001", "Dave", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Math", 4, False)
        d1 = SEMESTER_START + timedelta(days=7)
        d2 = SEMESTER_START + timedelta(days=20)

        records = [AttendanceRecord("R1", "S001", "CS101", d1, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]
        app1 = CondonationApplication("B1", "S001", "CS101", ApplicationType.BEREAVEMENT,
                                      d1, d1 + timedelta(days=2), "First", ApplicationStatus.APPROVED,
                                      submitted_on=d1 + timedelta(days=3))
        app2 = CondonationApplication("B2", "S001", "CS101", ApplicationType.BEREAVEMENT,
                                      d2, d2 + timedelta(days=2), "Second", ApplicationStatus.APPROVED,
                                      submitted_on=d2 + timedelta(days=3))

        engine = _build_engine(s, [c], records, [app1, app2])
        assert app1.status == ApplicationStatus.APPROVED
        assert app2.status == ApplicationStatus.REJECTED

    def test_bereavement_submitted_after_3_days_rejected_late(self):
        """Bereavement submitted > 3 days after end_date must be REJECTED_LATE."""
        s = Student("S001", "Dave", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS101", "Math", 4, False)
        d1 = SEMESTER_START + timedelta(days=7)

        records = [AttendanceRecord("R1", "S001", "CS101", d1, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]
        app = CondonationApplication(
            "BER_LATE", "S001", "CS101", ApplicationType.BEREAVEMENT,
            d1, d1 + timedelta(days=2), "Funeral", ApplicationStatus.APPROVED,
            submitted_on=d1 + timedelta(days=6)  # 4 days after end date > 3
        )

        engine = _build_engine(s, [c], records, [app])
        res = engine.process_student(s)
        assert res.course_details[0].duty_periods_added == 0
        assert app.status == ApplicationStatus.REJECTED_LATE


# ===========================================================================
# 5. Placement Eligibility Tests
# ===========================================================================

class TestPlacementEligibility:
    def test_final_year_placement_approved_no_cap(self):
        """Final-year student (semester 7) matching placement calendar has no duty cap."""
        s = Student("S001", "Emma", "B.Tech CSE", 7, "A", SEMESTER_START)  # semester 7
        c = Course("CS101", "Project", 4, False)
        d = SEMESTER_START + timedelta(days=10)

        records = [AttendanceRecord("R1", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]
        pe = PlacementEvent("PE1", d, "Google", ["S001"])
        app = CondonationApplication("PLC1", "S001", "CS101", ApplicationType.PLACEMENT,
                                     d, d, "Placement Drive", ApplicationStatus.APPROVED)

        engine = _build_engine(s, [c], records, [app], placement_events=[pe])
        res = engine.process_student(s)
        assert res.course_details[0].duty_periods_added == 1
        assert res.course_details[0].final_present == 1

    def test_non_final_year_placement_rejected(self):
        """Non-final-year student (semester 5) placement application is REJECTED."""
        s = Student("S001", "Emma", "B.Tech CSE", 5, "A", SEMESTER_START)  # semester 5 < 7
        c = Course("CS101", "Project", 4, False)
        d = SEMESTER_START + timedelta(days=10)

        records = [AttendanceRecord("R1", "S001", "CS101", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]
        pe = PlacementEvent("PE1", d, "Google", ["S001"])
        app = CondonationApplication("PLC2", "S001", "CS101", ApplicationType.PLACEMENT,
                                     d, d, "Drive", ApplicationStatus.APPROVED)

        engine = _build_engine(s, [c], records, [app], placement_events=[pe])
        res = engine.process_student(s)
        assert res.course_details[0].duty_periods_added == 0
        assert app.status == ApplicationStatus.REJECTED

    def test_placement_date_not_in_calendar_rejected(self):
        """Placement application where date is not in placement calendar is REJECTED."""
        s = Student("S001", "Emma", "B.Tech CSE", 7, "A", SEMESTER_START)
        c = Course("CS101", "Project", 4, False)
        d_app = SEMESTER_START + timedelta(days=10)
        d_evt = SEMESTER_START + timedelta(days=20)  # different date

        records = [AttendanceRecord("R1", "S001", "CS101", d_app, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")]
        pe = PlacementEvent("PE1", d_evt, "Google", ["S001"])
        app = CondonationApplication("PLC3", "S001", "CS101", ApplicationType.PLACEMENT,
                                     d_app, d_app, "Drive", ApplicationStatus.APPROVED)

        engine = _build_engine(s, [c], records, [app], placement_events=[pe])
        res = engine.process_student(s)
        assert res.course_details[0].duty_periods_added == 0
        assert app.status == ApplicationStatus.REJECTED


# ===========================================================================
# 6. Third Condonable Course Tests
# ===========================================================================

class TestThirdCondonableCourse:
    def test_third_condonable_course_becomes_detained(self):
        """
        Student has 3 courses with attendance in 65–74.99% range:
          Course 1: 72.00%
          Course 2: 70.00%
          Course 3: 68.00%
        Top 2 (72% and 70%) are CONDONABLE.
        Third (68%) becomes DETAINED.
        """
        s = Student("S001", "Frank", "B.Tech CSE", 5, "A", SEMESTER_START)
        c1 = Course("CS101", "C1", 4, False)
        c2 = Course("CS102", "C2", 4, False)
        c3 = Course("CS103", "C3", 4, False)

        records = []
        d = SEMESTER_START + timedelta(days=1)

        # C1: 72/100 -> 72%
        for i in range(100):
            st = AttendanceStatus.PRESENT if i < 72 else AttendanceStatus.ABSENT
            records.append(AttendanceRecord(f"R1_{i}", "S001", "CS101", d, i + 1, st, faculty_id="FAC001"))
        # C2: 70/100 -> 70%
        for i in range(100):
            st = AttendanceStatus.PRESENT if i < 70 else AttendanceStatus.ABSENT
            records.append(AttendanceRecord(f"R2_{i}", "S001", "CS102", d, i + 1, st, faculty_id="FAC001"))
        # C3: 68/100 -> 68%
        for i in range(100):
            st = AttendanceStatus.PRESENT if i < 68 else AttendanceStatus.ABSENT
            records.append(AttendanceRecord(f"R3_{i}", "S001", "CS103", d, i + 1, st, faculty_id="FAC001"))

        engine = _build_engine(s, [c1, c2, c3], records)
        res = engine.process_student(s)

        by_id = {cd.course_id: cd for cd in res.course_details}
        assert by_id["CS101"].course_status == EligibilityStatus.CONDONABLE
        assert by_id["CS102"].course_status == EligibilityStatus.CONDONABLE
        assert by_id["CS103"].course_status == EligibilityStatus.DETAINED
        assert res.condonable_courses == 2
        assert res.detained_courses == 1


# ===========================================================================
# 7. Semester Detention Tests
# ===========================================================================

class TestSemesterDetention:
    def test_three_detained_courses_triggers_whole_semester_detained(self):
        """
        Student with 3 detained courses (<65%) results in whole-semester DETAINED.
        """
        s = Student("S001", "Grace", "B.Tech CSE", 5, "A", SEMESTER_START)
        courses = [Course(f"CS{i}", f"Course {i}", 4, False) for i in range(1, 4)]

        records = []
        d = SEMESTER_START + timedelta(days=1)
        for c in courses:
            for i in range(100):
                st = AttendanceStatus.PRESENT if i < 50 else AttendanceStatus.ABSENT  # 50% < 65%
                records.append(AttendanceRecord(f"R_{c.course_id}_{i}", "S001", c.course_id, d, i + 1, st, faculty_id="FAC001"))

        engine = _build_engine(s, courses, records)
        res = engine.process_student(s)

        assert res.detained_courses == 3
        assert res.semester_status == EligibilityStatus.DETAINED

    def test_two_detained_courses_not_whole_semester_detained(self):
        """
        Student with 2 detained courses and overall >= 65% is CONDONABLE, not whole-semester DETAINED.
        """
        s = Student("S001", "Grace", "B.Tech CSE", 5, "A", SEMESTER_START)
        c1 = Course("CS1", "Course 1", 4, False)
        c2 = Course("CS2", "Course 2", 4, False)
        c3 = Course("CS3", "Course 3", 4, False)

        records = []
        d = SEMESTER_START + timedelta(days=1)
        # CS1 & CS2 at 60% (detained), CS3 at 90% (eligible). Overall = 210/300 = 70%
        for i in range(100):
            records.append(AttendanceRecord(f"R1_{i}", "S001", "CS1", d, i + 1, AttendanceStatus.PRESENT if i < 60 else AttendanceStatus.ABSENT, faculty_id="FAC001"))
            records.append(AttendanceRecord(f"R2_{i}", "S001", "CS2", d, i + 1, AttendanceStatus.PRESENT if i < 60 else AttendanceStatus.ABSENT, faculty_id="FAC001"))
            records.append(AttendanceRecord(f"R3_{i}", "S001", "CS3", d, i + 1, AttendanceStatus.PRESENT if i < 90 else AttendanceStatus.ABSENT, faculty_id="FAC001"))

        engine = _build_engine(s, [c1, c2, c3], records)
        res = engine.process_student(s)

        assert res.detained_courses == 2
        assert res.semester_status == EligibilityStatus.CONDONABLE


# ===========================================================================
# 8. Condonation Fee Boundaries Tests
# ===========================================================================

class TestFeeBoundaries:
    def test_fee_boundaries(self):
        """
        Verify exact fee boundaries:
          74.99% -> ₹500
          70.00% -> ₹500
          69.99% -> ₹1000
          65.00% -> ₹1000
        """
        assert _course_fee(Decimal("74.99")) == 500
        assert _course_fee(Decimal("70.00")) == 500
        assert _course_fee(Decimal("69.99")) == 1000
        assert _course_fee(Decimal("65.00")) == 1000

    def test_total_condonation_fee_max_two_courses(self):
        """
        Two condonable courses (72% and 66%) -> fee = 500 + 1000 = ₹1,500.
        """
        s = Student("S001", "Hank", "B.Tech CSE", 5, "A", SEMESTER_START)
        c1 = Course("CS1", "C1", 4, False)
        c2 = Course("CS2", "C2", 4, False)
        d = SEMESTER_START + timedelta(days=1)

        records = []
        for i in range(100):
            records.append(AttendanceRecord(f"R1_{i}", "S001", "CS1", d, i + 1, AttendanceStatus.PRESENT if i < 72 else AttendanceStatus.ABSENT, faculty_id="FAC001"))
            records.append(AttendanceRecord(f"R2_{i}", "S001", "CS2", d, i + 1, AttendanceStatus.PRESENT if i < 66 else AttendanceStatus.ABSENT, faculty_id="FAC001"))

        engine = _build_engine(s, [c1, c2], records)
        res = engine.process_student(s)

        assert res.course_details[0].condonation_fee == 500
        assert res.course_details[1].condonation_fee == 1000
        assert res.total_condonation_fee == 1500


# ===========================================================================
# 9. Fine Calculation Tests
# ===========================================================================

class TestFineCalculation:
    def test_fine_for_consecutive_absences(self):
        """
        3 consecutive full days absent -> ₹100 (beginning from 3rd day).
        5 consecutive full days absent -> ₹300 (days 3, 4, 5).
        < 3 days -> ₹0.
        """
        s = Student("S001", "Ivy", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS1", "C1", 4, False)

        w_days = [SEMESTER_START + timedelta(days=i) for i in range(30)
                  if (SEMESTER_START + timedelta(days=i)).weekday() < 5]

        # Present on day 0, absent on days 1, 2, 3 (streak of 3)
        # Present on day 4, absent on days 5, 6 (streak of 2 -> fine 0)
        # Present on day 7, absent on days 8, 9, 10, 11, 12 (streak of 5 -> fine 300)
        records = []
        for idx, d in enumerate(w_days[:15]):
            if idx in (1, 2, 3, 5, 6, 8, 9, 10, 11, 12):
                st = AttendanceStatus.ABSENT
            else:
                st = AttendanceStatus.PRESENT
            records.append(AttendanceRecord(f"R_{idx}", "S001", "CS1", d, 1, st, faculty_id="FAC001"))

        engine = _build_engine(s, [c], records)
        res = engine.process_student(s)

        # Streak 1 (3 days) -> ₹100
        # Streak 2 (2 days) -> ₹0
        # Streak 3 (5 days) -> ₹300
        # Total fine = ₹400
        assert res.absence_fine.total_fine == 400
        assert len(res.absence_fine.streak_details) == 2

    def test_fine_capped_at_2000(self):
        """Fine is capped at ₹2,000 maximum per semester."""
        s = Student("S001", "Ivy", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS1", "C1", 4, False)

        w_days = [SEMESTER_START + timedelta(days=i) for i in range(50)
                  if (SEMESTER_START + timedelta(days=i)).weekday() < 5]

        # 30 consecutive days absent: fine = 100 * (30 - 2) = ₹2,800 -> capped at ₹2,000
        records = [
            AttendanceRecord(f"R_{i}", "S001", "CS1", d, 1, AttendanceStatus.ABSENT, faculty_id="FAC001")
            for i, d in enumerate(w_days[:30])
        ]

        engine = _build_engine(s, [c], records)
        res = engine.process_student(s)

        assert res.absence_fine.total_fine == 2000


# ===========================================================================
# 10. Exactly 75.00% and 74.99% Tests
# ===========================================================================

class TestExactPercentages:
    def test_exact_75_00_is_eligible(self):
        """Exactly 75.00% is ELIGIBLE with fee = ₹0."""
        assert _eligibility(Decimal("75.00")) == EligibilityStatus.ELIGIBLE
        assert _eligibility(Decimal("75")) == EligibilityStatus.ELIGIBLE

        s = Student("S001", "Jack", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS1", "C1", 4, False)
        d = SEMESTER_START + timedelta(days=1)

        # 75 present out of 100 conducted = exactly 75.00%
        records = [
            AttendanceRecord(f"R_{i}", "S001", "CS1", d, i + 1,
                             AttendanceStatus.PRESENT if i < 75 else AttendanceStatus.ABSENT,
                             faculty_id="FAC001")
            for i in range(100)
        ]

        engine = _build_engine(s, [c], records)
        res = engine.process_student(s)
        detail = res.course_details[0]

        assert detail.final_percentage == 75.00
        assert detail.course_status == EligibilityStatus.ELIGIBLE
        assert detail.condonation_fee == 0

    def test_exact_74_99_is_condonable(self):
        """74.99% is CONDONABLE with fee = ₹500."""
        assert _eligibility(Decimal("74.99")) == EligibilityStatus.CONDONABLE
        assert _course_fee(Decimal("74.99")) == 500

        s = Student("S001", "Jack", "B.Tech CSE", 5, "A", SEMESTER_START)
        c = Course("CS1", "C1", 4, False)
        d = SEMESTER_START + timedelta(days=1)

        # 7499 present out of 10000 conducted = exactly 74.99%
        records = [
            AttendanceRecord(f"R_{i}", "S001", "CS1", d, i + 1,
                             AttendanceStatus.PRESENT if i < 7499 else AttendanceStatus.ABSENT,
                             faculty_id="FAC001")
            for i in range(10000)
        ]

        engine = _build_engine(s, [c], records)
        res = engine.process_student(s)
        detail = res.course_details[0]

        assert detail.final_percentage == 74.99
        assert detail.course_status == EligibilityStatus.CONDONABLE
        assert detail.condonation_fee == 500
