"""
PS-068 Course Manager
=====================
Maintains the course registry and provides section-aware lookups.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.models import Course

logger = logging.getLogger(__name__)


class CourseManager:
    """Registry for all courses with section-aware filtering."""

    def __init__(self, courses: Dict[str, Course]):
        self._courses: Dict[str, Course] = courses
        # Index by section: section="" means the course is offered to all sections
        self._by_section: Dict[str, List[Course]] = {}
        for c in courses.values():
            self._by_section.setdefault(c.section, []).append(c)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, course: Course) -> None:
        self._courses[course.course_id] = course
        self._by_section.setdefault(course.section, []).append(course)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, course_id: str) -> Optional[Course]:
        return self._courses.get(course_id)

    def all_courses(self) -> List[Course]:
        return list(self._courses.values())

    def for_section(self, section: str) -> List[Course]:
        """Return courses offered to a specific section plus any global courses."""
        section = section.upper()
        specific = self._by_section.get(section, [])
        global_  = self._by_section.get("", [])
        # Deduplicate by course_id
        seen = set()
        result = []
        for c in specific + global_:
            if c.course_id not in seen:
                seen.add(c.course_id)
                result.append(c)
        return result

    def is_lab(self, course_id: str) -> bool:
        c = self._courses.get(course_id)
        return c.is_lab if c else False

    def period_weight(self, course_id: str) -> int:
        """Returns 2 for lab courses, 1 for theory courses."""
        c = self._courses.get(course_id)
        return c.period_weight() if c else 1

    def count(self) -> int:
        return len(self._courses)

