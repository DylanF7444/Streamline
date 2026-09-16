# Personal Hub import templates

Personal Hub can add local tasks or local, read-only calendar items from a UTF-8 CSV file
or a pasted text list. The desktop import dialog can save the header-only CSV templates:

- [Task CSV template](PERSONAL_HUB_TASK_IMPORT_TEMPLATE.csv)
- [Calendar CSV template](PERSONAL_HUB_CALENDAR_IMPORT_TEMPLATE.csv)

## Task CSV

Use one row per task. `title` is required; the other columns are optional.

```csv
id,title,notes,due_date,due_time,all_day,timezone,priority,list,tags,reminders
task-1,Submit report,"Add the final numbers",2026-09-14,15:30,false,America/Chicago,high,Work,"release,finance","60,0"
```

Dates use `YYYY-MM-DD`; times use `HH:MM`. If a due time is omitted, the task is all-day.
`priority` is `none`, `low`, `normal`, or `high`. Tags and reminders are comma-separated;
reminders are minutes before the due date. An `id` makes later imports update the same task;
without one, the row identity is derived from its title, notes, and due date.

## Calendar CSV

Use one row per local calendar item. `title` and either `start_date` or `start_at` are
required. Calendar items are read-only in Personal Hub and are never written to a connected
Google, Outlook, Canvas, or iCalendar source.

```csv
id,title,start_date,start_time,end_date,end_time,all_day,timezone,location,description,url
event-1,Planning,2026-09-14,10:00,2026-09-14,11:00,false,America/Chicago,Room 4,Agenda,https://example.test/planning
```

For an all-day item, set `all_day` to `true`; the end date defaults to the following day.
For timed items without an end, Personal Hub uses the duration selected in the import dialog.
Only ordinary HTTP(S) URLs without credentials are accepted.

## Text lists

For tasks, paste one title per line. Blank lines and lines beginning with `#` are ignored;
common `- `, `* `, and checklist markers are accepted.

For calendar items, a line can optionally start with a date or date/time followed by `|`:

```text
2026-09-14 10:00 | Planning
2026-09-15 | Company holiday
Loose event uses the dialog's default date and time
```

The calendar import dialog supplies the default date, start time, duration, and all-day
setting for rows without a prefix. Imports are local, bounded to 5,000 rows and 2 MB, and
invalid rows are reported without sending content over the network.
