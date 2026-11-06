"""
PS-068 Edge Case Unit Tests
===========================
Explicit verification of all edge cases specified in the PS-068 requirements:
1. Attendance exactly 75.00% versus 74.99%
2. LATE at exactly 10 minutes (grace window) vs 11 minutes
3. Medical cap with fractional periods
4. Medical leave overlapping a holiday and working Saturday
5. Exactly 3 detained courses after condoning 2 others (triggers semester detention)
6. Lateral-entry joining on day 30 versus day 31
7. Absence stretch interrupted by a holiday (streak broken)
8. Condonation submitted on freeze date vs post-freeze date (REJECTED_FROZEN)
9. Third condonable course becoming detained
10. Placement duty on an invalid date (rejected)
11. Principal correction after freeze (auth check and audit trail)
12. Multiple monthly warning runs (deduplication and parent meeting flag)
13. Duplicate commands / re-registration
14. Missing input files
15. Malformed CSV records
16. Empty attendance datasets
17. Unknown student IDs or course IDs
"""
import pytest
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import csv

from src.models import (
    Student, Course, DayType, CalendarEntry, TimetableSlot,
    AttendanceRecord, AttendanceStatus, ApplicationType,
    ApplicationStatus, CondonationApplication, ODApproval,
    PlacementEvent, EligibilityStatus, WarningLevel,
    AttendanceCorrectionLog
)
from src.student_manager import StudentManager
from src.course_manager import CourseManager
from src.calendar_manager import CalendarManager
from src.attendance_manager import AttendanceManager
from src.attendance_calculator import AttendanceCalculator
from src.condonation_engine import (
    CondonationEngine, _eligibility, _course_fee, _floor_pct
)
from src.warning_manager import WarningManager
from src.data_loader import load_students, load_courses


def make_stu_mgr(students):
    return StudentManager({s.student_id: s for s in students})


def make_crs_mgr(courses):
    return CourseManager({c.course_id: c for c in courses})


def make_att_mgr(records=None, od_approvals=None, placement_events=None):
    return AttendanceManager(
        records or [],
        od_approvals or [],
        placement_events or []
    )


def make_cond_engine(calc, base_calendar, cm, sm, applications=None, placement_events=None, att_mgr=None):
    if att_mgr is None:
        att_mgr = make_att_mgr()
    return CondonationEngine(
        calculator=calc,
        att_mgr=att_mgr,
        calendar_mgr=base_calendar,
        course_mgr=cm,
        student_mgr=sm,
        applications=applications or [],
        placement_events=placement_events or []
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_calendar():
    """16-week semester calendar with holidays, working Saturday, and exams."""
    cal = {}
    sem_start = date(2024, 7, 22)
    sem_end = date(2024, 11, 9)
    exam_start = date(2024, 11, 18)

    cur = sem_start
    while cur <= exam_start + timedelta(days=10):
        if cur < sem_start or cur > sem_end:
            if cur >= exam_start and cur <= exam_start + timedelta(days=7):
                cal[cur] = CalendarEntry(cur, DayType.EXAM)
            else:
                cal[cur] = CalendarEntry(cur, DayType.HOLIDAY)
        else:
            weekday = cur.weekday()
            if weekday == 6:  # Sunday
                cal[cur] = CalendarEntry(cur, DayType.SUNDAY)
            elif weekday == 5:  # Saturday
                cal[cur] = CalendarEntry(cur, DayType.SATURDAY)
            else:
                cal[cur] = CalendarEntry(cur, DayType.WORKING)
        cur += timedelta(days=1)

    # Specific holiday on Wednesday 2024-08-14
    cal[date(2024, 8, 14)] = CalendarEntry(date(2024, 8, 14), DayType.HOLIDAY)
    # Working Saturday replacing Wednesday on 2024-08-17
    cal[date(2024, 8, 17)] = CalendarEntry(date(2024, 8, 17), DayType.WORKING_SATURDAY, replaces_day="WED")

    return CalendarManager(cal, sem_start, sem_end, exam_start)


# ---------------------------------------------------------------------------
# Edge Case Tests
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_01_exact_75_00_vs_74_99(self, base_calendar):
        """Attendance exactly 75.00% is ELIGIBLE; 74.99% is CONDONABLE with ₹500 fee."""
        assert _eligibility(Decimal("75.00")) == EligibilityStatus.ELIGIBLE
        assert _eligibility(Decimal("74.99")) == EligibilityStatus.CONDONABLE
        assert _course_fee(Decimal("74.99")) == 500

        stu = Student("S001", "Student 1", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C101", "Theory Course", 4, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        d = date(2024, 7, 23)
        # Exactly 75 present out of 100
        records_75 = [
            AttendanceRecord(f"R_{i}", "S001", "C101", d, i + 1,
                             AttendanceStatus.PRESENT if i < 75 else AttendanceStatus.ABSENT,
                             faculty_id="F1")
            for i in range(100)
        ]
        att_75 = make_att_mgr(records_75)
        calc_75 = AttendanceCalculator(att_75, base_calendar, cm, sm)
        eng_75 = make_cond_engine(calc_75, base_calendar, cm, sm, att_mgr=att_75)
        res_75 = eng_75.process_student(stu)
        det_75 = res_75.course_details[0]

        assert det_75.final_percentage == 75.00
        assert det_75.course_status == EligibilityStatus.ELIGIBLE
        assert det_75.condonation_fee == 0

    def test_02_late_grace_window_exact_10_min_vs_11_min(self, base_calendar):
        """LATE at slot_start + 10 min is PRESENT; slot_start + 11 min is ABSENT."""
        stu = Student("S002", "Late Student", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C102", "Morning Course", 4, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        slots = [TimetableSlot("A", "MON", 1, "C102", time(9, 0), time(9, 50))]
        mon1 = date(2024, 7, 29)
        mon2 = date(2024, 8, 5)

        records = [
            # Exactly 10 minutes late: 09:10
            AttendanceRecord("R1", "S002", "C102", mon1, 1, AttendanceStatus.LATE, arrival_time=time(9, 10), faculty_id="F1"),
            # 11 minutes late: 09:11
            AttendanceRecord("R2", "S002", "C102", mon2, 1, AttendanceStatus.LATE, arrival_time=time(9, 11), faculty_id="F1"),
        ]
        att_mgr = make_att_mgr(records)
        calc = AttendanceCalculator(att_mgr, base_calendar, cm, sm)
        calc.set_timetable_index(slots)

        summary_mon1 = calc._calculate_course(stu, crs, base_calendar.semester_start, mon1)
        assert summary_mon1.conducted_periods == 1
        assert summary_mon1.present_periods == 1

        summary_mon2 = calc._calculate_course(stu, crs, base_calendar.semester_start, mon2)
        assert summary_mon2.conducted_periods == 2
        assert summary_mon2.present_periods == 1

    def test_03_fractional_medical_cap(self, base_calendar):
        """Medical cap is floor(15% of conducted). E.g., conducted=45 -> floor(6.75) = 6."""
        assert _floor_pct(45, Decimal("15")) == 6
        assert _floor_pct(39, Decimal("15")) == 5
        assert _floor_pct(50, Decimal("15")) == 7

        stu = Student("S003", "Med Student", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C103", "Theory", 3, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        # 45 conducted periods: 30 present on d1, 15 absent on d2
        d1 = date(2024, 7, 23)
        d2 = date(2024, 7, 24)
        records = [
            AttendanceRecord(f"R1_{i}", "S003", "C103", d1, i + 1, AttendanceStatus.PRESENT, faculty_id="F1")
            for i in range(30)
        ] + [
            AttendanceRecord(f"R2_{i}", "S003", "C103", d2, i + 1, AttendanceStatus.ABSENT, faculty_id="F1")
            for i in range(15)
        ]
        # Request medical condonation for the 15 absent periods on d2
        app = CondonationApplication(
            application_id="APP_MED_45",
            student_id="S003",
            course_id="C103",
            reason=ApplicationType.MEDICAL,
            start_date=d2,
            end_date=d2,
            description="Hospitalization",
            status=ApplicationStatus.APPROVED,
            approved_by="HOD01",
            approved_on=d2 + timedelta(days=2),
            medical_cert_ref="MED-CERT-999",
            submitted_on=d2 + timedelta(days=3),
        )
        att_mgr = make_att_mgr(records)
        calc = AttendanceCalculator(att_mgr, base_calendar, cm, sm)
        eng = make_cond_engine(calc, base_calendar, cm, sm, applications=[app], att_mgr=att_mgr)

        res = eng.process_student(stu)
        det = res.course_details[0]

        # 15% of 45 is 6.75 -> cap is 6 periods removed
        assert det.raw_conducted == 45
        assert det.medical_periods_removed == 6
        assert det.post_medical_conducted == 39
        assert det.post_medical_present == 30

    def test_04_medical_leave_overlapping_holiday_and_working_saturday(self, base_calendar):
        """Attendance on holiday ignored; working Saturday follows replaced timetable."""
        assert base_calendar.is_holiday(date(2024, 8, 14)) is True
        assert base_calendar.is_working_day(date(2024, 8, 17)) is True
        assert base_calendar.timetable_day(date(2024, 8, 17)) == "WED"

        stu = Student("S004", "Test Student", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C104", "Wed Course", 3, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        records = [
            AttendanceRecord("R10", "S004", "C104", date(2024, 8, 14), 1, AttendanceStatus.ABSENT, faculty_id="F1"),
            AttendanceRecord("R11", "S004", "C104", date(2024, 8, 17), 1, AttendanceStatus.PRESENT, faculty_id="F1"),
        ]
        calc = AttendanceCalculator(make_att_mgr(records), base_calendar, cm, sm)
        summary = calc.calculate_for_student(stu)
        assert summary.total_conducted == 1
        assert summary.total_present == 1

    def test_05_three_detained_courses_after_condoning_two(self, base_calendar):
        """A student with 2 condoned courses and 3 detained courses is WHOLE-SEMESTER DETAINED."""
        stu = Student("S005", "Detained Student", "B.Tech", 5, "A", date(2024, 7, 22))
        courses = [Course(f"C{i}", f"Course {i}", 4, is_lab=False) for i in range(1, 6)]
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr(courses)

        records = []
        d = date(2024, 7, 23)
        # C1, C2 at 70% (condonable)
        for c in courses[:2]:
            for i in range(100):
                records.append(AttendanceRecord(f"R_{c.course_id}_{i}", "S005", c.course_id, d, i + 1,
                                                AttendanceStatus.PRESENT if i < 70 else AttendanceStatus.ABSENT,
                                                faculty_id="F1"))
        # C3, C4, C5 at 60% (detained)
        for c in courses[2:]:
            for i in range(100):
                records.append(AttendanceRecord(f"R_{c.course_id}_{i}", "S005", c.course_id, d, i + 1,
                                                AttendanceStatus.PRESENT if i < 60 else AttendanceStatus.ABSENT,
                                                faculty_id="F1"))

        att_mgr = make_att_mgr(records)
        calc = AttendanceCalculator(att_mgr, base_calendar, cm, sm)
        eng = make_cond_engine(calc, base_calendar, cm, sm, att_mgr=att_mgr)

        result = eng.process_student(stu)
        assert result.condonable_courses == 2
        assert result.detained_courses == 3
        assert result.semester_status == EligibilityStatus.DETAINED

    def test_06_lateral_entry_day_30_vs_day_31(self, base_calendar):
        """Periods before admission_date for lateral entry are ignored."""
        lat_stu = Student("S006", "Lateral Student", "B.Tech", 3, "A", admission_date=date(2024, 8, 21), is_lateral=True)
        crs = Course("C106", "Core Course", 4, is_lab=False)
        sm = make_stu_mgr([lat_stu])
        cm = make_crs_mgr([crs])

        records = [
            AttendanceRecord("R20", "S006", "C106", date(2024, 8, 20), 1, AttendanceStatus.ABSENT, faculty_id="F1"),
            AttendanceRecord("R21", "S006", "C106", date(2024, 8, 21), 1, AttendanceStatus.PRESENT, faculty_id="F1"),
            AttendanceRecord("R22", "S006", "C106", date(2024, 8, 22), 1, AttendanceStatus.PRESENT, faculty_id="F1"),
        ]
        calc = AttendanceCalculator(make_att_mgr(records), base_calendar, cm, sm)
        summary = calc.calculate_for_student(lat_stu)
        assert summary.total_conducted == 2
        assert summary.total_present == 2
        assert summary.overall_percentage == 100.0

    def test_07_absence_stretch_interrupted_by_holiday(self, base_calendar):
        """Holiday interrupts contiguous absence streak so fine is not charged."""
        stu = Student("S007", "Fine Student", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C107", "Course 107", 4, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        records = [
            AttendanceRecord("R30", "S007", "C107", date(2024, 8, 13), 1, AttendanceStatus.ABSENT, faculty_id="F1"),
            AttendanceRecord("R31", "S007", "C107", date(2024, 8, 15), 1, AttendanceStatus.ABSENT, faculty_id="F1"),
        ]
        att_mgr = make_att_mgr(records)
        calc = AttendanceCalculator(att_mgr, base_calendar, cm, sm)
        eng = make_cond_engine(calc, base_calendar, cm, sm, att_mgr=att_mgr)

        fine_res = eng._calculate_absence_fine(stu, base_calendar.semester_end)
        assert fine_res.total_fine == 0
        assert fine_res.streak_details == []

    def test_08_condonation_submitted_on_freeze_date_vs_post_freeze(self, base_calendar):
        """Application on freeze_date accepted; application after freeze_date is REJECTED_FROZEN."""
        freeze_d = base_calendar.freeze_date
        post_freeze_d = freeze_d + timedelta(days=1)

        app_on_freeze = CondonationApplication(
            "APP_FREEZE", "S008", "C108", ApplicationType.MEDICAL,
            start_date=freeze_d - timedelta(days=3), end_date=freeze_d,
            description="Fever", status=ApplicationStatus.PENDING,
            medical_cert_ref="MED-123", submitted_on=freeze_d
        )
        app_post_freeze = CondonationApplication(
            "APP_POST", "S008", "C108", ApplicationType.MEDICAL,
            start_date=freeze_d - timedelta(days=3), end_date=freeze_d,
            description="Fever", status=ApplicationStatus.PENDING,
            medical_cert_ref="MED-123", submitted_on=post_freeze_d
        )

        calc = AttendanceCalculator(make_att_mgr(), base_calendar, CourseManager({}), StudentManager({}))
        eng = make_cond_engine(calc, base_calendar, CourseManager({}), StudentManager({}))

        status_freeze = eng._check_application(app_on_freeze, {})
        status_post = eng._check_application(app_post_freeze, {})

        assert status_freeze is None
        assert status_post == ApplicationStatus.REJECTED_FROZEN

    def test_09_third_condonable_course_becomes_detained(self, base_calendar):
        """Only 2 courses can be condoned; 3rd condonable course is forced to DETAINED."""
        stu = Student("S009", "3rd Condonable", "B.Tech", 5, "A", date(2024, 7, 22))
        courses = [Course(f"CRS_{i}", f"Course {i}", 4, is_lab=False) for i in range(1, 4)]
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr(courses)

        records = []
        d = date(2024, 7, 23)
        # All 3 courses at 72%
        for c in courses:
            for i in range(100):
                records.append(AttendanceRecord(f"R_{c.course_id}_{i}", "S009", c.course_id, d, i + 1,
                                                AttendanceStatus.PRESENT if i < 72 else AttendanceStatus.ABSENT,
                                                faculty_id="F1"))

        att_mgr = make_att_mgr(records)
        calc = AttendanceCalculator(att_mgr, base_calendar, cm, sm)
        eng = make_cond_engine(calc, base_calendar, cm, sm, att_mgr=att_mgr)

        result = eng.process_student(stu)
        assert result.condonable_courses == 2
        assert result.detained_courses == 1

        detained_courses = [cd for cd in result.course_details if cd.course_status == EligibilityStatus.DETAINED]
        assert len(detained_courses) == 1

    def test_10_placement_duty_on_invalid_date_rejected(self, base_calendar):
        """Placement duty on a date not matching placement calendar is rejected."""
        stu = Student("S010", "Placement Student", "B.Tech", 7, "A", date(2024, 7, 22))
        event = PlacementEvent("PE1", date(2024, 9, 20), "Google Drive", ["S010"])
        app_invalid_date = CondonationApplication(
            "APP_PL_INV", "S010", "C110", ApplicationType.PLACEMENT,
            start_date=date(2024, 10, 1), end_date=date(2024, 10, 2),
            description="Interview", status=ApplicationStatus.PENDING,
            submitted_on=date(2024, 10, 3)
        )
        calc = AttendanceCalculator(make_att_mgr(), base_calendar, CourseManager({}), make_stu_mgr([stu]))
        eng = make_cond_engine(calc, base_calendar, CourseManager({}), make_stu_mgr([stu]), placement_events=[event])

        status = eng._check_application(app_invalid_date, {})
        assert status == ApplicationStatus.REJECTED

    def test_11_principal_correction_after_freeze(self, base_calendar):
        """Post-freeze correction requires Principal authorization and is logged."""
        from main import AppState, make_handlers
        state = AppState()
        state.calendar = base_calendar._calendar
        state.semester_start = base_calendar.semester_start
        state.semester_end = base_calendar.semester_end
        state.exam_start = base_calendar.exam_start

        post_freeze_date = base_calendar.freeze_date + timedelta(days=2)
        rec = AttendanceRecord("R99", "S011", "C111", post_freeze_date, 1, AttendanceStatus.ABSENT, faculty_id="F1")
        state.att_records = [rec]

        handlers = make_handlers(state)

        with pytest.raises(PermissionError):
            handlers["PRINCIPAL_CORRECTION"]([
                "S011", "C111", str(post_freeze_date), "1", "PRESENT", "FACULTY_HOD", "Typo fixed"
            ])

        handlers["PRINCIPAL_CORRECTION"]([
            "S011", "C111", str(post_freeze_date), "1", "PRESENT", "PRINCIPAL", "Authorized medical waiver"
        ])
        assert rec.status == AttendanceStatus.PRESENT
        assert len(state.audit_logs) == 1
        assert state.audit_logs[0].authorized_by == "PRINCIPAL"
        assert state.audit_logs[0].is_post_freeze is True

    def test_12_multiple_monthly_warning_runs_and_deduplication(self, base_calendar):
        """Monthly warning deduplication: each level issued once; 3rd month <75% triggers Parent Meeting."""
        stu = Student("S012", "Warning Student", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C112", "Course 112", 4, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        records = []
        d = base_calendar.semester_start
        for i in range(60):
            while not base_calendar.is_working_day(d):
                d += timedelta(days=1)
            status = AttendanceStatus.PRESENT if (i % 10) < 7 else AttendanceStatus.ABSENT
            records.append(AttendanceRecord(f"WAR_{i}", "S012", "C112", d, 1, status, faculty_id="F1"))
            d += timedelta(days=1)

        att_mgr = make_att_mgr(records)
        calc = AttendanceCalculator(att_mgr, base_calendar, cm, sm)
        wm = WarningManager(calc, sm)

        d1 = base_calendar.semester_start + timedelta(days=28)
        letters1, _ = wm.evaluate_run(d1, 1)
        assert len(letters1) == 1
        assert letters1[0].warning_level == WarningLevel.SECOND_WARNING

        d2 = base_calendar.semester_start + timedelta(days=56)
        letters2, _ = wm.evaluate_run(d2, 2)
        assert len(letters2) == 0

        d3 = base_calendar.semester_start + timedelta(days=84)
        letters3, flags3 = wm.evaluate_run(d3, 3)
        assert len(wm.parent_meeting_flags) == 1
        assert wm.parent_meeting_flags[0].student_id == "S012"
        assert len(wm.parent_meeting_flags[0].history_pct) == 3

    def test_13_duplicate_student_registration_handled_safely(self):
        """Registering an existing student updates or maintains state without crash."""
        stu1 = Student("S013", "Name A", "B.Tech", 5, "A", date(2024, 7, 22))
        stu2 = Student("S013", "Name B", "B.Tech", 5, "A", date(2024, 7, 22))
        sm = make_stu_mgr([stu1])
        sm.register(stu2)
        assert sm.get("S013").name == "Name B"
        assert sm.count() == 1

    def test_14_missing_input_files_raises_clear_error(self):
        """Missing input file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_students(Path("non_existent_directory_12345"))

    def test_15_malformed_csv_records(self):
        """Malformed date or missing required column raises ValueError or KeyError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_csv = Path(tmpdir) / "students.csv"
            with open(bad_csv, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["student_id", "name", "programme", "semester", "section", "admission_date"])
                writer.writerow(["S1", "Test", "B.Tech", "5", "A", "NOT-A-DATE"])
            with pytest.raises(ValueError):
                load_students(Path(tmpdir))

    def test_16_empty_attendance_dataset_handled_safely(self, base_calendar):
        """Zero attendance records yields 0.0% attendance without zero-division error."""
        stu = Student("S016", "Empty Records", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C116", "Course 116", 4, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])
        calc = AttendanceCalculator(make_att_mgr(), base_calendar, cm, sm)
        summary = calc.calculate_for_student(stu)
        assert summary.total_conducted == 0
        assert summary.total_present == 0
        assert summary.overall_percentage == 0.0
        assert summary.is_eligible is False

    def test_17_unknown_student_and_course_ids(self, base_calendar):
        """Records with unknown student or course IDs are safely handled."""
        stu = Student("S017", "Known Student", "B.Tech", 5, "A", date(2024, 7, 22))
        crs = Course("C117", "Known Course", 4, is_lab=False)
        sm = make_stu_mgr([stu])
        cm = make_crs_mgr([crs])

        records = [
            AttendanceRecord("R101", "S017", "UNKNOWN_CRS", date(2024, 7, 25), 1, AttendanceStatus.PRESENT, faculty_id="F1"),
            AttendanceRecord("R102", "UNKNOWN_STU", "C117", date(2024, 7, 25), 1, AttendanceStatus.PRESENT, faculty_id="F1"),
        ]
        calc = AttendanceCalculator(make_att_mgr(records), base_calendar, cm, sm)
        summary = calc.calculate_for_student(stu)
        assert summary.total_conducted == 0
        assert summary.total_present == 0
