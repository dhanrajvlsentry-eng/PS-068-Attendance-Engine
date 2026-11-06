# PS-068 – Student Attendance and Condonation / Eligibility Engine

A command-file-driven backend application for tracking student attendance,
computing eligibility, and processing condonation applications for a 16-week
academic semester.

---

## Project Scale

| Dimension              | Value            |
|------------------------|------------------|
| Students               | 300              |
| Sections               | 4 (A, B, C, D)   |
| Courses                | 24               |
| Semester length        | 16 weeks         |
| Attendance records     | ~40,000          |
| Condonation apps       | ~60              |

---

## Folder Structure

```
PS-068/
├── main.py                  ← Entry point; reads command.txt
├── generate_data.py         ← Deterministic sample-data generator
├── command.txt              ← Full workflow command file
├── requirements.txt
├── README.md
│
├── data/                    ← CSV input files (see schemas below)
│   ├── students.csv
│   ├── courses.csv
│   ├── timetable.csv
│   ├── academic_calendar.csv
│   ├── attendance.csv
│   ├── condonation_applications.csv
│   ├── placement_calendar.csv
│   └── od_approvals.csv
│
├── src/
│   ├── models.py            ← Dataclass entities + enums
│   ├── data_loader.py       ← CSV → typed domain objects
│   ├── student_manager.py   ← Student registry + section index
│   ├── course_manager.py    ← Course registry + section-aware lookup
│   ├── calendar_manager.py  ← Date classification, working-day enumeration
│   ├── attendance_manager.py← Indexed records; O(1) OD lookup
│   ├── attendance_calculator.py ← Core engine; applies all 9 business rules
│   └── command_parser.py    ← Line-by-line command dispatcher
│
├── tests/
│   └── test_attendance_engine.py  ← 40+ unit tests (pytest)
│
└── docs/
    └── design_document.md
```

---

## Input File Schemas

### `students.csv`
| Column         | Type    | Description                           |
|----------------|---------|---------------------------------------|
| student_id     | string  | Unique ID, e.g. `24CS0001`            |
| name           | string  | Full name                             |
| programme      | string  | e.g. `B.Tech CSE`                     |
| semester       | int     | Current semester number               |
| section        | string  | A / B / C / D                         |
| admission_date | date    | ISO format `YYYY-MM-DD`               |
| is_lateral     | bool    | `TRUE` / `FALSE`                      |

### `courses.csv`
| Column         | Type    | Description                           |
|----------------|---------|---------------------------------------|
| course_id      | string  | e.g. `CS101`                          |
| course_name    | string  | Full course name                      |
| weekly_periods | int     | Periods per week                      |
| is_lab         | bool    | `TRUE` = lab (each slot = 2 periods)  |
| section        | string  | Section assignment; blank = all       |

### `academic_calendar.csv`
| Column         | Type    | Description                                                     |
|----------------|---------|-----------------------------------------------------------------|
| calendar_date  | date    | ISO format                                                      |
| day_type       | string  | `WORKING` / `HOLIDAY` / `SATURDAY` / `SUNDAY` / `EXAM` / `WORKING_SATURDAY` |
| replaces_day   | string  | `MON`…`SAT` – only for `WORKING_SATURDAY`; blank otherwise      |

### `timetable.csv`
| Column     | Type   | Description                              |
|------------|--------|------------------------------------------|
| section    | string | A / B / C / D                            |
| day        | string | `MON` / `TUE` / `WED` / `THU` / `FRI` / `SAT` |
| period_no  | int    | 1-based period number                    |
| course_id  | string | Matches `courses.csv`                    |
| start_time | time   | `HH:MM`                                  |
| end_time   | time   | `HH:MM`                                  |

### `attendance.csv`
| Column       | Type   | Description                                          |
|--------------|--------|------------------------------------------------------|
| record_id    | string | Unique record ID                                     |
| student_id   | string | Matches `students.csv`                               |
| course_id    | string | Matches `courses.csv`                                |
| record_date  | date   | ISO format                                           |
| period_no    | int    | 1-based                                              |
| status       | string | `PRESENT` / `ABSENT` / `LATE` / `OD`                |
| arrival_time | time   | `HH:MM` (required for `LATE`; blank otherwise)       |
| faculty_id   | string | Non-blank = period was conducted; blank = not held   |

### `od_approvals.csv`
| Column      | Type   | Description                                   |
|-------------|--------|-----------------------------------------------|
| od_id       | string | Unique OD approval ID                         |
| student_id  | string |                                               |
| course_id   | string | `*` = applies to all courses on that date     |
| od_date     | date   | ISO format                                    |
| reason      | string | Free text                                     |
| approved_by | string | Faculty / HOD ID                              |

### `placement_calendar.csv`
| Column      | Type   | Description                                           |
|-------------|--------|-------------------------------------------------------|
| event_id    | string | Unique event ID                                       |
| event_date  | date   | ISO format                                            |
| company     | string | Company name                                          |
| student_ids | string | Pipe-separated (`|`) list of participating student IDs|

### `condonation_applications.csv`
| Column           | Type   | Description                                           |
|------------------|--------|-------------------------------------------------------|
| application_id   | string | Unique ID                                             |
| student_id       | string | Matches `students.csv`                                |
| course_id        | string | Matches `courses.csv`                                 |
| reason           | string | `MEDICAL` / `SPORTS` / `NCC_NSS` / `PLACEMENT` / `BEREAVEMENT` |
| start_date       | date   | ISO format                                            |
| end_date         | date   | ISO format                                            |
| description      | string | Free-text description                                 |
| status           | string | `PENDING` / `APPROVED` / `REJECTED` / `REJECTED_LATE` |
| approved_by      | string | Faculty / HOD ID                                      |
| approved_on      | date   | ISO format (blank if pending)                         |
| medical_cert_ref | string | Certificate reference (required for `MEDICAL`)        |
| submitted_on     | date   | Date application filed (used for deadline checks)     |

---

## Business Rules (Part 1)

| Rule | Description |
|------|-------------|
| R1   | Only periods with a non-blank `faculty_id` are counted as conducted |
| R2   | Attendance records on holiday dates are completely ignored |
| R3   | Working Saturdays use the timetable of their `replaces_day` |
| R4   | Lab sessions count as **2 periods** per slot |
| R5   | LATE counts as PRESENT if arrival ≤ slot_start + 10 min; otherwise ABSENT |
| R6   | OD counts as PRESENT only when an approved OD or placement event exists |
| R7   | Regular students: counted from semester_start. Lateral: from admission_date |
| R8   | Percentage = (present / conducted) × 100, displayed to 2 decimal places |
| R9   | Eligible if percentage ≥ 75 %. Condonable: 65 %–75 %. Ineligible: < 65 % |

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Generate sample data
```bash
python generate_data.py
# or
python main.py --generate-data
```

### 3. Run the full workflow
```bash
python main.py
# uses command.txt by default
```

### 4. Use a custom command file
```bash
python main.py --commands path/to/my_commands.txt
```

### 5. Run unit tests
```bash
pytest tests/ -v
# with coverage:
pytest tests/ -v --cov=src --cov-report=term-missing
```

---

## Command Reference

| Command                               | Arguments                                              |
|---------------------------------------|--------------------------------------------------------|
| `LOAD_DATA <dir>`                     | Load all CSVs from directory                           |
| `SET_SEMESTER <start> <end> [exam]`   | Explicitly set semester dates                          |
| `REGISTER_STUDENT ...`                | Add a student at runtime                               |
| `REGISTER_COURSE ...`                 | Add a course at runtime                                |
| `MARK_HOLIDAY <date> [desc]`          | Mark a date as holiday                                 |
| `MARK_WORKING_SAT <date> <day>`       | Mark a Saturday as working (follows `day` timetable)   |
| `RECORD_ATTENDANCE <...>`             | Add an attendance record                               |
| `APPROVE_OD <id> <sid> <cid> ...`     | Approve an OD                                          |
| `APPROVE_CONDONATION <id> <by> <date>`| Approve a condonation application                      |
| `REJECT_CONDONATION <id>`             | Reject a condonation application                       |
| `SHOW_ATTENDANCE <sid> [date]`        | Print student's raw attendance report                  |
| `SHOW_SECTION_ATTENDANCE <sec> [date]`| Print section summary                                  |
| `SHOW_INELIGIBLE [date]`              | List all students below 75 %                           |
| `GENERATE_REPORT <type> ...`          | Generate ALL/SECTION/INELIGIBLE/CONDONABLE report      |
| `CALENDAR_SUMMARY`                    | Print semester calendar statistics                     |
| `SHOW_CONDONATION <sid> [date]`       | Print full post-condonation report, fees & fines       |
| `SHOW_CONDONATION_SECTION <sec> [date]`| Section-wide condonation, fees and fines summary       |
| `GENERATE_CONDONATION_REPORT <sec/ALL> [date] [file]` | Generate CSV condonation report |
| `EXIT`                                | Stop processing                                        |

---

## Condonation & Fine Business Rules (Part 2)

### Processing Order
1. Read original attendance.
2. Apply approved medical removal.
3. Calculate post-medical conducted counts.
4. Apply duty-day additions (Sports / NCC_NSS / Placement / Bereavement).
5. Evaluate duty caps (10% post-medical per course, 20-day overall for Sports/NCC).
6. Calculate final course-wise and overall attendance.
7. Determine eligibility (Course: >=75% ELIGIBLE, 65–74.99% CONDONABLE, <65% DETAINED).
8. Calculate condonation fees (70–74.99% ₹500, 65–69.99% ₹1,000; max 2 courses).
9. Calculate absence fines (unbroken stretches >= 3 days, ₹100/day from 3rd day, max ₹2,000).

### Condonation Rules Summary
- **MEDICAL**: Removes conducted & attended periods from totals. Capped at 15% of original conducted periods (floor Decimal). Requires `medical_cert_ref`. Must be submitted within 7 days of return; otherwise `REJECTED_LATE`.
- **SPORTS / NCC_NSS**: Duty days counted as present. Overall cap: 20 duty days per semester. Course cap: 10% of post-medical conducted periods.
- **PLACEMENT**: No cap. Final-year students only (semester >= 7). Dates must match placement calendar.
- **BEREAVEMENT**: Max 5 consecutive calendar days. Once per semester. Must be submitted within 3 days; otherwise `REJECTED_LATE`. No certificate required.
- **ELIGIBILITY RESTRICTIONS**: Maximum 2 condonable courses per semester; the 3rd condonable course becomes DETAINED. 3 or more detained courses result in whole-semester DETAINED status.
- **FEES**: ₹500 per course for 70.00%–74.99%; ₹1,000 per course for 65.00%–69.99%. Max 2 courses condoned per semester.
- **FINES**: Unbroken stretches of >= 3 full days absent without approved leave. ₹100/day starting from the 3rd day (e.g. 3 days = ₹100, 4 days = ₹200). Capped at ₹2,000 per semester.


