"""
PS-068 Data Models
==================
Dataclass definitions for all domain entities used by the attendance engine.
All business rules (period weighting, OD validation, etc.) are enforced in
the manager / calculator layers, not here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AttendanceStatus(str, Enum):
    PRESENT  = "PRESENT"
    ABSENT   = "ABSENT"
    LATE     = "LATE"
    OD       = "OD"


class DayType(str, Enum):
    WORKING   = "WORKING"
    HOLIDAY   = "HOLIDAY"
    SATURDAY  = "SATURDAY"   # regular Saturday (non-working unless overridden)
    SUNDAY    = "SUNDAY"
    EXAM      = "EXAM"
    WORKING_SATURDAY = "WORKING_SATURDAY"  # Saturday that follows another day's timetable


class ApplicationType(str, Enum):
    MEDICAL     = "MEDICAL"
    SPORTS      = "SPORTS"
    NCC_NSS     = "NCC_NSS"
    PLACEMENT   = "PLACEMENT"
    BEREAVEMENT = "BEREAVEMENT"


class ApplicationStatus(str, Enum):
    PENDING         = "PENDING"
    APPROVED        = "APPROVED"
    REJECTED        = "REJECTED"
    REJECTED_LATE   = "REJECTED_LATE"     # submitted beyond the deadline
    REJECTED_FROZEN = "REJECTED_FROZEN"   # submitted after attendance freeze date


class WarningLevel(str, Enum):
    FIRST_WARNING  = "FIRST_WARNING"   # below 80%
    SECOND_WARNING = "SECOND_WARNING"  # below 75% (with parent copy)


class EligibilityStatus(str, Enum):
    ELIGIBLE   = "ELIGIBLE"      # overall >= 75 %
    CONDONABLE = "CONDONABLE"    # 65 % <= overall < 75 %, <= 2 condonable courses
    DETAINED   = "DETAINED"      # < 65 %, or > 2 condonable courses, or >= 3 detained


# ---------------------------------------------------------------------------
# Domain Entities
# ---------------------------------------------------------------------------

@dataclass
class Student:
    """Represents a registered student."""
    student_id:     str
    name:           str
    programme:      str          # e.g. "B.Tech CSE"
    semester:       int
    section:        str          # e.g. "A", "B", "C", "D"
    admission_date: date
    is_lateral:     bool = False  # lateral-entry students joined in semester 3

    def __post_init__(self):
        if isinstance(self.admission_date, str):
            from datetime import date as _date
            self.admission_date = _date.fromisoformat(self.admission_date)

    def __hash__(self):
        return hash(self.student_id)

    def __eq__(self, other):
        return isinstance(other, Student) and self.student_id == other.student_id


@dataclass
class Course:
    """Represents a registered course."""
    course_id:          str
    course_name:        str
    weekly_periods:     int       # periods per week (theory: 3-4, lab: 2 slots = 4 periods)
    is_lab:             bool = False
    section:            str = ""  # section this course is assigned to (can be empty for all)

    def period_weight(self) -> int:
        """Lab sessions count as 2 periods each slot."""
        return 2 if self.is_lab else 1

    def __hash__(self):
        return hash(self.course_id)

    def __eq__(self, other):
        return isinstance(other, Course) and self.course_id == other.course_id


@dataclass
class CalendarEntry:
    """One day in the academic calendar."""
    calendar_date:  date
    day_type:       DayType
    replaces_day:   Optional[str] = None   # for WORKING_SATURDAY: "MON", "TUE", …

    def is_working(self) -> bool:
        return self.day_type in (DayType.WORKING, DayType.WORKING_SATURDAY)

    def timetable_day(self) -> str:
        """Return the weekday key to look up in the timetable."""
        if self.day_type == DayType.WORKING_SATURDAY and self.replaces_day:
            return self.replaces_day.upper()
        return self.calendar_date.strftime("%a").upper()   # MON, TUE, …


@dataclass
class TimetableSlot:
    """One period slot in the weekly timetable."""
    section:    str
    day:        str        # MON | TUE | WED | THU | FRI | SAT
    period_no:  int        # 1-based
    course_id:  str
    start_time: time
    end_time:   time


@dataclass
class AttendanceRecord:
    """Raw attendance record for one student in one timetable slot on one date."""
    record_id:      str
    student_id:     str
    course_id:      str
    record_date:    date
    period_no:      int
    status:         AttendanceStatus
    arrival_time:   Optional[time] = None   # used for LATE validation
    faculty_id:     Optional[str]  = None   # None → period not conducted

    def __post_init__(self):
        if isinstance(self.status, str):
            self.status = AttendanceStatus(self.status)
        if isinstance(self.record_date, str):
            self.record_date = date.fromisoformat(self.record_date)
        if isinstance(self.arrival_time, str) and self.arrival_time:
            h, m = map(int, self.arrival_time.split(":"))
            self.arrival_time = time(h, m)


@dataclass
class ODApproval:
    """An approved OD (On-Duty) entry that makes an absence count as present."""
    od_id:       str
    student_id:  str
    course_id:   str           # "*" means all courses for that date
    od_date:     date
    reason:      str
    approved_by: str

    def __post_init__(self):
        if isinstance(self.od_date, str):
            self.od_date = date.fromisoformat(self.od_date)


@dataclass
class PlacementEvent:
    """A placement drive that generates OD for participating students."""
    event_id:    str
    event_date:  date
    company:     str
    student_ids: list = field(default_factory=list)   # students attending

    def __post_init__(self):
        if isinstance(self.event_date, str):
            self.event_date = date.fromisoformat(self.event_date)


@dataclass
class CondonationApplication:
    """A student's application for attendance condonation."""
    application_id:  str
    student_id:      str
    course_id:       str
    reason:          ApplicationType
    start_date:      date
    end_date:        date
    description:     str
    status:          ApplicationStatus = ApplicationStatus.PENDING
    approved_by:     Optional[str]     = None
    approved_on:     Optional[date]    = None
    medical_cert_ref: Optional[str]   = None   # required for MEDICAL applications
    submitted_on:    Optional[date]    = None   # date the application was filed

    def __post_init__(self):
        for attr in ("start_date", "end_date"):
            val = getattr(self, attr)
            if isinstance(val, str):
                setattr(self, attr, date.fromisoformat(val))
        if isinstance(self.reason, str):
            self.reason = ApplicationType(self.reason)
        if isinstance(self.status, str):
            self.status = ApplicationStatus(self.status)
        if isinstance(self.approved_on, str) and self.approved_on:
            self.approved_on = date.fromisoformat(self.approved_on)
        if isinstance(self.submitted_on, str) and self.submitted_on:
            self.submitted_on = date.fromisoformat(self.submitted_on)


# ---------------------------------------------------------------------------
# Computed result containers (returned by the calculator layer)
# ---------------------------------------------------------------------------

@dataclass
class CourseAttendanceSummary:
    """Attendance summary for one student in one course."""
    student_id:          str
    course_id:           str
    course_name:         str
    is_lab:              bool
    conducted_periods:   int     # periods conducted (faculty present)
    present_periods:     int     # periods student was effectively present
    absent_periods:      int
    late_periods:        int     # raw LATE records (some may count as present)
    od_periods:          int     # raw OD records
    percentage:          float   # present_periods / conducted_periods * 100
    eligible:            bool    # percentage >= 75 %

    @property
    def display_percentage(self) -> str:
        return f"{self.percentage:.2f}"


@dataclass
class OverallAttendanceSummary:
    """Aggregate attendance summary across all courses for one student."""
    student_id:              str
    student_name:            str
    section:                 str
    total_conducted:         int
    total_present:           int
    overall_percentage:      float
    course_summaries:        list   # list[CourseAttendanceSummary]
    condonation_applied:     bool  = False
    eligible_after_cond:     bool  = False

    @property
    def display_percentage(self) -> str:
        return f"{self.overall_percentage:.2f}"

    @property
    def is_eligible(self) -> bool:
        return self.overall_percentage >= 75.0


# ---------------------------------------------------------------------------
# Condonation result containers (produced by CondonationEngine)
# ---------------------------------------------------------------------------

@dataclass
class CourseCondonationDetail:
    """
    Per-course outcome after applying all condonation types in order.
    All period counts are weighted (lab = 2× per slot).
    """
    course_id:                  str
    course_name:                str
    is_lab:                     bool

    # --- Raw counts (Part 1 calculator output) ---
    raw_conducted:              int
    raw_present:                int
    raw_percentage:             float

    # --- After MEDICAL removal ---
    medical_periods_removed:    int   # periods removed from both counts
    post_medical_conducted:     int
    post_medical_present:       int
    post_medical_percentage:    float

    # --- After duty-day additions (SPORTS / NCC / PLACEMENT / BEREAVEMENT) ---
    duty_periods_added:         int   # extra present periods from duty
    final_conducted:            int
    final_present:              int
    final_percentage:           float

    # --- Course eligibility ---
    course_status:              EligibilityStatus   # ELIGIBLE / CONDONABLE / DETAINED
    condonation_fee:            int   # ₹ fee for this course (0 if eligible)
    applied_types:              list  # list[ApplicationType] – which types were applied

    @property
    def display_raw_pct(self) -> str:
        return f"{self.raw_percentage:.2f}"

    @property
    def display_final_pct(self) -> str:
        return f"{self.final_percentage:.2f}"


@dataclass
class AbsenceFineResult:
    """Fine levied for unbroken absence streaks."""
    student_id:          str
    streak_details:      list   # list[dict] with keys: start, end, days, fine
    total_fine:          int    # capped at ₹2,000


@dataclass
class SemesterEligibilityResult:
    """
    Final semester-level eligibility decision after all condonation processing.
    """
    student_id:              str
    student_name:            str
    section:                 str

    # Course-level breakdown
    course_details:          list   # list[CourseCondonationDetail]

    # Semester-level counts
    eligible_courses:        int    # courses >= 75 %
    condonable_courses:      int    # courses 65–74.99 %, up to 2 allowed
    detained_courses:        int    # courses < 65 %

    # Overall (aggregate across courses)
    total_conducted:         int
    total_present:           int
    overall_percentage:      float

    # Final verdict
    semester_status:         EligibilityStatus

    # Fees and fines
    total_condonation_fee:   int    # sum of per-course fees
    absence_fine:            AbsenceFineResult

    @property
    def display_overall_pct(self) -> str:
        return f"{self.overall_percentage:.2f}"


@dataclass
class WarningLetter:
    """Record of an issued attendance warning letter."""
    letter_id:               str
    student_id:              str
    student_name:            str
    section:                 str
    run_date:                date
    month_no:                int
    attendance_pct:          float
    warning_level:           WarningLevel
    parent_copy_required:    bool = False
    parent_meeting_flag:     bool = False
    letter_text:             str = ""


@dataclass
class ParentMeetingFlag:
    """Flag raised when a student is below 75% for 3 consecutive warning runs."""
    student_id:              str
    student_name:            str
    section:                 str
    flagged_date:            date
    month_3_attendance_pct:  float
    history_pct:             list = field(default_factory=list)  # list of pct for months 1, 2, 3
    reason:                  str = "Attendance below 75% for 3 consecutive monthly evaluations"


@dataclass
class AttendanceCorrectionLog:
    """Audit log entry for an attendance modification (especially post-freeze)."""
    log_id:                  str
    timestamp:               str
    student_id:              str
    course_id:               str
    record_date:             date
    period_no:               int
    old_status:              Optional[str]
    new_status:              str
    authorized_by:           str
    reason:                  str
    is_post_freeze:          bool = False


