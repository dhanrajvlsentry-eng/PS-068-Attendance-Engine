"""
PS-068 Attendance Manager
==========================
Builds and owns the in-memory attendance index used by the calculator.

Index structure (all built once at construction time):
  _records_by_student_course : {(student_id, course_id): [AttendanceRecord]}
  _records_by_date_course    : {(date, course_id): [AttendanceRecord]}
  _records_by_student_date   : {(student_id, date): [AttendanceRecord]}
  _od_index                  : {(student_id, date): set[course_id]}
                                 course_id "*" means all courses on that date.

This avoids O(n) full-list scans during per-student or per-course queries.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import Dict, List, Set, Tuple

from src.models import AttendanceRecord, ODApproval, PlacementEvent

logger = logging.getLogger(__name__)

# Type aliases
_StudentCourseKey = Tuple[str, str]       # (student_id, course_id)
_DateCourseKey    = Tuple[date, str]      # (record_date, course_id)
_StudentDateKey   = Tuple[str, date]      # (student_id, record_date)


class AttendanceManager:
    """
    Indexes raw attendance records and OD approvals for fast retrieval.
    """

    def __init__(
        self,
        records:   List[AttendanceRecord],
        od_approvals: List[ODApproval],
        placement_events: List[PlacementEvent],
    ):
        # Primary record storage
        self._all_records: List[AttendanceRecord] = records

        # --- Index 1: per (student, course) ---
        self._by_student_course: Dict[_StudentCourseKey, List[AttendanceRecord]] = defaultdict(list)
        # --- Index 2: per (date, course) – used to count conducted periods ---
        self._by_date_course: Dict[_DateCourseKey, List[AttendanceRecord]] = defaultdict(list)
        # --- Index 3: per (student, date) – used for absence-streak detection ---
        self._by_student_date: Dict[_StudentDateKey, List[AttendanceRecord]] = defaultdict(list)

        for rec in records:
            key_sc = (rec.student_id, rec.course_id)
            key_dc = (rec.record_date, rec.course_id)
            key_sd = (rec.student_id, rec.record_date)
            self._by_student_course[key_sc].append(rec)
            self._by_date_course[key_dc].append(rec)
            self._by_student_date[key_sd].append(rec)

        # --- Index 4: OD approvals ---
        # {(student_id, date): set[course_id]}   ("*" = all courses)
        self._od_index: Dict[Tuple[str, date], Set[str]] = defaultdict(set)
        for od in od_approvals:
            self._od_index[(od.student_id, od.od_date)].add(od.course_id)

        # Placement events also generate OD for all courses on that date
        for event in placement_events:
            for sid in event.student_ids:
                self._od_index[(sid, event.event_date)].add("*")

        logger.info(
            "AttendanceManager: indexed %d records, %d OD keys",
            len(records),
            len(self._od_index),
        )

    # ------------------------------------------------------------------
    # OD helpers
    # ------------------------------------------------------------------

    def has_od(self, student_id: str, record_date: date, course_id: str) -> bool:
        """Return True if the student has an approved OD for this date/course."""
        key = (student_id, record_date)
        courses = self._od_index.get(key, set())
        return "*" in courses or course_id in courses

    # ------------------------------------------------------------------
    # Record retrieval
    # ------------------------------------------------------------------

    def records_for_student_course(
        self, student_id: str, course_id: str
    ) -> List[AttendanceRecord]:
        """All records for a specific (student, course) pair."""
        return self._by_student_course.get((student_id, course_id), [])

    def records_for_date_course(
        self, record_date: date, course_id: str
    ) -> List[AttendanceRecord]:
        """All student records for a specific (date, course) pair."""
        return self._by_date_course.get((record_date, course_id), [])

    def records_for_student_date(
        self, student_id: str, record_date: date
    ) -> List[AttendanceRecord]:
        """All records for a student on a specific date (across all courses)."""
        return self._by_student_date.get((student_id, record_date), [])

    def all_course_ids_for_student(self, student_id: str) -> Set[str]:
        """Return the set of course IDs this student has any attendance record for."""
        return {
            course_id
            for (sid, course_id) in self._by_student_course
            if sid == student_id
        }

    def all_student_ids_for_course(self, course_id: str) -> Set[str]:
        """Return the set of student IDs with any record for this course."""
        return {
            sid
            for (sid, cid) in self._by_student_course
            if cid == course_id
        }

    def all_dates_for_student(self, student_id: str) -> List[date]:
        """Return sorted list of distinct dates with records for this student."""
        return sorted({
            d for (sid, d) in self._by_student_date
            if sid == student_id
        })

    # ------------------------------------------------------------------
    # Conducted-period detection
    # ------------------------------------------------------------------

    def periods_conducted(self, course_id: str, record_date: date) -> int:
        """
        Count how many distinct periods were conducted for a course on a date.
        A period is 'conducted' when at least one record for that
        (date, course, period_no) has a non-null faculty_id.
        """
        records = self._by_date_course.get((record_date, course_id), [])
        conducted_periods: Set[int] = set()
        for rec in records:
            if rec.faculty_id:  # period was actually conducted
                conducted_periods.add(rec.period_no)
        return len(conducted_periods)

    def total_records(self) -> int:
        return len(self._all_records)

