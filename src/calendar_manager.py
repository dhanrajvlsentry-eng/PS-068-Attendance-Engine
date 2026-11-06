"""
PS-068 Calendar Manager
========================
Provides date-classification and timetable-day resolution for every date
in the 16-week academic calendar.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

from src.models import CalendarEntry, DayType

logger = logging.getLogger(__name__)

# Weekday abbreviation map (Python weekday() → three-letter key)
_WEEKDAY_ABBR = {0: "MON", 1: "TUE", 2: "WED", 3: "THU", 4: "FRI", 5: "SAT", 6: "SUN"}


class CalendarManager:
    """
    Manages the academic calendar for one semester.

    Responsibilities:
    - Classify any date as working / holiday / exam / etc.
    - Resolve which timetable day applies (important for working Saturdays).
    - Enumerate all working days in a range.
    """

    def __init__(self, calendar: Dict[date, CalendarEntry],
                 semester_start: date, semester_end: date,
                 exam_start: Optional[date] = None):
        self._calendar: Dict[date, CalendarEntry] = calendar
        self.semester_start = semester_start
        self.semester_end   = semester_end
        self.exam_start     = exam_start

        # Build a set of holiday dates for fast membership tests
        self._holidays: set = {
            d for d, e in calendar.items()
            if e.day_type == DayType.HOLIDAY
        }
        self._working_saturdays: Dict[date, str] = {
            d: e.replaces_day
            for d, e in calendar.items()
            if e.day_type == DayType.WORKING_SATURDAY
        }

    # ------------------------------------------------------------------
    # Date classification
    # ------------------------------------------------------------------

    def get_entry(self, d: date) -> Optional[CalendarEntry]:
        return self._calendar.get(d)

    def is_holiday(self, d: date) -> bool:
        return d in self._holidays

    def is_working_day(self, d: date) -> bool:
        """True if the date is a scheduled teaching day (not holiday/exam/weekend)."""
        entry = self._calendar.get(d)
        if entry is None:
            return False
        return entry.is_working()

    def timetable_day(self, d: date) -> Optional[str]:
        """
        Return the three-letter weekday key (MON … SAT) for the timetable
        to use on this date, or None if the date is not a working day.
        """
        entry = self._calendar.get(d)
        if entry is None or not entry.is_working():
            return None
        return entry.timetable_day()

    # ------------------------------------------------------------------
    # Range helpers
    # ------------------------------------------------------------------

    def working_days(self, start: Optional[date] = None,
                     end: Optional[date] = None) -> List[date]:
        """Return sorted list of all working (teaching) days in [start, end]."""
        start = start or self.semester_start
        end   = end   or self.semester_end
        result = []
        cur = start
        while cur <= end:
            if self.is_working_day(cur):
                result.append(cur)
            cur += timedelta(days=1)
        return result

    def holiday_dates(self) -> List[date]:
        return sorted(self._holidays)

    def semester_weeks(self) -> int:
        """Approximate number of weeks in the semester."""
        delta = (self.semester_end - self.semester_start).days
        return max(1, round(delta / 7))

    # ------------------------------------------------------------------
    # Freeze logic
    # ------------------------------------------------------------------

    @property
    def freeze_date(self) -> Optional[date]:
        """Attendance freezes 7 days before examination start date."""
        if self.exam_start:
            return self.exam_start - timedelta(days=7)
        return None

    def is_frozen(self, on_date: date) -> bool:
        """True if on_date is strictly after the freeze date."""
        fd = self.freeze_date
        if fd is None:
            return False
        return on_date > fd

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        total_working = len(self.working_days())
        return {
            "semester_start":   str(self.semester_start),
            "semester_end":     str(self.semester_end),
            "exam_start":       str(self.exam_start) if self.exam_start else None,
            "freeze_date":      str(self.freeze_date) if self.freeze_date else None,
            "total_working_days": total_working,
            "total_holidays":   len(self._holidays),
            "working_saturdays": len(self._working_saturdays),
        }

