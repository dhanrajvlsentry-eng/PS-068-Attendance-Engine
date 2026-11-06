"""
PS-068 Condonation Engine
==========================
Applies all condonation rules in the mandated processing order and produces
SemesterEligibilityResult objects that include course-level details, fees,
and absence fines.

Processing Order
----------------
1.  Read raw (Part-1 calculator) attendance.
2.  Validate and apply approved MEDICAL applications
    → remove medical periods from both conducted and present counts.
3.  Calculate post-medical conducted counts per course.
4.  Validate and apply duty-day applications
    (SPORTS / NCC_NSS / PLACEMENT / BEREAVEMENT)
    → add approved absent periods back as present.
5.  Evaluate duty caps (per course and overall).
6.  Calculate final course-wise and overall percentages.
7.  Determine course-level and semester-level eligibility.
8.  Calculate condonation fees.
9.  Calculate absence fines.

Business Rules
--------------
MEDICAL (C1–C5):
  C1  Remove approved medical periods from both conducted AND present counts.
  C2  Cap = floor(15% of original conducted periods) per course.
      Excess medical periods are NOT removed (capped to avoid abuse).
  C3  medical_cert_ref must be non-empty; otherwise application is REJECTED.
  C4  submitted_on must be within 7 days of end_date; otherwise REJECTED_LATE.
  C5  Fractional cap: use integer division (floor) to avoid floating-point
      boundary issues (Decimal comparison for exact boundary).

SPORTS / NCC_NSS (C6–C8):
  C6  Count approved duty days as present (duty periods = all conducted
      periods for that student on that date).
  C7  Overall duty cap: max 20 duty days per semester across all duty types.
  C8  Per-course cap: max 10% of post-medical conducted periods per course
      (floor, Decimal-safe). Excess duty periods are dropped.

PLACEMENT (C9–C10):
  C9  No condonation cap applies to placement duty.
  C10 Only final-year students qualify (semester >= 7).
  C11 Application dates must intersect at least one placement-calendar event
      for which the student is listed.

BEREAVEMENT (C12–C14):
  C12 Max 5 consecutive calendar days per application.
  C13 Once per semester only (second application auto-rejected).
  C14 submitted_on must be within 3 days of end_date; otherwise REJECTED_LATE.
  C15 No documents required (medical_cert_ref not checked).

ELIGIBILITY (E1–E5):
  E1  Course >= 75 %     → ELIGIBLE
  E2  65 % <= course < 75 % → CONDONABLE
  E3  Course < 65 %     → DETAINED
  E4  Max 2 CONDONABLE courses per semester; 3rd becomes DETAINED for that course.
  E5  3+ DETAINED courses → whole-semester DETAINED.

FEES (F1–F2):
  F1  70 % <= course < 75 %: ₹500.
  F2  65 % <= course < 70 %: ₹1,000.
  Fee applies only to CONDONABLE courses (after E4 cap).
  Max 2 fee-bearing courses.

FINES (G1–G3):
  G1  Identify unbroken stretches of full-day absences (all conducted
      periods on that working day were absent) without approved leave.
  G2  Stretches of >= 3 days: fine = ₹100 × (days − 2), starting from
      the 3rd day.
  G3  Total fine capped at ₹2,000 per semester.

Scalability
-----------
All indexes are built once; per-student processing is O(k) where k is the
number of records for that student. Integer arithmetic is used for all
percentage caps (via Decimal intermediate values) to avoid IEEE-754 drift.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Set, Tuple

from src.models import (
    ApplicationStatus,
    ApplicationType,
    AttendanceStatus,
    CondonationApplication,
    CourseAttendanceSummary,
    CourseCondonationDetail,
    EligibilityStatus,
    AbsenceFineResult,
    SemesterEligibilityResult,
    Student,
    Course,
    PlacementEvent,
)
from src.attendance_manager import AttendanceManager
from src.calendar_manager import CalendarManager
from src.course_manager import CourseManager
from src.student_manager import StudentManager
from src.attendance_calculator import AttendanceCalculator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (all monetary values in integer ₹)
# ---------------------------------------------------------------------------

MEDICAL_CAP_PCT       = Decimal("15")    # % of conducted periods
DUTY_OVERALL_CAP_DAYS = 20              # max duty days per semester
DUTY_COURSE_CAP_PCT   = Decimal("10")   # % of post-medical conducted
BEREAVEMENT_MAX_DAYS  = 5
BEREAVEMENT_SUBMIT_DAYS = 3
MEDICAL_SUBMIT_DAYS   = 7
MAX_CONDONABLE_COURSES = 2
MIN_DETAINED_FOR_SEMESTER = 3
ELIGIBLE_THRESHOLD    = Decimal("75")
CONDONABLE_THRESHOLD  = Decimal("65")
FEE_TIER1_MIN         = Decimal("70")   # 70–74.99% → ₹500
FEE_TIER1_AMOUNT      = 500
FEE_TIER2_AMOUNT      = 1000           # 65–69.99% → ₹1000
FINE_PER_DAY          = 100
FINE_MAX              = 2000
FINAL_YEAR_SEMESTER   = 7              # placement: semester >= 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _floor_pct(conducted: int, pct: Decimal) -> int:
    """floor( conducted × pct / 100 ) using Decimal to avoid fp drift."""
    if conducted <= 0:
        return 0
    result = (Decimal(conducted) * pct / Decimal("100")).to_integral_value(ROUND_DOWN)
    return int(result)


def _pct(present: int, conducted: int) -> Decimal:
    """Return Decimal percentage; 0 if conducted == 0."""
    if conducted == 0:
        return Decimal("0")
    return (Decimal(present) * Decimal("100") / Decimal(conducted)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )


def _eligibility(pct: Decimal) -> EligibilityStatus:
    if pct >= ELIGIBLE_THRESHOLD:
        return EligibilityStatus.ELIGIBLE
    elif pct >= CONDONABLE_THRESHOLD:
        return EligibilityStatus.CONDONABLE
    else:
        return EligibilityStatus.DETAINED


def _course_fee(pct: Decimal) -> int:
    if pct >= FEE_TIER1_MIN:
        return FEE_TIER1_AMOUNT
    return FEE_TIER2_AMOUNT


# ---------------------------------------------------------------------------
# Condonation Engine
# ---------------------------------------------------------------------------

class CondonationEngine:
    """
    Applies all condonation rules in the specified processing order and
    returns SemesterEligibilityResult for each processed student.

    The engine depends on:
    - A base AttendanceCalculator (Part 1) for raw course summaries.
    - The list of CondonationApplication objects (approved ones only).
    - PlacementEvent list for placement validation.
    - CalendarManager for working-day classification.
    """

    def __init__(
        self,
        calculator:       AttendanceCalculator,
        att_mgr:          AttendanceManager,
        calendar_mgr:     CalendarManager,
        course_mgr:       CourseManager,
        student_mgr:      StudentManager,
        applications:     List[CondonationApplication],
        placement_events: List[PlacementEvent],
    ):
        self._calc       = calculator
        self._att        = att_mgr
        self._cal        = calendar_mgr
        self._crs        = course_mgr
        self._stu        = student_mgr
        self._placement_events = placement_events
        self._all_applications = list(applications)

        # Pre-validate all applications and store approved ones by student
        # {student_id: [CondonationApplication]}
        self._approved_apps: Dict[str, List[CondonationApplication]] = defaultdict(list)
        # Placement: {event_date: set[student_id]}
        self._placement_index: Dict[date, Set[str]] = defaultdict(set)
        for ev in placement_events:
            for sid in ev.student_ids:
                self._placement_index[ev.event_date].add(sid)

        self._validate_and_index_applications(applications)

    @property
    def all_applications(self) -> List[CondonationApplication]:
        return self._all_applications

    # ------------------------------------------------------------------
    # Application validation
    # ------------------------------------------------------------------

    def _validate_and_index_applications(
        self, apps: List[CondonationApplication]
    ) -> None:
        """
        Validate each application and mark it REJECTED / REJECTED_LATE if it
        fails checks.  Approved applications are indexed by student_id.
        PENDING applications are skipped (not yet decided).
        """
        # Track bereavement: student → count approved this semester
        bereavement_count: Dict[str, int] = defaultdict(int)

        for app in apps:
            if app.status == ApplicationStatus.PENDING:
                continue   # not yet decided; skip
            if app.status in (ApplicationStatus.REJECTED,
                              ApplicationStatus.REJECTED_LATE):
                continue   # already rejected; keep as-is

            # app.status == APPROVED → re-validate business rules
            rejection_reason = self._check_application(app, bereavement_count)
            if rejection_reason:
                app.status = rejection_reason
                logger.info(
                    "Application %s set to %s during validation",
                    app.application_id, rejection_reason
                )
                continue

            # Valid approved application
            self._approved_apps[app.student_id].append(app)
            if app.reason == ApplicationType.BEREAVEMENT:
                bereavement_count[app.student_id] += 1

    def _check_application(
        self, app: CondonationApplication,
        bereavement_count: Dict[str, int]
    ) -> Optional[ApplicationStatus]:
        """
        Return the rejection status if invalid, else None (valid).
        """
        # C0: Freeze Rule - Applications submitted after the freeze date (exam_start - 7 days)
        # are rejected as REJECTED_FROZEN. Submission on the freeze date is allowed.
        if app.submitted_on and self._cal.is_frozen(app.submitted_on):
            logger.info("Application %s submitted after freeze date; REJECTED_FROZEN", app.application_id)
            return ApplicationStatus.REJECTED_FROZEN

        reason = app.reason

        # --- MEDICAL ---
        if reason == ApplicationType.MEDICAL:
            # C3: requires medical certificate reference
            if not (app.medical_cert_ref and app.medical_cert_ref.strip()):
                logger.warning("MEDICAL app %s missing cert ref", app.application_id)
                return ApplicationStatus.REJECTED
            # C4: must be submitted within 7 days of end_date
            if app.submitted_on:
                deadline = app.end_date + timedelta(days=MEDICAL_SUBMIT_DAYS)
                if app.submitted_on > deadline:
                    return ApplicationStatus.REJECTED_LATE
            # else: no submitted_on → accept (backward compat)

        # --- BEREAVEMENT ---
        elif reason == ApplicationType.BEREAVEMENT:
            # C12: max 5 consecutive calendar days
            span = (app.end_date - app.start_date).days + 1
            if span > BEREAVEMENT_MAX_DAYS:
                logger.warning(
                    "BEREAVEMENT app %s spans %d days (max %d)",
                    app.application_id, span, BEREAVEMENT_MAX_DAYS
                )
                return ApplicationStatus.REJECTED
            # C13: once per semester
            if bereavement_count.get(app.student_id, 0) >= 1:
                return ApplicationStatus.REJECTED
            # C14: submitted within 3 days
            if app.submitted_on:
                deadline = app.end_date + timedelta(days=BEREAVEMENT_SUBMIT_DAYS)
                if app.submitted_on > deadline:
                    return ApplicationStatus.REJECTED_LATE

        # --- PLACEMENT ---
        elif reason == ApplicationType.PLACEMENT:
            # C10: final-year only
            student = self._stu.get(app.student_id)
            if student and student.semester < FINAL_YEAR_SEMESTER:
                logger.warning(
                    "PLACEMENT app %s: student not in final year (sem=%d)",
                    app.application_id, student.semester
                )
                return ApplicationStatus.REJECTED
            # C11: dates must match a placement event
            matched = False
            cur = app.start_date
            while cur <= app.end_date:
                if app.student_id in self._placement_index.get(cur, set()):
                    matched = True
                    break
                cur += timedelta(days=1)
            if not matched:
                logger.warning(
                    "PLACEMENT app %s: no matching placement event found",
                    app.application_id
                )
                return ApplicationStatus.REJECTED

        # SPORTS / NCC_NSS: no extra validation beyond approval
        return None   # valid

    # ------------------------------------------------------------------
    # Core per-student processing
    # ------------------------------------------------------------------

    def process_student(
        self,
        student: Student,
        up_to_date: Optional[date] = None,
    ) -> SemesterEligibilityResult:
        """
        Apply all condonation rules for one student and return the full result.
        """
        cutoff = up_to_date or self._cal.semester_end

        # Step 1: Get raw summaries from Part-1 calculator
        raw_summary = self._calc.calculate_for_student(student, up_to_date)
        raw_by_course: Dict[str, CourseAttendanceSummary] = {
            cs.course_id: cs for cs in raw_summary.course_summaries
        }

        # Collect approved apps for this student
        apps = self._approved_apps.get(student.student_id, [])

        # Step 2: Apply MEDICAL removal per course
        # medical_removed[course_id] = integer conducted periods removed
        # medical_present_removed[course_id] = integer present periods removed
        medical_removed: Dict[str, int] = defaultdict(int)
        medical_present_removed: Dict[str, int] = defaultdict(int)

        for app in apps:
            if app.reason != ApplicationType.MEDICAL:
                continue
            cid = app.course_id
            raw_cs = raw_by_course.get(cid)
            if raw_cs is None:
                continue
            # Count conducted and attended periods in the medical date range
            med_periods, med_attended = self._count_medical_in_range(
                student, cid, app.start_date, app.end_date, cutoff
            )
            # C2: apply 15% cap on original conducted periods
            cap = _floor_pct(raw_cs.conducted_periods, MEDICAL_CAP_PCT)
            already_removed = medical_removed[cid]
            can_remove = max(0, cap - already_removed)
            to_remove = min(med_periods, can_remove)
            # Scale or cap attended removal proportionally if capped
            if med_periods > 0:
                att_to_remove = min(med_attended, to_remove)
            else:
                att_to_remove = 0

            medical_removed[cid] += to_remove
            medical_present_removed[cid] += att_to_remove

        # Step 3: Compute post-medical counts per course
        post_conducted: Dict[str, int] = {}
        post_present:   Dict[str, int] = {}
        for cid, raw_cs in raw_by_course.items():
            rem_c = medical_removed.get(cid, 0)
            rem_p = medical_present_removed.get(cid, 0)
            pc = max(0, raw_cs.conducted_periods - rem_c)
            pp = max(0, raw_cs.present_periods   - rem_p)
            post_conducted[cid] = pc
            post_present[cid]   = pp

        # Step 4+5: Apply duty-day additions (SPORTS / NCC_NSS / PLACEMENT / BEREAVEMENT)
        # Duty is added to present count (up to caps).
        # Overall duty cap: max 20 duty days per semester for SPORTS / NCC_NSS.
        duty_days_used = 0          # overall sports/ncc counter
        duty_added: Dict[str, int] = defaultdict(int)   # per course additions

        # Separate apps by duty type (preserve processing order)
        duty_apps = [
            a for a in apps
            if a.reason in (
                ApplicationType.SPORTS,
                ApplicationType.NCC_NSS,
                ApplicationType.PLACEMENT,
                ApplicationType.BEREAVEMENT,
            )
        ]

        for app in duty_apps:
            cid = app.course_id
            is_sports_ncc = app.reason in (ApplicationType.SPORTS, ApplicationType.NCC_NSS)

            # Count absent periods in the duty range for this course
            duty_periods, duty_days = self._count_absent_in_range(
                student, cid, app.start_date, app.end_date, cutoff
            )
            if duty_periods == 0:
                continue

            # Overall 20-day cap for SPORTS / NCC_NSS
            if is_sports_ncc:
                days_available = DUTY_OVERALL_CAP_DAYS - duty_days_used
                if days_available <= 0:
                    continue
                # Clip duty_days to remaining cap
                if duty_days > days_available:
                    duty_periods, duty_days = self._count_absent_in_range(
                        student, cid, app.start_date, app.end_date, cutoff,
                        max_days=days_available
                    )

            # Per-course 10% cap on post-medical conducted periods for SPORTS / NCC_NSS
            if is_sports_ncc:
                pc = post_conducted.get(cid, 0)
                course_cap = _floor_pct(pc, DUTY_COURSE_CAP_PCT)
                already_added = duty_added.get(cid, 0)
                can_add = max(0, course_cap - already_added)
                duty_periods = min(duty_periods, can_add)
                if duty_periods == 0:
                    continue

            # Apply the duty
            duty_added[cid] += duty_periods
            if is_sports_ncc:
                duty_days_used += duty_days

        # Step 6: Final per-course counts and percentages
        course_details: List[CourseCondonationDetail] = []
        total_final_conducted = 0
        total_final_present   = 0

        for cid, raw_cs in sorted(raw_by_course.items()):
            course = self._crs.get(cid)
            if course is None:
                continue

            med_rem    = medical_removed.get(cid, 0)
            duty_add   = duty_added.get(cid, 0)
            pc         = post_conducted[cid]
            pp         = post_present[cid]
            final_c    = pc
            final_p    = min(pp + duty_add, final_c)   # present cannot exceed conducted
            raw_pct    = _pct(raw_cs.present_periods, raw_cs.conducted_periods)
            post_med_pct = _pct(pp, pc)
            final_pct  = _pct(final_p, final_c)

            # Determine which app types were applied for this course
            applied_types = []
            if med_rem > 0:
                applied_types.append(ApplicationType.MEDICAL)
            for app in apps:
                if app.course_id == cid and app.reason != ApplicationType.MEDICAL:
                    if duty_added.get(cid, 0) > 0:
                        applied_types.append(app.reason)

            course_details.append(CourseCondonationDetail(
                course_id=cid,
                course_name=raw_cs.course_name,
                is_lab=raw_cs.is_lab,
                raw_conducted=raw_cs.conducted_periods,
                raw_present=raw_cs.present_periods,
                raw_percentage=float(raw_pct),
                medical_periods_removed=med_rem,
                post_medical_conducted=pc,
                post_medical_present=pp,
                post_medical_percentage=float(post_med_pct),
                duty_periods_added=duty_add,
                final_conducted=final_c,
                final_present=final_p,
                final_percentage=float(final_pct),
                course_status=_eligibility(final_pct),   # will be revised below
                condonation_fee=0,                        # will be set below
                applied_types=applied_types,
            ))
            total_final_conducted += final_c
            total_final_present   += final_p

        # Step 7: Eligibility — apply E4 (max 2 condonable) and E5 (3+ detained)
        condonable_count = 0
        detained_count   = 0

        # Sort: process ELIGIBLE first, then CONDONABLE by final_pct desc, then DETAINED
        course_details.sort(key=lambda cd: (-cd.final_percentage))

        for cd in course_details:
            status = _eligibility(Decimal(str(cd.final_percentage)))
            if status == EligibilityStatus.ELIGIBLE:
                cd.course_status = EligibilityStatus.ELIGIBLE
            elif status == EligibilityStatus.CONDONABLE:
                if condonable_count < MAX_CONDONABLE_COURSES:
                    cd.course_status = EligibilityStatus.CONDONABLE
                    condonable_count += 1
                else:
                    # E4: 3rd condonable course becomes DETAINED
                    cd.course_status = EligibilityStatus.DETAINED
                    detained_count += 1
            else:
                cd.course_status = EligibilityStatus.DETAINED
                detained_count += 1

        # Step 8: Fees
        fee_courses = 0
        for cd in course_details:
            if cd.course_status == EligibilityStatus.CONDONABLE and fee_courses < MAX_CONDONABLE_COURSES:
                cd.condonation_fee = _course_fee(Decimal(str(cd.final_percentage)))
                fee_courses += 1

        overall_pct = _pct(total_final_present, total_final_conducted)

        # Semester status:
        # 3 or more detained courses result in whole-semester DETAINED status.
        # Overall < 65% also results in DETAINED.
        # Otherwise, if any courses are CONDONABLE or DETAINED (1-2), or overall < 75%, semester is CONDONABLE.
        # Otherwise ELIGIBLE.
        if detained_count >= MIN_DETAINED_FOR_SEMESTER or overall_pct < CONDONABLE_THRESHOLD:
            semester_status = EligibilityStatus.DETAINED
        elif condonable_count > 0 or detained_count > 0 or overall_pct < ELIGIBLE_THRESHOLD:
            semester_status = EligibilityStatus.CONDONABLE
        else:
            semester_status = EligibilityStatus.ELIGIBLE

        total_cond_fee = sum(cd.condonation_fee for cd in course_details)

        # Step 9: Absence fine
        fine_result = self._calculate_absence_fine(student, cutoff)

        return SemesterEligibilityResult(
            student_id=student.student_id,
            student_name=student.name,
            section=student.section,
            course_details=course_details,
            eligible_courses=sum(1 for cd in course_details
                                 if cd.course_status == EligibilityStatus.ELIGIBLE),
            condonable_courses=condonable_count,
            detained_courses=detained_count,
            total_conducted=total_final_conducted,
            total_present=total_final_present,
            overall_percentage=float(overall_pct),
            semester_status=semester_status,
            total_condonation_fee=total_cond_fee,
            absence_fine=fine_result,
        )

    # ------------------------------------------------------------------
    # Date-range period counters
    # ------------------------------------------------------------------

    def _count_medical_in_range(
        self,
        student:    Student,
        course_id:  str,
        start:      date,
        end:        date,
        cutoff:     date,
    ) -> Tuple[int, int]:
        """
        Count (conducted_weighted, attended_weighted) periods for (student, course)
        in [start, end].
        Conducted = periods with faculty_id set, not holiday.
        Attended  = conducted periods where student was PRESENT, approved OD,
                    or LATE within grace.
        """
        course = self._crs.get(course_id)
        weight = course.period_weight() if course else 1
        conducted_total = 0
        attended_total  = 0
        records = self._att.records_for_student_course(student.student_id, course_id)
        seen_periods: Set[Tuple[date, int]] = set()

        for rec in records:
            d = rec.record_date
            if d < start or d > end or d > cutoff:
                continue
            if self._cal.is_holiday(d):
                continue
            if not rec.faculty_id:
                continue
            key = (d, rec.period_no)
            if key not in seen_periods:
                seen_periods.add(key)
                conducted_total += weight
                status = rec.status
                if status == AttendanceStatus.PRESENT:
                    attended_total += weight
                elif status == AttendanceStatus.OD:
                    if self._att.has_od(student.student_id, d, course_id):
                        attended_total += weight
                elif status == AttendanceStatus.LATE:
                    if self._calc._is_within_grace(student.section, d, rec.period_no, course_id, rec.arrival_time):
                        attended_total += weight

        return conducted_total, attended_total

    def _count_absent_in_range(
        self,
        student:    Student,
        course_id:  str,
        start:      date,
        end:        date,
        cutoff:     date,
        max_days:   int = 9999,
    ) -> Tuple[int, int]:
        """
        Count absent weighted periods and calendar days (working days with
        at least one absent period) in [start, end] for (student, course).
        Returns (periods, days).  max_days limits the calendar days counted.
        """
        course = self._crs.get(course_id)
        weight = course.period_weight() if course else 1
        total_periods = 0
        days_counted  = 0
        days_seen: Set[date] = set()
        records = self._att.records_for_student_course(student.student_id, course_id)
        for rec in records:
            d = rec.record_date
            if d < start or d > end or d > cutoff:
                continue
            if self._cal.is_holiday(d):
                continue
            if not rec.faculty_id:
                continue
            if rec.status not in (AttendanceStatus.ABSENT,):
                continue
            if d not in days_seen:
                if days_counted >= max_days:
                    break
                days_seen.add(d)
                days_counted += 1
            total_periods += weight
        return total_periods, days_counted

    # ------------------------------------------------------------------
    # Absence fine calculation (G1–G3)
    # ------------------------------------------------------------------

    def _calculate_absence_fine(
        self,
        student: Student,
        cutoff:  date,
    ) -> AbsenceFineResult:
        """
        G1: Find unbroken stretches of full-day absences without approved leave.
        G2: Fine = ₹100 × (days − 2) for stretches >= 3 days.
        G3: Cap at ₹2,000.

        A "full-day absence" means every conducted period on that working day
        is ABSENT (not PRESENT, LATE within grace, or approved OD).
        """
        # Build an ordered list of working days in the semester
        working_days = self._cal.working_days(end=cutoff)

        # For efficiency, pre-determine which working days the student was
        # fully absent (across ALL courses on that day).
        fully_absent_days: Set[date] = set()

        # Get all courses for this student
        course_ids = self._att.all_course_ids_for_student(student.student_id)
        if not course_ids:
            return AbsenceFineResult(student_id=student.student_id,
                                      streak_details=[], total_fine=0)

        # Build per-day status: day → (conducted_total, absent_total)
        day_stats: Dict[date, Tuple[int, int]] = {}

        for cid in course_ids:
            course = self._crs.get(cid)
            weight = course.period_weight() if course else 1
            for rec in self._att.records_for_student_course(student.student_id, cid):
                d = rec.record_date
                if d > cutoff or self._cal.is_holiday(d):
                    continue
                if not self._cal.is_working_day(d):
                    continue
                if not rec.faculty_id:
                    continue
                if d not in day_stats:
                    day_stats[d] = [0, 0]
                day_stats[d][0] += weight   # conducted
                # Check effective absence
                status = rec.status
                is_absent = False
                if status == AttendanceStatus.ABSENT:
                    is_absent = True
                elif status == AttendanceStatus.OD:
                    # Only absent if no approved OD
                    if not self._att.has_od(student.student_id, d, cid):
                        is_absent = True
                elif status == AttendanceStatus.LATE:
                    # Treat as absent for fine purposes if beyond grace
                    # (conservative — if no arrival_time, treat as absent)
                    is_absent = True   # fine calc uses full-day check below
                # For fine purposes, full-day = all conducted are absent
                if is_absent:
                    day_stats[d][1] += weight   # absent

        # Determine fully-absent days (all conducted periods were absent)
        for d, (cond, absent) in day_stats.items():
            if cond > 0 and cond == absent:
                # Check no approved OD for ANY course on this day
                has_any_od = any(
                    self._att.has_od(student.student_id, d, cid)
                    for cid in course_ids
                )
                if not has_any_od:
                    fully_absent_days.add(d)

        # Build streaks from working_days list
        streak_details = []
        total_fine = 0
        streak_start: Optional[date] = None
        streak_end:   Optional[date] = None
        streak_len = 0

        def _close_streak():
            nonlocal total_fine
            if streak_len >= 3:
                fine = FINE_PER_DAY * (streak_len - 2)
                remaining = max(0, FINE_MAX - total_fine)
                fine = min(fine, remaining)
                total_fine += fine
                streak_details.append({
                    "start": streak_start,
                    "end":   streak_end,
                    "days":  streak_len,
                    "fine":  fine,
                })

        for d in working_days:
            if d in fully_absent_days:
                # If an official holiday occurred between the previous absent day and today,
                # the unbroken absence stretch is interrupted.
                if streak_end is not None:
                    intervening_days = (d - streak_end).days
                    has_holiday = any(
                        self._cal.is_holiday(streak_end + timedelta(days=step))
                        for step in range(1, intervening_days)
                    )
                    if has_holiday:
                        _close_streak()
                        streak_start = None
                        streak_end   = None
                        streak_len   = 0

                if streak_start is None:
                    streak_start = d
                streak_end = d
                streak_len += 1
            else:
                _close_streak()
                streak_start = None
                streak_end   = None
                streak_len   = 0

        _close_streak()   # close any open streak at end of semester

        return AbsenceFineResult(
            student_id=student.student_id,
            streak_details=streak_details,
            total_fine=min(total_fine, FINE_MAX),
        )

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def process_all(
        self,
        up_to_date: Optional[date] = None,
    ) -> List[SemesterEligibilityResult]:
        results = []
        for student in self._stu.all_students():
            results.append(self.process_student(student, up_to_date))
        return results

    def process_section(
        self,
        section: str,
        up_to_date: Optional[date] = None,
    ) -> List[SemesterEligibilityResult]:
        results = []
        for student in self._stu.by_section(section):
            results.append(self.process_student(student, up_to_date))
        return results

