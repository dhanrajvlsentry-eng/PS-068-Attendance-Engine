"""
PS-068 Attendance Calculator
=============================
Core engine: computes course-wise and overall attendance for every student
while applying all business rules.

Business Rules Implemented
--------------------------
R1  Conducted periods: a period is counted only when faculty_id is set.
R2  Holiday exclusion: records on holiday dates are ignored entirely.
R3  Working Saturday: timetable of the replaced day applies (handled upstream
    in the timetable; records already carry the right course_id / period_no).
R4  Lab weight: a lab period slot counts as 2 periods (weight = 2).
R5  LATE → PRESENT: if arrival_time is within LATE_GRACE_MINUTES of the
    slot's start_time, it counts as PRESENT; otherwise ABSENT.
R6  OD → PRESENT: only when an approved OD (or placement-event OD) exists
    for that (student, date, course).
R7  Admission-date rule:
    - Regular students: periods before semester_start are excluded.
    - Lateral-entry students: periods before their admission_date are excluded.
R8  Percentage = (effective_present / conducted_periods) × 100.
    Displayed to 2 decimal places.
R9  Eligibility threshold: >= 75 % for theory; same threshold for labs.
"""
from __future__ import annotations

import logging
from datetime import date, time, timedelta
from typing import Dict, List, Optional

from src.models import (
    AttendanceStatus,
    CourseAttendanceSummary,
    OverallAttendanceSummary,
    Student,
    Course,
)
from src.attendance_manager import AttendanceManager
from src.calendar_manager import CalendarManager
from src.course_manager import CourseManager
from src.student_manager import StudentManager

logger = logging.getLogger(__name__)

LATE_GRACE_MINUTES = 10          # R5: LATE counts as PRESENT within this window
ELIGIBILITY_THRESHOLD = 75.0     # R9


def _add_minutes(t: time, minutes: int) -> time:
    """Return a new time = t + minutes (wraps at midnight, safe for small additions)."""
    total = t.hour * 60 + t.minute + minutes
    return time(total // 60 % 24, total % 60)


class AttendanceCalculator:
    """
    Computes attendance summaries for students.
    Thread-safe as long as the underlying managers are not mutated during use.
    """

    def __init__(
        self,
        attendance_mgr:  AttendanceManager,
        calendar_mgr:    CalendarManager,
        course_mgr:      CourseManager,
        student_mgr:     StudentManager,
    ):
        self._att   = attendance_mgr
        self._cal   = calendar_mgr
        self._crs   = course_mgr
        self._stu   = student_mgr

        # Pre-build timetable start-time index:
        # {(section, day, period_no, course_id): start_time}
        # This is populated externally via set_timetable_index().
        self._slot_start: Dict[tuple, time] = {}

    def set_timetable_index(self, slots) -> None:
        """
        Pre-build a dict of slot start times keyed by
        (section, day_abbr, period_no, course_id) for LATE validation.
        """
        from src.models import TimetableSlot
        for s in slots:
            key = (s.section.upper(), s.day.upper(), s.period_no, s.course_id)
            self._slot_start[key] = s.start_time

    # ------------------------------------------------------------------
    # Core per-student calculation
    # ------------------------------------------------------------------

    def calculate_for_student(
        self,
        student: Student,
        up_to_date: Optional[date] = None,
    ) -> OverallAttendanceSummary:
        """
        Compute full attendance summary for one student.

        Parameters
        ----------
        student    : the Student object
        up_to_date : only consider records on or before this date
                     (defaults to semester_end)
        """
        cutoff = up_to_date or self._cal.semester_end

        # Admission-date rule (R7)
        # Regular students: effective start = semester_start
        # Lateral-entry   : effective start = max(semester_start, admission_date)
        if student.is_lateral:
            effective_start = max(self._cal.semester_start, student.admission_date)
        else:
            effective_start = self._cal.semester_start

        # Find all courses this student has records for
        course_ids = self._att.all_course_ids_for_student(student.student_id)

        course_summaries: List[CourseAttendanceSummary] = []
        total_conducted = 0
        total_present   = 0

        for course_id in sorted(course_ids):
            course = self._crs.get(course_id)
            if course is None:
                logger.warning("Unknown course_id %s in attendance data", course_id)
                continue

            summary = self._calculate_course(
                student, course, effective_start, cutoff
            )
            course_summaries.append(summary)
            total_conducted += summary.conducted_periods
            total_present   += summary.present_periods

        overall_pct = (
            (total_present / total_conducted * 100.0)
            if total_conducted > 0
            else 0.0
        )

        return OverallAttendanceSummary(
            student_id=student.student_id,
            student_name=student.name,
            section=student.section,
            total_conducted=total_conducted,
            total_present=total_present,
            overall_percentage=overall_pct,
            course_summaries=course_summaries,
        )

    def _calculate_course(
        self,
        student: Student,
        course:  Course,
        effective_start: date,
        cutoff: date,
    ) -> CourseAttendanceSummary:
        """
        Compute attendance for one (student, course) pair.
        """
        records = self._att.records_for_student_course(student.student_id, course.course_id)
        weight  = course.period_weight()   # 2 for lab, 1 for theory

        conducted  = 0
        present    = 0
        absent     = 0
        late_raw   = 0
        od_raw     = 0

        for rec in records:
            d = rec.record_date

            # R2: skip holidays
            if self._cal.is_holiday(d):
                continue

            # R7: skip records before effective start or after cutoff
            if d < effective_start or d > cutoff:
                continue

            # R1: skip if period was not conducted (no faculty_id)
            if not rec.faculty_id:
                continue

            # Period is conducted → add to conducted count
            conducted += weight

            status = rec.status

            # R6: OD → PRESENT (approved OD)
            if status == AttendanceStatus.OD:
                od_raw += 1
                if self._att.has_od(student.student_id, d, course.course_id):
                    present += weight
                else:
                    absent  += weight   # unapproved OD counts as absent

            # R5: LATE → PRESENT within grace window
            elif status == AttendanceStatus.LATE:
                late_raw += 1
                if self._is_within_grace(student.section, d, rec.period_no, course.course_id, rec.arrival_time):
                    present += weight
                else:
                    absent  += weight

            elif status == AttendanceStatus.PRESENT:
                present += weight

            else:  # ABSENT
                absent += weight

        pct = (present / conducted * 100.0) if conducted > 0 else 0.0

        return CourseAttendanceSummary(
            student_id=student.student_id,
            course_id=course.course_id,
            course_name=course.course_name,
            is_lab=course.is_lab,
            conducted_periods=conducted,
            present_periods=present,
            absent_periods=absent,
            late_periods=late_raw,
            od_periods=od_raw,
            percentage=pct,
            eligible=pct >= ELIGIBILITY_THRESHOLD,
        )

    def _is_within_grace(
        self,
        section: str,
        d: date,
        period_no: int,
        course_id: str,
        arrival_time: Optional[time],
    ) -> bool:
        """
        R5: LATE counts as PRESENT if arrival_time <= slot_start + LATE_GRACE_MINUTES.
        If no arrival_time is recorded (data gap), conservatively count as ABSENT.
        """
        if arrival_time is None:
            return False
        timetable_day = self._cal.timetable_day(d)
        if timetable_day is None:
            return False
        slot_key = (section.upper(), timetable_day, period_no, course_id)
        slot_start = self._slot_start.get(slot_key)
        if slot_start is None:
            # Fallback: if we cannot find the slot, treat LATE as present
            # (benefit of the doubt when timetable data is incomplete)
            logger.debug("No slot start for %s; treating LATE as PRESENT", slot_key)
            return True
        grace_cutoff = _add_minutes(slot_start, LATE_GRACE_MINUTES)
        return arrival_time <= grace_cutoff

    # ------------------------------------------------------------------
    # Batch calculation
    # ------------------------------------------------------------------

    def calculate_for_section(
        self,
        section: str,
        up_to_date: Optional[date] = None,
    ) -> List[OverallAttendanceSummary]:
        """Calculate attendance for all students in a section."""
        students = self._stu.by_section(section)
        results = []
        for student in students:
            results.append(self.calculate_for_student(student, up_to_date))
        return results

    def calculate_all(
        self,
        up_to_date: Optional[date] = None,
    ) -> List[OverallAttendanceSummary]:
        """Calculate attendance for every registered student."""
        results = []
        for student in self._stu.all_students():
            results.append(self.calculate_for_student(student, up_to_date))
        return results

    # ------------------------------------------------------------------
    # Eligibility helpers
    # ------------------------------------------------------------------

    def ineligible_students(
        self,
        up_to_date: Optional[date] = None,
    ) -> List[OverallAttendanceSummary]:
        """Return summaries for students below the eligibility threshold."""
        return [
            s for s in self.calculate_all(up_to_date)
            if not s.is_eligible
        ]

