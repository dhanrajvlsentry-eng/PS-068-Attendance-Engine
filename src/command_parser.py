"""
PS-068 Command Parser
======================
Reads and executes a plain-text command file line by line.

Command syntax (one command per line, blank lines and # comments ignored):

  REGISTER_STUDENT  student_id  name  programme  semester  section  admission_date  is_lateral
  REGISTER_COURSE   course_id   course_name  weekly_periods  is_lab  section
  MARK_HOLIDAY      date  description
  MARK_WORKING_SAT  date  replaces_day
  RECORD_ATTENDANCE student_id  course_id  date  period_no  status  [arrival_time]  [faculty_id]
  APPROVE_OD        od_id  student_id  course_id  date  reason  approved_by
  APPROVE_CONDONATION  application_id  approved_by  approved_on
  REJECT_CONDONATION   application_id  rejected_by
  SHOW_ATTENDANCE   student_id  [up_to_date]
  SHOW_SECTION_ATTENDANCE  section  [up_to_date]
  SHOW_INELIGIBLE   [up_to_date]
  GENERATE_REPORT   report_type  [section]  [up_to_date]  [output_file]
  SET_SEMESTER      start_date  end_date  [exam_start_date]
  LOAD_DATA         data_dir
  EXIT

All dates use ISO format (YYYY-MM-DD).  
Boolean fields accept TRUE / FALSE (case-insensitive).
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CommandParser:
    """
    Parses and dispatches commands to registered handler functions.

    Usage::

        parser = CommandParser()
        parser.register("LOAD_DATA", handle_load_data)
        parser.run_file("command.txt")
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._stop = False

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register(self, command: str, handler: Callable) -> None:
        """Register a handler function for a command keyword."""
        self._handlers[command.upper()] = handler

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_file(self, path: str | Path) -> None:
        """Read and execute every command in a command file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Command file not found: {path}")

        with open(path, encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                if self._stop:
                    break
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                self._dispatch(line, line_no)

    def run_line(self, line: str) -> None:
        """Execute a single command string (for interactive / test use)."""
        line = line.strip()
        if line and not line.startswith("#"):
            self._dispatch(line, 0)

    def stop(self) -> None:
        """Signal the parser to stop processing after the current command."""
        self._stop = True

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, line: str, line_no: int) -> None:
        parts = line.split()
        if not parts:
            return
        cmd  = parts[0].upper()
        args = parts[1:]

        handler = self._handlers.get(cmd)
        if handler is None:
            if cmd == "EXIT":
                logger.info("EXIT command received; stopping parser.")
                self._stop = True
                return
            logger.warning("Line %d: Unknown command '%s'; skipped.", line_no, cmd)
            return

        try:
            handler(args)
        except Exception as exc:
            logger.error("Line %d: Error executing '%s': %s", line_no, cmd, exc)
            raise  # re-raise so test harness can catch failures


# ---------------------------------------------------------------------------
# Argument parsing helpers (used by handlers in main.py)
# ---------------------------------------------------------------------------

def require_args(args: List[str], n: int, cmd_name: str) -> None:
    """Raise ValueError if fewer than n args were supplied."""
    if len(args) < n:
        raise ValueError(
            f"{cmd_name} requires at least {n} argument(s); got {len(args)}: {args}"
        )


def parse_bool_arg(val: str) -> bool:
    return val.strip().upper() in ("TRUE", "1", "YES", "Y")


def parse_date_arg(val: str) -> date:
    return date.fromisoformat(val.strip())


def parse_optional_date(args: List[str], index: int) -> Optional[date]:
    if index < len(args) and args[index].strip():
        return parse_date_arg(args[index])
    return None

