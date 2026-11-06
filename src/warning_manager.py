"""
PS-068 Warning Manager
=======================
Manages monthly warning letter evaluations, parent copies, escalation tracking,
and parent meeting flags.

Business Rules
--------------
- Below 80%: FIRST_WARNING.
- Below 75%: SECOND_WARNING with parent copy.
- Third month below 75%: PARENT_MEETING flag.
- Issue each warning level at most once per semester per student.
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.models import (
    ParentMeetingFlag,
    Student,
    WarningLevel,
    WarningLetter,
)
from src.attendance_calculator import AttendanceCalculator
from src.student_manager import StudentManager

logger = logging.getLogger(__name__)


class WarningManager:
    """
    Evaluates students during monthly warning letter runs, persists history,
    and enforces level deduplication and parent meeting escalation.
    """

    def __init__(
        self,
        calculator: AttendanceCalculator,
        student_mgr: StudentManager,
    ):
        self._calc = calculator
        self._stu  = student_mgr

        # History tracking across runs
        # student_id -> set of WarningLevels already issued
        self._issued_levels: Dict[str, Set[WarningLevel]] = defaultdict(set)
        # student_id -> list of (month_no, pct) for evaluations where pct < 75.0
        self._below_75_months: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
        # All generated letters across runs
        self._all_letters: List[WarningLetter] = []
        # Flagged students for parent meeting
        self._parent_meeting_flags: Dict[str, ParentMeetingFlag] = {}

    @property
    def all_letters(self) -> List[WarningLetter]:
        return self._all_letters

    @property
    def parent_meeting_flags(self) -> List[ParentMeetingFlag]:
        return list(self._parent_meeting_flags.values())

    # ------------------------------------------------------------------
    # Warning evaluation run
    # ------------------------------------------------------------------

    def evaluate_run(
        self,
        run_date: date,
        month_no: int,
        output_dir: Optional[Path] = None,
        section: Optional[str] = None,
    ) -> Tuple[List[WarningLetter], List[ParentMeetingFlag]]:
        """
        Execute a monthly warning run for a given cut-off date and month number.
        Returns newly generated letters and new/updated parent meeting flags.
        """
        students = self._stu.by_section(section) if section else self._stu.all_students()
        new_letters: List[WarningLetter] = []
        new_flags: List[ParentMeetingFlag] = []

        letter_seq = len(self._all_letters)

        for student in sorted(students, key=lambda s: s.student_id):
            summary = self._calc.calculate_for_student(student, up_to_date=run_date)
            pct = summary.overall_percentage

            has_parent_meeting = False

            # Check if below 75%
            if pct < 75.0:
                self._below_75_months[student.student_id].append((month_no, pct))
                # Check for 3rd month below 75%
                if len(self._below_75_months[student.student_id]) >= 3:
                    has_parent_meeting = True
                    if student.student_id not in self._parent_meeting_flags:
                        history = [p for _, p in self._below_75_months[student.student_id]]
                        pm_flag = ParentMeetingFlag(
                            student_id=student.student_id,
                            student_name=student.name,
                            section=student.section,
                            flagged_date=run_date,
                            month_3_attendance_pct=pct,
                            history_pct=history,
                        )
                        self._parent_meeting_flags[student.student_id] = pm_flag
                        new_flags.append(pm_flag)

                # Check SECOND_WARNING eligibility (issue at most once per semester)
                if WarningLevel.SECOND_WARNING not in self._issued_levels[student.student_id]:
                    letter_seq += 1
                    lid = f"WL-{run_date.strftime('%Y%m')}-{letter_seq:04d}"
                    text = self._format_letter(
                        student=student,
                        run_date=run_date,
                        month_no=month_no,
                        pct=pct,
                        conducted=summary.total_conducted,
                        present=summary.total_present,
                        level=WarningLevel.SECOND_WARNING,
                        parent_meeting=has_parent_meeting,
                    )
                    wl = WarningLetter(
                        letter_id=lid,
                        student_id=student.student_id,
                        student_name=student.name,
                        section=student.section,
                        run_date=run_date,
                        month_no=month_no,
                        attendance_pct=pct,
                        warning_level=WarningLevel.SECOND_WARNING,
                        parent_copy_required=True,
                        parent_meeting_flag=has_parent_meeting,
                        letter_text=text,
                    )
                    self._issued_levels[student.student_id].add(WarningLevel.SECOND_WARNING)
                    self._all_letters.append(wl)
                    new_letters.append(wl)

            elif pct < 80.0:
                # FIRST_WARNING eligibility (below 80%, at most once per semester)
                if WarningLevel.FIRST_WARNING not in self._issued_levels[student.student_id]:
                    letter_seq += 1
                    lid = f"WL-{run_date.strftime('%Y%m')}-{letter_seq:04d}"
                    text = self._format_letter(
                        student=student,
                        run_date=run_date,
                        month_no=month_no,
                        pct=pct,
                        conducted=summary.total_conducted,
                        present=summary.total_present,
                        level=WarningLevel.FIRST_WARNING,
                        parent_meeting=False,
                    )
                    wl = WarningLetter(
                        letter_id=lid,
                        student_id=student.student_id,
                        student_name=student.name,
                        section=student.section,
                        run_date=run_date,
                        month_no=month_no,
                        attendance_pct=pct,
                        warning_level=WarningLevel.FIRST_WARNING,
                        parent_copy_required=False,
                        parent_meeting_flag=False,
                        letter_text=text,
                    )
                    self._issued_levels[student.student_id].add(WarningLevel.FIRST_WARNING)
                    self._all_letters.append(wl)
                    new_letters.append(wl)

        # Write files if output directory specified
        if output_dir:
            self._write_run_output(output_dir, run_date, month_no, new_letters, new_flags)

        logger.info(
            "Warning run %d (date=%s): issued %d letters, %d parent meeting flags",
            month_no, run_date, len(new_letters), len(new_flags)
        )
        return new_letters, new_flags

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_letter(
        self,
        student: Student,
        run_date: date,
        month_no: int,
        pct: float,
        conducted: int,
        present: int,
        level: WarningLevel,
        parent_meeting: bool,
    ) -> str:
        border = "=" * 76
        sub = "OFFICIAL ATTENDANCE WARNING LETTER" if level == WarningLevel.FIRST_WARNING else "CRITICAL ATTENDANCE WARNING - PARENT COPY INCLUDED"
        lines = [
            border,
            f"                     INSTITUTE OF TECHNOLOGY & SCIENCE",
            f"                         OFFICE OF THE DEAN / HOD",
            border,
            f"Date         : {run_date.strftime('%d-%b-%Y')}",
            f"Month Eval   : Month {month_no}",
            f"Student Name : {student.name}",
            f"Student ID   : {student.student_id}",
            f"Programme    : {student.programme} (Semester {student.semester}, Section {student.section})",
            f"Subject      : {sub}",
            border,
            "",
            f"Dear {student.name},",
            "",
            f"This is an official communication regarding your academic attendance records.",
            f"As of {run_date}, your cumulative attendance is {pct:.2f}% ({present} out of {conducted} periods held).",
            "",
        ]

        if level == WarningLevel.FIRST_WARNING:
            lines.extend([
                "LEVEL: FIRST ATTENDANCE WARNING (Below 80% Threshold)",
                "-------------------------------------------------------",
                "Your attendance has dropped below 80%. Please note that university regulations require",
                "a minimum of 75% attendance in every course to be eligible for end-semester examinations.",
                "You are advised to attend all upcoming classes regularly to avoid disciplinary action.",
            ])
        else:
            lines.extend([
                "LEVEL: SECOND ATTENDANCE WARNING (Below 75% Mandatory Minimum) - PARENT COPY",
                "------------------------------------------------------------------------------",
                "CRITICAL: Your attendance has fallen below the mandatory 75% statutory requirement.",
                "Students with attendance below 75% will NOT be permitted to write final examinations",
                "unless eligible condonation is formally granted.",
                "A copy of this notice is being forwarded to your parent/guardian.",
            ])

        if parent_meeting:
            lines.extend([
                "",
                "*** MANDATORY PARENT MEETING REQUIRED ***",
                "Your attendance has remained below 75% for THREE consecutive evaluation periods.",
                "Your parents / legal guardians are hereby requested to meet the Head of the Department",
                "within one week of receipt of this notice.",
            ])

        lines.extend([
            "",
            "Dean (Academics) / Head of Department",
            border,
        ])
        return "\n".join(lines)

    def _write_run_output(
        self,
        output_dir: Path,
        run_date: date,
        month_no: int,
        letters: List[WarningLetter],
        flags: List[ParentMeetingFlag],
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)

        # 1. Summary CSV
        summary_csv = output_dir / f"warning_run_{month_no}_summary.csv"
        with open(summary_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "letter_id", "student_id", "student_name", "section",
                "run_date", "month_no", "attendance_pct", "warning_level",
                "parent_copy_required", "parent_meeting_flag"
            ])
            for l in letters:
                writer.writerow([
                    l.letter_id, l.student_id, l.student_name, l.section,
                    l.run_date, l.month_no, f"{l.attendance_pct:.2f}",
                    l.warning_level.value, "YES" if l.parent_copy_required else "NO",
                    "YES" if l.parent_meeting_flag else "NO",
                ])

        # 2. Parent meeting flags CSV
        pm_csv = output_dir / f"parent_meeting_flags_month_{month_no}.csv"
        with open(pm_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "student_id", "student_name", "section", "flagged_date",
                "month_3_attendance_pct", "reason"
            ])
            for sid, f in self._parent_meeting_flags.items():
                writer.writerow([
                    f.student_id, f.student_name, f.section, f.flagged_date,
                    f"{f.month_3_attendance_pct:.2f}", f.reason
                ])

        # 3. Individual letters directory
        letters_dir = output_dir / "letters"
        letters_dir.mkdir(parents=True, exist_ok=True)
        for l in letters:
            fn = letters_dir / f"{l.student_id}_{l.warning_level.value}.txt"
            with open(fn, "w", encoding="utf-8") as fh:
                fh.write(l.letter_text)

