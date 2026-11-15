"""
Tests for Data Loader compatibility with data_v2 schema.
Verifies column mappings, section resolution, attendance records,
applications, and placement drives.
"""
import pytest
from pathlib import Path
from decimal import Decimal

from src.data_loader import (
    load_all,
    load_students,
    load_courses,
    load_academic_calendar,
    load_timetable,
    load_attendance,
    load_od_approvals,
    load_placement_calendar,
    load_condonation_applications,
)
from src.models import (
    AttendanceStatus,
    ApplicationType,
    ApplicationStatus,
    DayType,
)


@pytest.fixture
def data_v2_path():
    p = Path("data_v2")
    if not p.exists():
        pytest.skip("data_v2 directory not found")
    return p


def test_load_students_data_v2(data_v2_path):
    students = load_students(data_v2_path)
    assert len(students) == 96

    # Test student mapping: register_no -> student_id
    s1 = students["4AT24CS001"]
    assert s1.student_id == "4AT24CS001"
    assert s1.name == "Abhishek Khan"
    assert s1.section == "SEC-CS5A"
    assert s1.programme == "CSE"
    assert s1.semester == 5
    assert not s1.is_lateral
    assert s1.parent_mobile == "6399869879"

    # Lateral entry student
    s_lat = students["4AT25EC002"]
    assert s_lat.is_lateral is True
    assert s_lat.programme == "ECE"
    assert s_lat.semester == 3


def test_load_courses_data_v2(data_v2_path):
    courses = load_courses(data_v2_path)
    assert len(courses) == 24

    c_theory = courses["CS501"]
    assert c_theory.course_id == "CS501"
    assert c_theory.course_name == "Software Engineering"
    assert c_theory.section == "SEC-CS5A"
    assert c_theory.weekly_periods == 2
    assert not c_theory.is_lab
    assert c_theory.faculty_id == "FAC-001"

    c_lab = courses["CS506L"]
    assert c_lab.is_lab is True
    assert c_lab.period_weight() == 2


def test_load_calendar_data_v2(data_v2_path):
    cal = load_academic_calendar(data_v2_path)
    assert len(cal) >= 84

    # Holiday check
    from datetime import date
    indep = cal[date(2026, 8, 15)]
    assert indep.day_type == DayType.HOLIDAY

    # Working Saturday check
    sat = cal[date(2026, 9, 19)]
    assert sat.day_type == DayType.WORKING_SATURDAY
    assert sat.replaces_day == "MON"

    # Exam date from semester.csv
    exam_d = date(2026, 11, 30)
    assert exam_d in cal
    assert cal[exam_d].day_type == DayType.EXAM


def test_load_timetable_data_v2(data_v2_path):
    slots = load_timetable(data_v2_path)
    assert len(slots) == 40
    slot1 = slots[0]
    assert slot1.section == "SEC-CS5A"
    assert slot1.course_id == "CS501"


def test_load_attendance_data_v2(data_v2_path):
    records = load_attendance(data_v2_path)
    assert len(records) == 13835

    # Check arrival_minute parsing on LATE record
    late_recs = [r for r in records if r.status == AttendanceStatus.LATE and r.arrival_minute is not None]
    assert len(late_recs) > 0
    assert any(r.arrival_minute == 10 for r in late_recs)
    assert any(r.arrival_minute == 11 for r in late_recs)


def test_load_condonation_applications_data_v2(data_v2_path):
    apps = load_condonation_applications(data_v2_path)
    assert len(apps) == 60

    app1 = next(a for a in apps if a.application_id == "APP-001")
    assert app1.student_id == "4AT24CS011"
    assert app1.reason == ApplicationType.SPORTS
    assert app1.medical_cert_ref == "DOC-38183"


def test_load_placement_drives_data_v2(data_v2_path):
    students = load_students(data_v2_path)
    drives = load_placement_calendar(data_v2_path, students=students)
    assert len(drives) == 4

    # MECH final year students should be listed
    drive1 = drives[0]
    assert drive1.company == "Tata Elxsi"
    assert len(drive1.student_ids) == 24  # 24 MECH final year students
    assert "4AT23ME001" in drive1.student_ids


def test_load_all_data_v2(data_v2_path):
    bundle = load_all(data_v2_path)
    assert len(bundle["students"]) == 96
    assert len(bundle["courses"]) == 24
    assert len(bundle["calendar"]) >= 85
    assert len(bundle["timetable"]) == 40
    assert len(bundle["attendance"]) == 13835
    assert len(bundle["placement"]) == 4
    assert len(bundle["condonation"]) == 60


def test_report_generator_section_detection_and_skipping(data_v2_path, tmp_path):
    from src.student_manager import StudentManager
    from src.course_manager import CourseManager
    from src.calendar_manager import CalendarManager
    from src.attendance_manager import AttendanceManager
    from src.attendance_calculator import AttendanceCalculator
    from src.condonation_engine import CondonationEngine
    from src.warning_manager import WarningManager
    from src.report_generator import ReportGenerator

    bundle = load_all(data_v2_path)
    cal_dates = [d for d in bundle["calendar"].keys()]
    sem_start = min(cal_dates)
    sem_end = max(cal_dates)
    cal_mgr = CalendarManager(
        bundle["calendar"],
        sem_start,
        sem_end,
    )
    stu_mgr = StudentManager(bundle["students"])
    course_mgr = CourseManager(bundle["courses"])
    att_mgr = AttendanceManager(bundle["attendance"], [], bundle["placement"])
    calc = AttendanceCalculator(att_mgr, cal_mgr, course_mgr, stu_mgr)
    cond = CondonationEngine(calc, att_mgr, cal_mgr, course_mgr, stu_mgr, bundle["condonation"], bundle["placement"])
    warn = WarningManager(calc, stu_mgr)
    report_gen = ReportGenerator(calc, cond, warn, [])

    # Verify detected available sections
    avail = report_gen.available_sections()
    assert set(avail) == {"SEC-CS5A", "SEC-IS5A", "SEC-EC3A", "SEC-ME7A"}

    # Verify has_section behavior
    assert report_gen.has_section("A") is True
    assert report_gen.has_section("SEC-CS5A") is True
    assert report_gen.has_section("B") is False
    assert report_gen.has_section("SEC-CS5B") is False
    assert report_gen.has_section(None) is True

    # Verify skipping missing Section B without generating file
    out_b = tmp_path / "attendance_register_secB.csv"
    count_b = report_gen.generate_attendance_register(out_b, section="B")
    assert count_b == 0
    assert not out_b.exists()

    # Verify Section A succeeds and generates records
    out_a = tmp_path / "attendance_register_secA.csv"
    count_a = report_gen.generate_attendance_register(out_a, section="A")
    assert count_a > 0
    assert out_a.exists()


