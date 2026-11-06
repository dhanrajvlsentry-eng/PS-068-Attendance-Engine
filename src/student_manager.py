"""
PS-068 Student Manager
=======================
Maintains the in-memory student registry and provides lookup helpers used
by the attendance calculator.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List, Optional

from src.models import Student

logger = logging.getLogger(__name__)


class StudentManager:
    """Thin wrapper around the student dictionary with query helpers."""

    def __init__(self, students: Dict[str, Student]):
        self._students: Dict[str, Student] = students
        # Build section index for fast section-based queries
        self._by_section: Dict[str, List[Student]] = {}
        for s in students.values():
            self._by_section.setdefault(s.section, []).append(s)

    # ------------------------------------------------------------------
    # Registration helpers
    # ------------------------------------------------------------------

    def register(self, student: Student) -> None:
        """Add or update a student record."""
        self._students[student.student_id] = student
        self._by_section.setdefault(student.section, [])
        if student not in self._by_section[student.section]:
            self._by_section[student.section].append(student)
        logger.debug("Registered student %s", student.student_id)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def get(self, student_id: str) -> Optional[Student]:
        return self._students.get(student_id)

    def all_students(self) -> List[Student]:
        return list(self._students.values())

    def by_section(self, section: str) -> List[Student]:
        return self._by_section.get(section.upper(), [])

    def sections(self) -> List[str]:
        return sorted(self._by_section.keys())

    def count(self) -> int:
        return len(self._students)

    # ------------------------------------------------------------------
    # Admission-date rule
    # ------------------------------------------------------------------

    def eligible_from(self, student_id: str) -> Optional[date]:
        """
        Return the earliest date from which attendance counts for this student.

        Rules:
        - Regular students: attendance counted from semester start (admission_date
          is used only to verify they were admitted; the calling layer passes the
          actual semester start date).
        - Lateral-entry students: attendance counted from their admission_date
          because they join mid-programme (semester 3 onwards).

        The attendance_calculator calls this to skip records before the
        effective start date.
        """
        s = self._students.get(student_id)
        if s is None:
            return None
        return s.admission_date  # attendance_calculator compares with semester_start

    def is_lateral(self, student_id: str) -> bool:
        s = self._students.get(student_id)
        return s.is_lateral if s else False

