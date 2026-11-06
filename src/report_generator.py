"""
PS-068 Report Generator
========================
Generates all 11 standard project reports with configurable filters for:
  - student
  - section
  - course
  - date / cut-off
  - semester
"""
from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.models import (
    ApplicationStatus,
    AttendanceCorrectionLog,
    Course,
    CourseAttendanceSummary,
    EligibilityStatus,
    ParentMeetingFlag,
    SemesterEligibilityResult,
    Student,
    WarningLetter,
)
from src.attendance_calculator import AttendanceCalculator
from src.condonation_engine import CondonationEngine
from src.warning_manager import WarningManager

logger = logging.getLogger(__name__)


class ReportGenerator:
    """
    Coordinates and renders all 11 reports with multi-criteria filtering.
    """

    def __init__(
        self,
        calculator: AttendanceCalculator,
        cond_engine: CondonationEngine,
        warning_mgr: WarningManager,
        audit_logs: List[AttendanceCorrectionLog],
    ):
        self._calc = calculator
        self._cond = cond_engine
        self._warn = warning_mgr
        self._audit_logs = audit_logs

    # ------------------------------------------------------------------
    # Filter helper
    # ------------------------------------------------------------------

    def _filter_students(
        self,
        students: List[Student],
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        semester: Optional[int] = None,
    ) -> List[Student]:
        res = students
        if student_id:
            res = [s for s in res if s.student_id.upper() == student_id.upper()]
        if section:
            res = [s for s in res if s.section.upper() == section.upper()]
        if semester is not None:
            res = [s for s in res if s.semester == semester]
        return res

    # ------------------------------------------------------------------
    # 1. Attendance Register
    # ------------------------------------------------------------------

    def generate_attendance_register(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        course_id: Optional[str] = None,
        up_to_date: Optional[date] = None,
        semester: Optional[int] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section, semester)
        student_ids = {s.student_id for s in students}

        rows_written = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "record_date",
                "period_no", "course_id", "status", "arrival_time", "faculty_id"
            ])
            for sid in sorted(student_ids):
                s = self._calc._stu.get(sid)
                target_courses = [course_id] if course_id else sorted(self._calc._att.all_course_ids_for_student(sid))
                for cid in target_courses:
                    for rec in self._calc._att.records_for_student_course(sid, cid):
                        if up_to_date and rec.record_date > up_to_date:
                            continue
                        writer.writerow([
                            s.student_id, s.name, s.section, rec.record_date,
                            rec.period_no, rec.course_id, rec.status.value,
                            rec.arrival_time or "", rec.faculty_id or ""
                        ])
                        rows_written += 1
        return rows_written

    # ------------------------------------------------------------------
    # 2. Course-wise Attendance Percentages
    # ------------------------------------------------------------------

    def generate_course_wise_report(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        course_id: Optional[str] = None,
        up_to_date: Optional[date] = None,
        semester: Optional[int] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section, semester)

        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "course_id", "course_name",
                "is_lab", "conducted_periods", "present_periods", "absent_periods",
                "percentage", "eligible"
            ])
            for s in sorted(students, key=lambda x: x.student_id):
                summary = self._calc.calculate_for_student(s, up_to_date)
                for cs in sorted(summary.course_summaries, key=lambda c: c.course_id):
                    if course_id and cs.course_id.upper() != course_id.upper():
                        continue
                    writer.writerow([
                        s.student_id, s.name, s.section, cs.course_id, cs.course_name,
                        "YES" if cs.is_lab else "NO", cs.conducted_periods, cs.present_periods,
                        cs.absent_periods, f"{cs.percentage:.2f}", "YES" if cs.eligible else "NO"
                    ])
                    count += 1
        return count

    # ------------------------------------------------------------------
    # 3. Overall Attendance Percentages
    # ------------------------------------------------------------------

    def generate_overall_attendance_report(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        up_to_date: Optional[date] = None,
        semester: Optional[int] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section, semester)

        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "semester",
                "total_conducted", "total_present", "overall_percentage", "eligible"
            ])
            for s in sorted(students, key=lambda x: x.student_id):
                summary = self._calc.calculate_for_student(s, up_to_date)
                writer.writerow([
                    s.student_id, s.name, s.section, s.semester,
                    summary.total_conducted, summary.total_present,
                    f"{summary.overall_percentage:.2f}",
                    "YES" if summary.is_eligible else "NO"
                ])
                count += 1
        return count

    # ------------------------------------------------------------------
    # 4. Eligibility List (Final Post-Condonation)
    # ------------------------------------------------------------------

    def generate_eligibility_list(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        up_to_date: Optional[date] = None,
        semester: Optional[int] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section, semester)

        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "overall_pct",
                "semester_status", "eligible_courses", "condonable_courses",
                "detained_courses", "total_condonation_fee", "absence_fine"
            ])
            for s in sorted(students, key=lambda x: x.student_id):
                res = self._cond.process_student(s, up_to_date)
                writer.writerow([
                    res.student_id, res.student_name, res.section,
                    f"{res.overall_percentage:.2f}", res.semester_status.value,
                    res.eligible_courses, res.condonable_courses, res.detained_courses,
                    res.total_condonation_fee, res.absence_fine.total_fine
                ])
                count += 1
        return count

    # ------------------------------------------------------------------
    # 5. Shortage List (<75%)
    # ------------------------------------------------------------------

    def generate_shortage_list(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        course_id: Optional[str] = None,
        up_to_date: Optional[date] = None,
        semester: Optional[int] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section, semester)

        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "course_id", "course_name",
                "percentage", "shortage_pct", "status"
            ])
            for s in sorted(students, key=lambda x: x.student_id):
                res = self._cond.process_student(s, up_to_date)
                for cd in res.course_details:
                    if course_id and cd.course_id.upper() != course_id.upper():
                        continue
                    if cd.final_percentage < 75.0:
                        shortage = 75.0 - cd.final_percentage
                        writer.writerow([
                            s.student_id, s.name, s.section, cd.course_id, cd.course_name,
                            f"{cd.final_percentage:.2f}", f"{shortage:.2f}", cd.course_status.value
                        ])
                        count += 1
        return count

    # ------------------------------------------------------------------
    # 6. Condonation Application Status Report
    # ------------------------------------------------------------------

    def generate_condonation_status_report(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "application_id", "student_id", "student_name", "course_id",
                "reason", "start_date", "end_date", "medical_cert_ref",
                "submitted_on", "status", "approved_by", "approved_on"
            ])
            for app in self._cond.all_applications:
                s = self._cond._stu.get(app.student_id)
                if student_id and app.student_id.upper() != student_id.upper():
                    continue
                if section and s and s.section.upper() != section.upper():
                    continue
                if status_filter and app.status.value.upper() != status_filter.upper():
                    continue
                name = s.name if s else "Unknown"
                writer.writerow([
                    app.application_id,
                    app.student_id,
                    name,
                    app.course_id,
                    app.reason.value,
                    app.start_date.isoformat(),
                    app.end_date.isoformat(),
                    app.medical_cert_ref or "",
                    app.submitted_on.isoformat() if app.submitted_on else "",
                    app.status.value,
                    app.approved_by or "",
                    app.approved_on.isoformat() if app.approved_on else "",
                ])
                count += 1
        return count

    # ------------------------------------------------------------------
    # 7. Condonation Fee Report
    # ------------------------------------------------------------------

    def generate_fee_report(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        up_to_date: Optional[date] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section)

        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "course_id",
                "final_pct", "course_fee", "total_fee"
            ])
            for s in sorted(students, key=lambda x: x.student_id):
                res = self._cond.process_student(s, up_to_date)
                if res.total_condonation_fee > 0:
                    for cd in res.course_details:
                        if cd.condonation_fee > 0:
                            writer.writerow([
                                s.student_id, s.name, s.section, cd.course_id,
                                f"{cd.final_percentage:.2f}", cd.condonation_fee,
                                res.total_condonation_fee
                            ])
                            count += 1
        return count

    # ------------------------------------------------------------------
    # 8. Absence Fine Report
    # ------------------------------------------------------------------

    def generate_fine_report(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
        up_to_date: Optional[date] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        students = self._filter_students(self._calc._stu.all_students(), student_id, section)

        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "streak_start",
                "streak_end", "streak_days", "streak_fine", "total_student_fine"
            ])
            for s in sorted(students, key=lambda x: x.student_id):
                res = self._cond.process_student(s, up_to_date)
                if res.absence_fine.total_fine > 0:
                    for st in res.absence_fine.streak_details:
                        writer.writerow([
                            s.student_id, s.name, s.section, st["start"],
                            st["end"], st["days"], st["fine"], res.absence_fine.total_fine
                        ])
                        count += 1
        return count

    # ------------------------------------------------------------------
    # 9. Warning Letters Summary
    # ------------------------------------------------------------------

    def generate_warning_letters_report(
        self,
        output_path: Path,
        student_id: Optional[str] = None,
        section: Optional[str] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "letter_id", "student_id", "student_name", "section", "run_date",
                "month_no", "attendance_pct", "warning_level",
                "parent_copy_required", "parent_meeting_flag"
            ])
            for l in self._warn.all_letters:
                if student_id and l.student_id.upper() != student_id.upper():
                    continue
                if section and l.section.upper() != section.upper():
                    continue
                writer.writerow([
                    l.letter_id, l.student_id, l.student_name, l.section,
                    l.run_date, l.month_no, f"{l.attendance_pct:.2f}",
                    l.warning_level.value, "YES" if l.parent_copy_required else "NO",
                    "YES" if l.parent_meeting_flag else "NO"
                ])
                count += 1
        return count

    # ------------------------------------------------------------------
    # 10. Parent Meeting Flags
    # ------------------------------------------------------------------

    def generate_parent_meeting_report(
        self,
        output_path: Path,
        section: Optional[str] = None,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "flagged_date",
                "month_3_attendance_pct", "reason"
            ])
            for f in self._warn.parent_meeting_flags:
                if section and f.section.upper() != section.upper():
                    continue
                writer.writerow([
                    f.student_id, f.student_name, f.section, f.flagged_date,
                    f"{f.month_3_attendance_pct:.2f}", f.reason
                ])
                count += 1
        return count

    # ------------------------------------------------------------------
    # 11. Attendance Correction Audit Log
    # ------------------------------------------------------------------

    def generate_audit_log_report(
        self,
        output_path: Path,
    ) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "log_id", "timestamp", "student_id", "course_id",
                "record_date", "period_no", "old_status", "new_status",
                "authorized_by", "reason", "is_post_freeze"
            ])
            for log in self._audit_logs:
                writer.writerow([
                    log.log_id, log.timestamp, log.student_id, log.course_id,
                    log.record_date, log.period_no, log.old_status or "NONE",
                    log.new_status, log.authorized_by, log.reason,
                    "YES" if log.is_post_freeze else "NO"
                ])
                count += 1
        return count
