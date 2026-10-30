# Study Coach

An MCP server that gives Claude read access to your Canvas courses and your
calendar, so it can build study plans tailored to your grades, deadlines and
schedule.

Only Canvas is required. Calendar feeds, Notion and calendar booking are optional.

## What Claude can do with it

| Tool | What it does |
|---|---|
| `list_courses` | Active courses, current scores, and your notes on each |
| `get_upcoming_assignments` | What's due in the next N days (or already past, with `include_past`), with files the instructions link to |
| `get_grades` | Scores per assignment for a course |
| `get_submission_feedback` | Instructor comments and rubric scores |
| `get_syllabus` | Syllabus as plain text, with linked files |
| `list_announcements` | Recent announcements, with attached and linked files |
| `list_course_pages`, `get_course_page` | Course pages from Modules (notes, instructions) and the files they link |
| `list_course_files` | Every file a course links, from Modules, syllabus, assignments, pages and announcements, each tagged with where it's linked (works even when the Files tab is hidden) |
| `read_pdf` | Text of a course PDF, page by page, without you uploading it |
| `view_pdf_pages` | Images of PDF pages, for diagrams, charts and scanned handouts |
| `get_calendar_events` | Events from your calendar feeds |
| `get_availability` | Free study time after classes, work and events |
| `propose_study_blocks` | Fits tasks into free time, earliest deadline first (a draft, nothing is booked) |
| `export_plan_ics` | Saves an approved plan as an .ics file for any calendar app |
| `get_student_profile` | Your study preferences and course notes |
| `update_schedule_preferences`, `add_blackout`, `remove_blackout`, `update_course_profile` | Personalize by chatting ("I work Tue/Thu 5–9") |

There is also a `plan_my_week` prompt that runs the whole flow.

Claude decides **what** to study and for how long. The server's code decides
**when**, because language models are unreliable at time arithmetic.

## Setup

Requires Python 3.10+. Works on macOS and Windows.

**macOS / Linux**

```bash
git clone <this repo> && cd study-coach
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/study-coach init
```

**Windows (PowerShell)**

```powershell
git clone <this repo>; cd study-coach
py -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\study-coach init
```

`init` asks for your Canvas URL, time zone, study hours and an optional
calendar feed URL, writes your config file, and prints the exact snippet
(with the correct full path for your machine) to add to Claude Desktop.

| | macOS | Windows |
|---|---|---|
| Your settings | `~/.study-coach/config.yaml` | `C:\Users\<you>\.study-coach\config.yaml` |
| Claude Desktop config | `~/Library/Application Support/Claude/claude_desktop_config.json` | `%APPDATA%\Claude\claude_desktop_config.json` |

### Canvas token
In Canvas: **Account → Settings → New Access Token**. Put it in the `env`
section of the Claude Desktop config as `CANVAS_TOKEN`. It's never written to
`config.yaml`.

### Calendar feed (optional)
- Google Calendar: Settings → your calendar → **Secret address in iCal format**
- Canvas: Calendar → **Calendar Feed**

Add URLs under `calendar_feeds` in the config, or paste one during `init`.

### Claude Desktop

```json
{
  "mcpServers": {
    "study-coach": {
      "command": "/full/path/to/study-coach/.venv/bin/study-coach",
      "env": { "CANVAS_TOKEN": "your-token" }
    }
  }
}
```

On Windows the command is `...\\.venv\\Scripts\\study-coach.exe`. Backslashes
must be doubled in JSON; the snippet `init` prints already does this.

Restart Claude Desktop, then try: *"Use the plan_my_week prompt."*

### Troubleshooting

- **`No module named 'study_coach'` on macOS with Python 3.13+.** Python skips
  `.pth` files that carry macOS's "hidden" flag, and iCloud-synced folders
  (Desktop, Documents) can set it inside `.venv`. Fix with
  `chflags -R nohidden .venv`, or keep the project in a folder iCloud doesn't sync.

## Personalization

Everything personal lives in `~/.study-coach/config.yaml` (see
`config.example.yaml`). You rarely need to edit it by hand. Tell Claude things
like "never schedule before 10am" or "Regression is my hardest class" and it
saves them with the personalization tools.

Set `STUDY_COACH_CONFIG` to use a different config file.

## Privacy

- Read-only access to Canvas. Nothing is submitted or posted.
- Your token stays on your machine and is only sent to your school's Canvas.
- On macOS/Linux the config file is readable only by your account (it can hold
  secret calendar feed URLs). On Windows, your user folder is already private
  to your account by default.
- Tool results (grades, feedback) are sent to Claude as part of the conversation.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

```
src/study_coach/
  config.py        personal settings (pydantic + YAML)
  lms/             LMS interface + Canvas client (paginated, read-only)
  calendars/       ICS feed reader
  scheduling/      free-slot finding and block placement (pure functions)
  server.py        MCP tools and prompts
  cli.py           `study-coach` / `study-coach init`
```

Adding another LMS means implementing `LmsProvider` in `lms/`. The MCP layer
stays the same.
