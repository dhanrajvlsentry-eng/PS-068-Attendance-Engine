"""
PS-068 Sample Data Generator
=============================
Generates realistic, deterministic sample data for the attendance engine.

Scale:
  - 300 students across 4 sections (A, B, C, D)
  - 24 courses (theory + lab)
  - 16-week academic calendar (semester = 112 days, ~80 teaching days)
  - ~40,000 period-level attendance records
  - ~60 condonation applications
  - Placement calendar and OD approvals

Fixed random seed → fully reproducible.

Usage:
  python generate_data.py                  # writes to ./data/
  python generate_data.py --out-dir data   # explicit directory
  from generate_data import generate_all; generate_all(Path("data"))
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import random
from datetime import date, timedelta, time
from pathlib import Path
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

SEED = 42
RNG  = random.Random(SEED)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECTIONS       = ["A", "B", "C", "D"]
STUDENTS_TOTAL = 300
SEMESTER_START = date(2024, 7, 29)   # Monday, week 1
WEEKS          = 16
SEMESTER_END   = SEMESTER_START + timedelta(weeks=WEEKS) - timedelta(days=1)
EXAM_START     = SEMESTER_END - timedelta(weeks=2)   # last 2 weeks = exams

DAYS_OF_WEEK   = ["MON", "TUE", "WED", "THU", "FRI"]   # Sat only for working-Sat
PERIODS_PER_DAY = 7   # period 1-7

LATE_GRACE_MIN  = 10

# Period start times (HH:MM)
PERIOD_TIMES = {
    1: (time(8, 0),  time(8, 55)),
    2: (time(8, 55), time(9, 50)),
    3: (time(9, 55), time(10, 50)),
    4: (time(10, 50), time(11, 45)),
    5: (time(11, 45), time(12, 40)),
    6: (time(13, 30), time(14, 25)),
    7: (time(14, 25), time(15, 20)),
}

PROGRAMMES = [
    "B.Tech CSE", "B.Tech ECE", "B.Tech EEE", "B.Tech MECH",
]

# ---------------------------------------------------------------------------
# Faculty pool
# ---------------------------------------------------------------------------

FACULTY_IDS = [f"FAC{i:03d}" for i in range(1, 41)]

# ---------------------------------------------------------------------------
# 24 Courses definition
# ---------------------------------------------------------------------------

RAW_COURSES = [
    # (course_id, name, weekly_periods, is_lab, section)
    # section="" means all sections share the course
    ("CS101", "Engineering Mathematics I",    4, False, ""),
    ("CS102", "Physics for Engineers",         3, False, ""),
    ("CS103", "Chemistry for Engineers",       3, False, ""),
    ("CS104", "Programming in C",             4, False, ""),
    ("CS105", "Digital Logic Design",          3, False, ""),
    ("CS106", "Data Structures",              4, False, ""),
    ("CS107", "Computer Organisation",        3, False, ""),
    ("CS108", "Discrete Mathematics",         3, False, ""),
    ("CS109", "Object Oriented Programming",  4, False, ""),
    ("CS110", "Database Management Systems",  3, False, ""),
    ("CS111", "Operating Systems",            3, False, ""),
    ("CS112", "Computer Networks",            3, False, ""),
    ("CS113", "Software Engineering",         3, False, ""),
    ("CS114", "Theory of Computation",        3, False, ""),
    ("CS115", "Compiler Design",              3, False, ""),
    ("CS116", "Artificial Intelligence",      3, False, ""),
    ("CS117", "Machine Learning",             3, False, ""),
    ("CS118", "Web Technologies",             3, False, ""),
    # Labs (is_lab=True, weekly_periods = slots; each slot = 2 periods)
    ("CS191", "Physics Lab",                  2, True,  ""),
    ("CS192", "Chemistry Lab",                2, True,  ""),
    ("CS193", "Programming in C Lab",         2, True,  ""),
    ("CS194", "Data Structures Lab",          2, True,  ""),
    ("CS195", "DBMS Lab",                     2, True,  ""),
    ("CS196", "Networks Lab",                 2, True,  ""),
]

assert len(RAW_COURSES) == 24, f"Expected 24 courses, got {len(RAW_COURSES)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_time(t: time) -> str:
    return t.strftime("%H:%M")


def _date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _add_minutes(t: time, mins: int) -> time:
    total = t.hour * 60 + t.minute + mins
    return time(total // 60 % 24, total % 60)


def _write_csv(path: Path, headers: List[str], rows: List[List]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)
    logger.info("Wrote %d rows → %s", len(rows), path)


# ---------------------------------------------------------------------------
# Generator functions
# ---------------------------------------------------------------------------

def gen_students() -> List[List]:
    """300 students across 4 sections; ~5% lateral entry."""
    rows = []
    per_section = STUDENTS_TOTAL // len(SECTIONS)
    for idx, section in enumerate(SECTIONS):
        for i in range(per_section):
            n       = idx * per_section + i + 1
            sid     = f"24CS{n:04d}"
            name    = _random_name()
            prog    = RNG.choice(PROGRAMMES)
            semester = 3
            is_lat  = RNG.random() < 0.05   # ~5%
            if is_lat:
                adm_date = SEMESTER_START + timedelta(days=RNG.randint(0, 7))
            else:
                adm_date = SEMESTER_START
            rows.append([sid, name, prog, semester, section,
                          str(adm_date), str(is_lat).upper()])
    return rows


def _random_name() -> str:
    firsts = [
        "Aarav", "Aditya", "Akash", "Ananya", "Anjali", "Arjun", "Ashwin",
        "Bhavana", "Charan", "Deepika", "Dhruv", "Divya", "Farhan", "Gayatri",
        "Harish", "Ishaan", "Jaya", "Karthik", "Kavya", "Keerthi", "Lakshmi",
        "Madhav", "Meera", "Mohit", "Nandini", "Nikhil", "Nisha", "Pooja",
        "Pranav", "Priya", "Rahul", "Ramya", "Ravi", "Rohit", "Sakshi",
        "Sandhya", "Sanjay", "Shreya", "Siddharth", "Sneha", "Suresh",
        "Tanisha", "Tejas", "Uma", "Varun", "Vijay", "Vishnu", "Yamini", "Zara",
    ]
    lasts = [
        "Agarwal", "Bhat", "Chakraborty", "Das", "Desai", "Gandhi", "Gupta",
        "Iyer", "Jain", "Joshi", "Kapoor", "Kumar", "Mehta", "Mishra",
        "Nair", "Patel", "Pillai", "Rao", "Reddy", "Sharma", "Singh",
        "Sinha", "Srivastava", "Verma", "Yadav",
    ]
    return f"{RNG.choice(firsts)} {RNG.choice(lasts)}"


def gen_courses() -> List[List]:
    rows = []
    for cid, name, wp, is_lab, section in RAW_COURSES:
        rows.append([cid, name, wp, str(is_lab).upper(), section])
    return rows


def gen_academic_calendar() -> Tuple[List[List], Set[date]]:
    """
    Returns (rows, holiday_set).
    16 weeks, July 29 2024 – Nov 16 2024.
    Includes:
      - 10 public holidays
      - 3 working Saturdays
      - Exam period (last 14 days) marked as EXAM
    """
    rows        = []
    holidays    = set()
    exam_dates  = set()

    # Fixed public holidays
    holiday_defs = [
        (date(2024, 8, 15), "Independence Day"),
        (date(2024, 9, 2),  "Onam"),
        (date(2024, 9, 16), "Milad-un-Nabi"),
        (date(2024, 10, 2), "Gandhi Jayanti"),
        (date(2024, 10, 12), "Dussehra"),
        (date(2024, 10, 24), "Diwali"),
        (date(2024, 10, 25), "Diwali Holiday"),
        (date(2024, 11, 1),  "Kannada Rajyotsava"),
        (date(2024, 11, 5),  "Deepavali"),
        (date(2024, 11, 15), "Guru Nanak Jayanti"),
    ]
    for d, _ in holiday_defs:
        holidays.add(d)

    # Working Saturdays (follow previous day's timetable)
    working_sats = [
        (date(2024, 8, 10), "MON"),
        (date(2024, 9, 14), "TUE"),
        (date(2024, 10, 19), "WED"),
    ]
    working_sat_dates = {d for d, _ in working_sats}

    # Exam period: last 2 weeks before semester end
    exam_start = SEMESTER_END - timedelta(days=13)
    cur = exam_start
    while cur <= SEMESTER_END:
        if cur.weekday() < 5:  # Mon-Fri
            exam_dates.add(cur)
        cur += timedelta(days=1)

    for d in _date_range(SEMESTER_START, SEMESTER_END):
        wd = d.weekday()   # 0=Mon … 6=Sun
        if d in holidays:
            day_type = "HOLIDAY"
            replaces = ""
        elif d in exam_dates:
            day_type = "EXAM"
            replaces = ""
        elif d in working_sat_dates:
            day_type = "WORKING_SATURDAY"
            replaces = dict(working_sats)[d]
        elif wd == 6:   # Sunday
            day_type = "SUNDAY"
            replaces = ""
        elif wd == 5:   # regular Saturday (not working)
            day_type = "SATURDAY"
            replaces = ""
        else:           # Mon-Fri
            day_type = "WORKING"
            replaces = ""

        rows.append([str(d), day_type, replaces])

    return rows, holidays


def gen_timetable(courses: List[List]) -> List[List]:
    """
    Generate a weekly timetable for each section.
    Theory courses: occupy 1 period slot each.
    Lab courses   : occupy 2 consecutive period slots (counts as 2 periods each).
    Each section has the same 24 courses scheduled across Mon-Fri.
    """
    theory = [(r[0], r[1], int(r[2])) for r in courses if r[3] == "FALSE"]
    labs   = [(r[0], r[1], int(r[2])) for r in courses if r[3] == "TRUE"]

    rows = []

    for section in SECTIONS:
        # Build a simple fixed timetable for the section
        # 5 days × 7 periods = 35 slots; theory fills most, labs get 2-slot blocks

        # Assign theory courses to Mon-Fri periods
        # Each theory course appears `weekly_periods` times per week
        slots_grid: Dict[str, List[str]] = {d: [""] * 8 for d in DAYS_OF_WEEK}
        # period 0 unused, periods 1-7

        # Flatten: for each theory course, assign periods
        all_theory_assignments = []
        for cid, cname, wp in theory:
            for _ in range(wp):
                all_theory_assignments.append(cid)

        # Shuffle with section-specific seed for variety
        rng_local = random.Random(SEED + ord(section))
        rng_local.shuffle(all_theory_assignments)

        # Fill periods day by day, leaving slots 6-7 on specific days for labs
        lab_slots = {   # (day, period_start): course_id
            "MON": (6, labs[0][0] if labs else None),
            "TUE": (6, labs[1][0] if len(labs)>1 else None),
            "WED": (6, labs[2][0] if len(labs)>2 else None),
            "THU": (6, labs[3][0] if len(labs)>3 else None),
            "FRI": (6, labs[4][0] if len(labs)>4 else None),
        }
        # Second lab batch
        lab_slots2 = {
            "MON": (None, labs[5][0] if len(labs)>5 else None),
        }

        idx = 0
        for day in DAYS_OF_WEEK:
            lab_cid  = lab_slots.get(day, (None, None))[1]
            lab_pstart = lab_slots.get(day, (None, None))[0]

            for period in range(1, 8):
                # Lab slot: periods 6-7
                if lab_cid and period in (6, 7) and lab_pstart == 6:
                    slots_grid[day][period] = lab_cid
                else:
                    if idx < len(all_theory_assignments):
                        if slots_grid[day][period] == "":
                            slots_grid[day][period] = all_theory_assignments[idx]
                            idx += 1

        # Write timetable rows
        for day in DAYS_OF_WEEK:
            for period in range(1, 8):
                cid = slots_grid[day][period]
                if not cid:
                    continue
                st, en = PERIOD_TIMES[period]
                rows.append([section, day, period, cid,
                              _fmt_time(st), _fmt_time(en)])

    return rows


def gen_attendance(
    students: List[List],
    timetable: List[List],
    calendar_rows: List[List],
    holidays: Set[date],
) -> Tuple[List[List], List[List], List[List]]:
    """
    Generate ~40,000 attendance records with realistic attendance patterns.
    Returns (attendance_rows, od_approval_rows, condonation_rows).
    """
    # Build quick lookup structures
    # timetable: (section, day, period) -> course_id
    tt_map: Dict[Tuple[str, str, int], str] = {}
    for row in timetable:
        sec, day, pno, cid = row[0], row[1], int(row[2]), row[3]
        tt_map[(sec, day, pno)] = cid

    # Calendar: date -> day_type, replaces_day
    cal_map: Dict[date, Tuple[str, str]] = {}
    working_sat_map: Dict[date, str] = {}
    for row in calendar_rows:
        d = date.fromisoformat(row[0])
        cal_map[d] = (row[1], row[2])
        if row[1] == "WORKING_SATURDAY":
            working_sat_map[d] = row[2]

    def timetable_day_for(d: date) -> str | None:
        dt, rep = cal_map.get(d, ("", ""))
        if dt == "WORKING":
            abbr = ["MON", "TUE", "WED", "THU", "FRI"][d.weekday()]
            return abbr
        if dt == "WORKING_SATURDAY":
            return rep
        return None

    # Student -> (section, attendance_profile)
    # Profile: float probability of being present (determines bucket)
    student_profiles: Dict[str, Tuple[str, float]] = {}
    for srow in students:
        sid, section = srow[0], srow[4]
        p = RNG.random()
        if p < 0.15:
            prob = RNG.uniform(0.55, 0.65)   # very low (<65%)
        elif p < 0.35:
            prob = RNG.uniform(0.65, 0.75)   # condonable (65-75%)
        elif p < 0.60:
            prob = RNG.uniform(0.75, 0.80)   # borderline (75-80%)
        else:
            prob = RNG.uniform(0.80, 0.98)   # good (>80%)
        student_profiles[sid] = (section, prob)

    att_rows       = []
    od_rows        = []
    cond_rows      = []
    rec_counter    = 0
    od_counter     = 0
    cond_counter   = 0

    # Faculty assignment: each (day, course) -> random faculty
    fac_assign: Dict[Tuple[str, str], str] = {}
    for day in DAYS_OF_WEEK:
        for cid in {r[3] for r in timetable}:
            fac_assign[(day, cid)] = RNG.choice(FACULTY_IDS)

    # Condonation type pool
    cond_types = ["MEDICAL", "SPORTS", "NCC_NSS", "PLACEMENT", "BEREAVEMENT"]
    cond_weights = [0.4, 0.2, 0.15, 0.15, 0.1]

    # OD-approved students (placement events will add more)
    od_student_pool: Set[Tuple[str, date]] = set()

    # Generate day-by-day
    all_dates = list(_date_range(SEMESTER_START, SEMESTER_END))

    for d in all_dates:
        tt_day = timetable_day_for(d)
        if tt_day is None:
            continue   # holiday / weekend / exam day

        for srow in students:
            sid, name, prog, sem, section = (
                srow[0], srow[1], srow[2], srow[3], srow[4]
            )
            adm_date = date.fromisoformat(srow[5])
            if d < adm_date:
                continue   # student not yet admitted

            section_u = section.upper()
            _, pres_prob = student_profiles[sid]

            # Determine if student is "out" for the whole day
            day_absent = RNG.random() > pres_prob
            is_od_day  = False
            if day_absent and RNG.random() < 0.08:   # 8% of absences are OD
                is_od_day = True

            for period in range(1, 8):
                key = (section_u, tt_day, period)
                cid = tt_map.get(key)
                if not cid:
                    continue

                rec_counter += 1
                rid = f"R{rec_counter:07d}"

                # Faculty teaches this period
                faculty_id = fac_assign.get((tt_day, cid), FACULTY_IDS[0])

                if is_od_day:
                    status       = "OD"
                    arrival_time = ""
                    # Create OD approval ~70% of the time
                    if RNG.random() < 0.70:
                        od_counter += 1
                        od_rows.append([
                            f"OD{od_counter:04d}", sid, cid, str(d),
                            "Placement / Official duty", "HOD001"
                        ])
                        od_student_pool.add((sid, d))

                elif day_absent:
                    status       = "ABSENT"
                    arrival_time = ""

                else:
                    # Present or Late
                    if RNG.random() < 0.06:   # 6% chance of LATE
                        status = "LATE"
                        # 50% within grace window
                        st_time = PERIOD_TIMES[period][0]
                        if RNG.random() < 0.50:
                            # within grace → will count as PRESENT
                            delta = RNG.randint(1, LATE_GRACE_MIN)
                        else:
                            # beyond grace → counts as ABSENT
                            delta = RNG.randint(LATE_GRACE_MIN + 1, 25)
                        arr = _add_minutes(st_time, delta)
                        arrival_time = _fmt_time(arr)
                    else:
                        status       = "PRESENT"
                        arrival_time = ""

                att_rows.append([
                    rid, sid, cid, str(d), period, status,
                    arrival_time, faculty_id
                ])

    # --- Condonation applications (~60) -----------------------------------
    # Pick students with attendance 65-80% for applications
    # We'll sample from the student list
    cond_candidates = [
        sid for sid, (sec, prob) in student_profiles.items()
        if 0.55 <= prob <= 0.80
    ]
    RNG.shuffle(cond_candidates)
    cond_target = 60

    # RAW_COURSES has Python booleans; use the native tuple directly
    cond_cids = [r[0] for r in RAW_COURSES if not r[3]]   # theory courses (is_lab=False)

    for i, sid in enumerate(cond_candidates[:cond_target]):
        cond_counter += 1
        app_id  = f"COND{cond_counter:04d}"
        cid     = RNG.choice(cond_cids)
        reason  = RNG.choices(cond_types, cond_weights)[0]
        week    = RNG.randint(2, 12)
        start_d = SEMESTER_START + timedelta(weeks=week, days=RNG.randint(0, 4))
        end_d   = start_d + timedelta(days=RNG.randint(1, 5))
        end_d   = min(end_d, SEMESTER_END - timedelta(weeks=2))

        status   = RNG.choices(["PENDING", "APPROVED", "REJECTED"], [0.3, 0.5, 0.2])[0]
        appr_by  = "HOD001" if status != "PENDING" else ""
        appr_on  = str(end_d + timedelta(days=3)) if status != "PENDING" else ""

        # Medical certificate reference for MEDICAL applications
        if reason == "MEDICAL":
            med_cert = f"MC-{app_id}"
            # 90% submitted on time (within 7 days), 10% late
            submit_offset = RNG.randint(1, 6) if RNG.random() < 0.90 else RNG.randint(8, 14)
            submit_d = str(end_d + timedelta(days=submit_offset))
        elif reason == "BEREAVEMENT":
            med_cert = ""
            # Ensure bereavement spans at most 5 calendar days
            end_d = min(end_d, start_d + timedelta(days=4))
            submit_offset = RNG.randint(1, 3) if RNG.random() < 0.90 else RNG.randint(4, 7)
            submit_d = str(end_d + timedelta(days=submit_offset))
        else:
            med_cert = ""
            submit_d = str(end_d + timedelta(days=RNG.randint(1, 5)))

        descs = {
            "MEDICAL":     "Hospitalised due to illness",
            "SPORTS":      "Inter-university sports event",
            "NCC_NSS":     "NCC/NSS camp attendance",
            "PLACEMENT":   "Placement drive at company",
            "BEREAVEMENT": "Family bereavement",
        }
        cond_rows.append([
            app_id, sid, cid, reason,
            str(start_d), str(end_d),
            descs[reason],
            status, appr_by, appr_on,
            med_cert, submit_d
        ])

    # --- Placement events (additional OD source) -------------------------
    # Already generated inline; placement_calendar handled separately
    return att_rows, od_rows, cond_rows


def gen_placement_calendar(students: List[List]) -> List[List]:
    """Generate ~8 placement events across the semester."""
    companies = [
        "TCS", "Infosys", "Wipro", "Cognizant", "HCL",
        "Accenture", "Capgemini", "Amazon", "Google", "Microsoft"
    ]
    rows = []
    all_sids = [r[0] for r in students]

    for i in range(8):
        event_id  = f"PE{i+1:03d}"
        week      = RNG.randint(8, 14)
        day_offset = RNG.randint(0, 4)   # Mon-Fri
        ev_date   = SEMESTER_START + timedelta(weeks=week, days=day_offset)
        company   = RNG.choice(companies)
        # 10-25 students per drive
        count     = RNG.randint(10, 25)
        selected  = RNG.sample(all_sids, min(count, len(all_sids)))
        rows.append([event_id, str(ev_date), company, "|".join(selected)])

    return rows


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def generate_all(out_dir: Path = Path("data")):
    """Generate all CSV files and write them to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Generating sample data  →  {out_dir.resolve()}")

    # Students
    student_rows = gen_students()
    _write_csv(
        out_dir / "students.csv",
        ["student_id", "name", "programme", "semester", "section",
         "admission_date", "is_lateral"],
        student_rows,
    )

    # Courses
    course_rows = gen_courses()
    _write_csv(
        out_dir / "courses.csv",
        ["course_id", "course_name", "weekly_periods", "is_lab", "section"],
        course_rows,
    )

    # Academic calendar
    cal_rows, holidays = gen_academic_calendar()
    _write_csv(
        out_dir / "academic_calendar.csv",
        ["calendar_date", "day_type", "replaces_day"],
        cal_rows,
    )

    # Timetable
    tt_rows = gen_timetable(course_rows)
    _write_csv(
        out_dir / "timetable.csv",
        ["section", "day", "period_no", "course_id", "start_time", "end_time"],
        tt_rows,
    )

    # Attendance, OD approvals, Condonation applications
    att_rows, od_rows, cond_rows = gen_attendance(
        student_rows, tt_rows, cal_rows, holidays
    )
    _write_csv(
        out_dir / "attendance.csv",
        ["record_id", "student_id", "course_id", "record_date", "period_no",
         "status", "arrival_time", "faculty_id"],
        att_rows,
    )
    _write_csv(
        out_dir / "od_approvals.csv",
        ["od_id", "student_id", "course_id", "od_date", "reason", "approved_by"],
        od_rows,
    )
    _write_csv(
        out_dir / "condonation_applications.csv",
        ["application_id", "student_id", "course_id", "reason",
         "start_date", "end_date", "description", "status",
         "approved_by", "approved_on", "medical_cert_ref", "submitted_on"],
        cond_rows,
    )

    # Placement calendar
    pl_rows = gen_placement_calendar(student_rows)
    _write_csv(
        out_dir / "placement_calendar.csv",
        ["event_id", "event_date", "company", "student_ids"],
        pl_rows,
    )

    total_att = len(att_rows)
    total_cond = len(cond_rows)
    print(f"\nSample data generation complete:")
    print(f"  Students               : {len(student_rows)}")
    print(f"  Courses                : {len(course_rows)}")
    print(f"  Calendar entries       : {len(cal_rows)}")
    print(f"  Timetable slots        : {len(tt_rows)}")
    print(f"  Attendance records     : {total_att:,}")
    print(f"  OD approvals           : {len(od_rows)}")
    print(f"  Condonation applications: {total_cond}")
    print(f"  Placement events       : {len(pl_rows)}")
    print(f"\nAll files written to: {out_dir.resolve()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="PS-068 Sample Data Generator")
    ap.add_argument("--out-dir", default="data", help="Output directory (default: data)")
    args = ap.parse_args()
    generate_all(Path(args.out_dir))
