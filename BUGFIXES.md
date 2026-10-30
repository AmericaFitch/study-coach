# Bug fixes

Every bug found and fixed so far, in the order they were found. Each entry
explains what you would have seen, why it happened, how it was fixed, and how
we know the fix works. Most have a test named after the behavior, so you can
read the test to see the bug pinned down.

| # | Bug | Severity | Found by | Verified by |
|---|---|---|---|---|
| 1 | Long free slot only got one study block | High | Test failure | `test_blocks_split_by_max_length_with_breaks` |
| 2 | Crash when there's no free time | High | Code review, reproduced | `test_no_free_time_with_mixed_due_dates_reports_unscheduled` |
| 3 | Recent announcements missing | High | Code review (Canvas docs) | `test_announcements_send_end_date_so_recent_posts_are_included` |
| 4 | Cancelled events blocked time | Medium | Code review, reproduced | `test_cancelled_events_are_skipped` |
| 5 | Config file readable by other accounts | Medium | Code review, reproduced | `test_config_file_is_private` |
| 6 | Off by an hour across daylight saving | Medium | Code review, reproduced | `test_dst_fall_back_night_counts_the_extra_hour` |
| 7 | Network connections leaked | Low | Code review | Smoke test |
| 8 | Confusing error for an expired token | Low | Code review | `test_expired_token_gives_helpful_error` |
| 9 | Tiny leftover study blocks | Low | Code review, reproduced | `test_no_leftover_sliver` |
| 10 | Priority behavior was undocumented | Low | Code review | `test_undated_tasks_go_after_dated_ones_regardless_of_priority` |
| 11 | Old courses could appear | Low | Code review | `test_courses_request_filters_to_available` |
| 12 | `init` crashed on bad input | Low | Code review | Manual |
| 13 | Profile returned empty fields | Low | Test failure | `test_course_profile_shows_in_student_profile` |
| 14 | Server wouldn't start on Windows | High | Cross-platform review | CI on Windows (once pushed to GitHub) |
| 15 | `init` printed a command Claude can't run | High | Checking item 14 | `test_server_command_is_an_absolute_path_next_to_python` |
| 16 | Server wouldn't start from an iCloud folder | High | Checking item 15 | Manual (`import study_coach` works) |
| 17 | Secret URLs written to Claude Desktop's log | High | Live test of PDF reading | `test_secret_urls_are_not_written_to_the_log` |

---

## 1. A long free slot only got one study block

**What you'd see:** a 150-minute task with a whole free afternoon got one
90-minute block, and the second block landed in a *different* free slot (or
was reported as not fitting), even though the afternoon had plenty of room.

**Why:** the scheduler loop placed one block per free slot and then moved to the
next slot. It never went back to use the rest of the same slot.

**Fix:** an inner `while` loop keeps filling the same slot, with a break between
blocks, until the task is done or the slot runs out.
[`slots.py` — `propose_blocks`](src/study_coach/scheduling/slots.py)

**Lesson:** this was caught by the first test run. Writing tests for the
scheduling math before using it paid off immediately.

## 2. Crash when there's no free time

**What you'd see:** in a packed week with zero free slots, asking for a plan
that mixed tasks *with* and *without* due dates crashed with
`TypeError: '<' not supported between 'datetime' and 'NoneType'`.

**Why:** to sort tasks, undated ones were given a fake "far future" date
borrowed from the first free slot's time zone. With no free slots, the fake
date was `None`, and Python can't compare `None` with a date.

**Fix:** sort by `(has no due date, due date, priority)`. Undated tasks sort last
because `True > False`, and no fake date is needed. The tool now correctly
reports every task as unscheduled.

## 3. Recent announcements were missing

**What you'd see:** asking for announcements from the last 60 days returned
posts from 60–32 days ago and *nothing from the last month*.

**Why:** we sent Canvas a `start_date` but no `end_date`. According to the Canvas
API docs, `end_date` then defaults to 28 days after `start_date`, so the newest
posts fell outside the range.

**Fix:** always send `end_date` set to tomorrow.

**Note:** this one is based on the Canvas documentation. Your real Canvas will
confirm it.

## 4. Cancelled calendar events still blocked time

**What you'd see:** a meeting that was cancelled still showed up as busy, so the
planner avoided time you actually had free.

**Why:** calendar feeds keep cancelled events and mark them `STATUS:CANCELLED`.
The reader didn't check that field.

**Fix:** skip events whose status is `CANCELLED`.
[`ics.py`](src/study_coach/calendars/ics.py)

## 5. The config file was readable by other accounts on the Mac

**What you'd see:** `~/.study-coach/config.yaml` was saved with permissions
`-rw-r--r--` (anyone on the computer can read it).

**Why it matters:** the config holds your calendar feed URLs. Those are secret
links: anyone who has one can read your whole calendar.

**Fix:** save the file with permissions `600` (only you can read or write it),
and its folder with `700`. On Windows, Python can't set these, but user folders
there are already private by default.

## 6. Time math was off by an hour around daylight saving

**What you'd see:** study hours of 10pm–3am on the night clocks fall back
(Nov 1, 2026) were counted as 5 hours. They're really 6.

**Why:** a subtle Python rule. When two datetimes share the same time zone
object, Python subtracts and compares their *clock* times and ignores the DST
shift between them.

**Fix:** do all scheduling math in UTC (which has no DST) and convert to your
local time only when showing results. The docstring at the top of
[`slots.py`](src/study_coach/scheduling/slots.py) explains this for future readers.

**Interview tip:** this is a classic bug. "Store and compute in UTC, display in
local time" is a rule worth being able to explain.

## 7. Network connections leaked

**What you'd see:** nothing at first. Over a long Claude session, open
connections would pile up.

**Why:** every tool call created a new HTTP client and never closed it.

**Fix:** one shared client for the life of the server, which also makes calls
faster because connections get reused.
[`server.py` — `_http`](src/study_coach/server.py)

## 8. An expired token gave a confusing error

**What you'd see:** Claude reports a raw `401 Unauthorized` and can't tell you
what to do.

**Fix:** `_check()` in [`canvas.py`](src/study_coach/lms/canvas.py) turns 401 into
"Canvas rejected the token… create a new one under Account → Settings" and 403
into a clear "access denied" message. Useful if your school limits tokens to 30 days.

## 9. Tiny leftover study blocks

**What you'd see:** a 100-minute task became a 90-minute block plus a pointless
10-minute block.

**Fix:** when a split would leave less than the minimum block length, split
evenly enough that both pieces are useful: 70 + 30 instead of 90 + 10.

## 10. Priority behavior was undocumented

**Not a crash, but a surprise:** a priority-1 task with no due date was still
scheduled after every task that had a due date. That's intentional (earliest
deadline first), but nothing told Claude.

**Fix:** the tool description now says priority only breaks ties and undated
tasks go last, so Claude knows to give urgent work a due date.

## 11. Old courses could appear

**Why:** `enrollment_state=active` can include past-term courses that were never
formally closed, depending on how a school sets up Canvas.

**Fix:** also request `state[]=available`. Your real Canvas account will show
whether any old courses still slip through.

## 12. `init` crashed on bad input

**What you'd see:** typing `ninety` for the longest study block crashed the
wizard, and rerunning `init` with the same calendar URL added it twice.

**Fix:** the wizard asks again until it gets a valid number or `HH:MM` time,
and skips feed URLs it already has.

## 13. The profile returned empty fields

**What you'd see:** `get_student_profile` returned things like `"nickname": null`
for courses without a nickname. Harmless, but it wastes Claude's context.

**Fix:** leave out empty fields. Caught by a new test.

## 14. The server wouldn't start on Windows

**What you'd see:** on Windows, the server crashes immediately with
`ZoneInfoNotFoundError: 'No time zone found with key America/Chicago'`.

**Why:** Python's `zoneinfo` reads time zone data from the operating system.
macOS and Linux include it; Windows doesn't.

**Fix:** install the `tzdata` package on Windows only:
`"tzdata; sys_platform == 'win32'"` in [`pyproject.toml`](pyproject.toml). The
`; sys_platform == ...` part is a standard "environment marker" that installs a
dependency only on matching systems.

**Verified by:** not yet, since there's no Windows machine here. The GitHub Actions
workflow in [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs all
tests on Windows, macOS and Linux once the repo is pushed to GitHub.

## 15. `init` printed a command Claude Desktop can't run

**What you'd see:** the config snippet `init` printed said
`"command": "study-coach"` instead of the full path, and Claude Desktop
couldn't start the server.

**Why:** `init` looked up its own command by searching your `PATH`. If you run
it by full path without "activating" the virtual environment (the way the
setup steps say to), the `.venv` folder isn't on `PATH`, so the search found
nothing.

**Fix:** `server_command()` in [`cli.py`](src/study_coach/cli.py) looks next to
the Python interpreter that's running, where pip always puts the command
(`.venv/bin` on Mac, `.venv\Scripts` on Windows).

## 16. The server wouldn't start from an iCloud-synced folder

**What you'd see:** `ModuleNotFoundError: No module named 'study_coach'`, even
though everything was installed.

**Why:** two things combined:
1. Your Desktop syncs to iCloud, and macOS set its "hidden" flag on files in `.venv`.
2. Starting with Python 3.13, Python skips hidden `.pth` files for security.
   A `.pth` file is how an editable install (`pip install -e .`) tells Python
   where your source code lives. With it skipped, Python couldn't find the project.

**Fix (for now):** `chflags -R nohidden .venv` removes the flag. iCloud may set
it again later. A permanent fix is to keep the project in a folder iCloud
doesn't sync. The README's Troubleshooting section covers this.

**Lesson:** the tests had passed in this folder earlier, so the flag was probably
set *after* installation, by iCloud. Bugs that depend on the environment, not the
code, are why "it works on my machine" isn't enough, and why CI runs on clean
machines.

## 17. Secret URLs were written to Claude Desktop's log file

**What you'd see:** lines like `HTTP Request: GET https://...` in
`~/Library/Logs/Claude/mcp-server-study-coach.log`, one for every request the
server made, including your private calendar feed URL.

**Why it matters:** a calendar feed URL works like a password, because anyone
who has it can read your whole calendar (see #5). Canvas file download links
are also signed and short-lived. Logs get pasted into bug reports and support
chats, so secrets don't belong in them.

**Why:** two libraries combined:
1. `httpx` logs every request's full URL at the `INFO` level.
2. The MCP SDK turns on `INFO` logging and sends it to stderr, which Claude
   Desktop saves to that log file.

The Canvas token itself was never logged, because it travels in a header, not
in the URL.

**Fix:** set the `httpx` logger to `WARNING` when the server starts
([`server.py`](src/study_coach/server.py)), so routine requests aren't logged
but real problems still are.

**Verified by:** a test that makes a request with a fake secret URL and checks
the log. It fails without the fix and passes with it. A live run against Canvas
logged 0 request lines afterward.

**Lesson:** found by watching the output of a real run, not by the tests.
Check what your program writes to logs, not just what it returns.

---

## Not a bug: Windows backslashes in the config snippet

I worried that Windows paths printed by `init` would break the JSON config,
because backslashes must be doubled in JSON (`C:\\Users\\...`). Python's
`json.dumps` already does this automatically, so no fix was needed.
