"""Bounded CSV and plain-text list imports for Personal Hub.

Imports are deliberately local and deterministic.  A CSV file is treated as a
small, user-selected data document rather than as a spreadsheet formula source:
there is no formula evaluation, network access, or arbitrary expression parsing.
The parser returns valid rows together with bounded row-level issues so the UI
can explain partial imports without exposing an exception or silently accepting
misspelled columns.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ImportTarget = Literal["tasks", "calendar"]
ImportFormat = Literal["csv", "text"]

MAX_IMPORT_BYTES: Final[int] = 2 * 1024 * 1024
MAX_IMPORT_ROWS: Final[int] = 5_000
MAX_IMPORT_COLUMNS: Final[int] = 32
MAX_IMPORT_FIELD_LENGTH: Final[int] = 20_000
MAX_IMPORT_NAME_LENGTH: Final[int] = 160
DEFAULT_CALENDAR_DURATION_MINUTES: Final[int] = 60
DEFAULT_CALENDAR_START_TIME: Final[time] = time(9, 0)

TASK_IMPORT_HEADERS: Final[tuple[str, ...]] = (
    "id",
    "title",
    "notes",
    "due_date",
    "due_time",
    "all_day",
    "timezone",
    "priority",
    "list",
    "tags",
    "reminders",
)
CALENDAR_IMPORT_HEADERS: Final[tuple[str, ...]] = (
    "id",
    "title",
    "start_date",
    "start_time",
    "end_date",
    "end_time",
    "all_day",
    "timezone",
    "location",
    "description",
    "url",
)

_TASK_FIELDS: Final[frozenset[str]] = frozenset(
    {
        *TASK_IMPORT_HEADERS,
        "due_at",
    }
)
_CALENDAR_FIELDS: Final[frozenset[str]] = frozenset(
    {
        *CALENDAR_IMPORT_HEADERS,
        "start_at",
        "end_at",
    }
)
_HEADER_ALIASES: Final[dict[str, str]] = {
    "key": "id",
    "uid": "id",
    "name": "title",
    "summary": "title",
    "details": "description",
    "body": "description",
    "due": "due_date",
    "due_date_time": "due_at",
    "start": "start_at",
    "start_datetime": "start_at",
    "end": "end_at",
    "end_datetime": "end_at",
    "list_name": "list",
    "reminder": "reminders",
}
_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "t", "yes", "y"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "f", "no", "n"})
_PRIORITIES: Final[frozenset[str]] = frozenset({"none", "low", "normal", "high"})


class ImportFileError(ValueError):
    """A selected import file could not be read within the local bounds."""


@dataclass(frozen=True, slots=True)
class ImportIssue:
    """A safe, row-scoped parser issue suitable for display in the UI."""

    row_number: int
    message: str


@dataclass(frozen=True, slots=True)
class TaskImportItem:
    """One normalized task row ready for the Hub task repository."""

    stable_key: str
    title: str
    notes: str = ""
    due_at: datetime | None = None
    due_is_all_day: bool = False
    timezone: str = "UTC"
    priority: str = "normal"
    list_name: str = ""
    tags: tuple[str, ...] = ()
    reminder_offsets_minutes: tuple[int, ...] = ()
    row_number: int = 0


@dataclass(frozen=True, slots=True)
class CalendarImportItem:
    """One normalized local calendar event row."""

    stable_key: str
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    timezone: str = "UTC"
    location: str = ""
    description: str = ""
    deep_link: str = ""
    row_number: int = 0


ImportItem = TaskImportItem | CalendarImportItem


@dataclass(frozen=True, slots=True)
class ImportParseResult:
    """Normalized parser output, including rows that were rejected safely."""

    target: ImportTarget
    source_format: ImportFormat
    items: tuple[ImportItem, ...] = ()
    issues: tuple[ImportIssue, ...] = ()

    @property
    def rows(self) -> tuple[ImportItem, ...]:
        return self.items

    @property
    def errors(self) -> tuple[ImportIssue, ...]:
        return self.issues

    @property
    def task_items(self) -> tuple[TaskImportItem, ...]:
        return tuple(item for item in self.items if isinstance(item, TaskImportItem))

    @property
    def calendar_items(self) -> tuple[CalendarImportItem, ...]:
        return tuple(item for item in self.items if isinstance(item, CalendarImportItem))


@dataclass(frozen=True, slots=True)
class BulkImportResult:
    """Safe summary returned after valid rows have been persisted."""

    target: ImportTarget
    source_format: ImportFormat
    source_name: str
    imported_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    issues: tuple[ImportIssue, ...] = ()
    import_id: str | None = None

    @property
    def created_count(self) -> int:
        return self.imported_count

    @property
    def error_count(self) -> int:
        return len(self.issues)

    @property
    def total_processed(self) -> int:
        return self.imported_count + self.updated_count + self.skipped_count


def normalize_import_target(value: str) -> ImportTarget:
    """Validate the two supported import destinations."""

    if not isinstance(value, str):
        raise TypeError("import target must be text")
    normalized = value.strip().casefold()
    if normalized not in {"tasks", "calendar"}:
        raise ValueError("import target must be tasks or calendar")
    return cast(ImportTarget, normalized)


def normalize_import_name(value: str | None, *, fallback: str = "Import") -> str:
    """Return a bounded, single-line local import label."""

    candidate = fallback if value is None else value
    if not isinstance(candidate, str):
        raise TypeError("import name must be text")
    normalized = " ".join(str(candidate).split())
    if not normalized:
        normalized = fallback
    if len(normalized) > MAX_IMPORT_NAME_LENGTH:
        raise ValueError("import name is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("import name contains control characters")
    return normalized


def read_import_file(path: Path | str) -> str:
    """Read one explicitly selected UTF-8 CSV/text file within the import limit."""

    selected = Path(path).expanduser().resolve(strict=False)
    if not selected.is_file():
        raise ImportFileError("The selected import file does not exist.")
    try:
        payload = selected.read_bytes()
    except OSError as error:
        raise ImportFileError("The selected import file could not be read safely.") from error
    if len(payload) > MAX_IMPORT_BYTES:
        raise ImportFileError("Import files must be 2 MB or smaller.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ImportFileError("Import files must use UTF-8 text encoding.") from error
    try:
        _reject_control_text(text, "import file")
    except ValueError as error:
        raise ImportFileError("The selected import file contains unsupported text.") from error
    return text


def task_template_csv() -> str:
    """Return the header-only task CSV template."""

    return _template_csv(TASK_IMPORT_HEADERS)


def calendar_template_csv() -> str:
    """Return the header-only local-calendar CSV template."""

    return _template_csv(CALENDAR_IMPORT_HEADERS)


def template_csv(target: ImportTarget | str) -> str:
    """Return the header-only template for a selected destination."""

    normalized = normalize_import_target(target)
    return task_template_csv() if normalized == "tasks" else calendar_template_csv()


def text_list_instructions(target: ImportTarget | str) -> str:
    """Return concise instructions shown beside the pasted text-list editor."""

    normalized = normalize_import_target(target)
    if normalized == "tasks":
        return "Enter one task title per line. Blank lines are ignored."
    return (
        "Enter one event per line. Use ‘YYYY-MM-DD HH:MM | title’ for a row-specific time; "
        "otherwise the default date and time below are used."
    )


def parse_csv(
    content: str,
    *,
    target: ImportTarget | str,
    default_timezone: str = "UTC",
    default_calendar_date: date | None = None,
    default_start_time: time | str | None = None,
    default_duration_minutes: int = DEFAULT_CALENDAR_DURATION_MINUTES,
    default_all_day: bool = False,
    max_rows: int = MAX_IMPORT_ROWS,
) -> ImportParseResult:
    """Parse a bounded CSV document into task or local-calendar rows."""

    normalized_target = normalize_import_target(target)
    _validate_content(content, max_rows=max_rows)
    try:
        timezone_name, _ = _zone(default_timezone)
    except ValueError as error:
        return ImportParseResult(
            target=normalized_target,
            source_format="csv",
            issues=(ImportIssue(1, str(error)),),
        )
    try:
        if default_calendar_date is not None and type(default_calendar_date) is not date:
            raise ValueError("default calendar date must be a date")
        if type(default_all_day) is not bool:
            raise ValueError("default all-day setting must be true or false")
        selected_time = (
            _parse_time_value(
                default_start_time,
                field_name="default calendar time",
                allow_empty=True,
            )
            or DEFAULT_CALENDAR_START_TIME
        )
        duration = _duration_minutes(default_duration_minutes)
    except ValueError as error:
        return ImportParseResult(
            target=normalized_target,
            source_format="csv",
            issues=(ImportIssue(1, str(error)),),
        )
    reader = csv.reader(io.StringIO(content, newline=""), strict=True)
    try:
        raw_headers = next(reader, None)
    except csv.Error:
        raw_headers = None
    if not raw_headers:
        return ImportParseResult(
            target=normalized_target,
            source_format="csv",
            issues=(ImportIssue(1, "The CSV needs a header row."),),
        )
    issues: list[ImportIssue] = []
    if len(raw_headers) > MAX_IMPORT_COLUMNS:
        issues.append(ImportIssue(1, f"CSV has more than {MAX_IMPORT_COLUMNS} columns."))
    headers: list[str] = []
    seen_headers: set[str] = set()
    for header in raw_headers[:MAX_IMPORT_COLUMNS]:
        normalized_header = _normalize_header(header)
        normalized_header = _HEADER_ALIASES.get(normalized_header, normalized_header)
        if not normalized_header:
            issues.append(ImportIssue(1, "CSV headers cannot be blank."))
            headers.append("")
            continue
        if normalized_header in seen_headers:
            issues.append(ImportIssue(1, f"CSV header is duplicated: {normalized_header}."))
        seen_headers.add(normalized_header)
        headers.append(normalized_header)
    allowed = _TASK_FIELDS if normalized_target == "tasks" else _CALENDAR_FIELDS
    unknown = sorted({header for header in headers if header and header not in allowed})
    if unknown:
        issues.append(ImportIssue(1, "Unknown CSV column(s): " + ", ".join(unknown) + "."))
    required = "title"
    if required not in headers:
        issues.append(ImportIssue(1, "CSV must include a title column."))
    if normalized_target == "calendar" and not ("start_date" in headers or "start_at" in headers):
        issues.append(ImportIssue(1, "Calendar CSV must include start_date or start_at."))

    items: list[ImportItem] = []
    stable_key_counts: dict[str, int] = {}
    row_count = 0
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            issues.append(ImportIssue(row_count + 2, "The CSV contains malformed quoting."))
            break
        row_count += 1
        row_number = row_count + 1
        if row_count > max_rows:
            issues.append(ImportIssue(row_number, f"Import is limited to {max_rows} rows."))
            break
        if len(row) > MAX_IMPORT_COLUMNS:
            issues.append(
                ImportIssue(row_number, f"Row has more than {MAX_IMPORT_COLUMNS} columns.")
            )
            continue
        if not any(value.strip() for value in row):
            continue
        try:
            values = {
                header: cell
                for index, header in enumerate(headers)
                if header and index < len(row)
                for cell in (_bounded_cell(row[index], header),)
            }
            item = (
                _parse_task_row(values, row_number, timezone_name)
                if normalized_target == "tasks"
                else _parse_calendar_row(
                    values,
                    row_number,
                    timezone_name,
                    default_calendar_date=default_calendar_date,
                    default_start_time=selected_time,
                    default_duration_minutes=duration,
                    default_all_day=default_all_day,
                )
            )
        except ValueError as error:
            issues.append(ImportIssue(row_number, str(error)))
            continue
        item = _unique_item_key(item, stable_key_counts)
        items.append(item)
    return ImportParseResult(
        target=normalized_target,
        source_format="csv",
        items=tuple(items),
        issues=tuple(issues[:MAX_IMPORT_ROWS]),
    )


def parse_text_list(
    content: str,
    *,
    target: ImportTarget | str,
    default_timezone: str = "UTC",
    default_calendar_date: date | None = None,
    default_start_time: time | str | None = None,
    default_duration_minutes: int = DEFAULT_CALENDAR_DURATION_MINUTES,
    default_all_day: bool = False,
    max_rows: int = MAX_IMPORT_ROWS,
) -> ImportParseResult:
    """Parse one title per line, with optional date prefixes for calendar rows."""

    normalized_target = normalize_import_target(target)
    _validate_content(content, max_rows=max_rows)
    try:
        if default_calendar_date is not None and type(default_calendar_date) is not date:
            raise ValueError("default calendar date must be a date")
        if type(default_all_day) is not bool:
            raise ValueError("default all-day setting must be true or false")
        timezone_name, _ = _zone(default_timezone)
        selected_date = default_calendar_date or datetime.now(ZoneInfo(timezone_name)).date()
        selected_time = (
            _parse_time_value(
                default_start_time,
                field_name="default calendar time",
                allow_empty=True,
            )
            or DEFAULT_CALENDAR_START_TIME
        )
        duration = _duration_minutes(default_duration_minutes)
    except ValueError as error:
        return ImportParseResult(
            target=normalized_target,
            source_format="text",
            issues=(ImportIssue(1, str(error)),),
        )
    items: list[ImportItem] = []
    issues: list[ImportIssue] = []
    stable_key_counts: dict[str, int] = {}
    nonempty_rows = 0
    for row_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        nonempty_rows += 1
        if nonempty_rows > max_rows:
            issues.append(ImportIssue(row_number, f"Import is limited to {max_rows} rows."))
            break
        try:
            title = _clean_text(_strip_list_marker(line), "title", 240, collapse=True)
            if normalized_target == "tasks":
                item: ImportItem = TaskImportItem(
                    stable_key=_stable_key("task", title),
                    title=title,
                    timezone=timezone_name,
                    row_number=row_number,
                )
            else:
                parsed_date, parsed_time, parsed_title, prefixed_all_day = _calendar_text_prefix(
                    line
                )
                title = _clean_text(parsed_title, "title", 240, collapse=True)
                is_all_day = prefixed_all_day if parsed_date is not None else default_all_day
                start_date = parsed_date or selected_date
                start_time = parsed_time or selected_time
                starts_at, ends_at = _calendar_window(
                    start_date=start_date,
                    start_time=start_time,
                    end_date=None,
                    end_time=None,
                    all_day=is_all_day,
                    timezone_name=timezone_name,
                    default_duration_minutes=duration,
                    start_at=None,
                    end_at=None,
                )
                item = CalendarImportItem(
                    stable_key=_stable_key(
                        "calendar",
                        title,
                        starts_at.isoformat(),
                        ends_at.isoformat(),
                    ),
                    title=title,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    all_day=is_all_day,
                    timezone=timezone_name,
                    row_number=row_number,
                )
            items.append(_unique_item_key(item, stable_key_counts))
        except ValueError as error:
            issues.append(ImportIssue(row_number, str(error)))
    return ImportParseResult(
        target=normalized_target,
        source_format="text",
        items=tuple(items),
        issues=tuple(issues[:MAX_IMPORT_ROWS]),
    )


def parse_import(
    content: str,
    *,
    target: ImportTarget | str,
    source_format: ImportFormat | str,
    **options: object,
) -> ImportParseResult:
    """Dispatch to the explicit CSV or text-list parser."""

    if not isinstance(source_format, str):
        raise TypeError("import format must be text")
    normalized_format = source_format.strip().casefold()
    if normalized_format == "csv":
        return parse_csv(content, target=target, **options)  # type: ignore[arg-type]
    if normalized_format in {"text", "txt", "list"}:
        return parse_text_list(content, target=target, **options)  # type: ignore[arg-type]
    raise ValueError("import format must be csv or text")


def _template_csv(headers: tuple[str, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    return output.getvalue()


def _validate_content(content: str, *, max_rows: int) -> None:
    if not isinstance(content, str):
        raise TypeError("import content must be text")
    if not 1 <= max_rows <= MAX_IMPORT_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_IMPORT_ROWS}")
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("Import files must be 2 MB or smaller.")
    _reject_control_text(content, "import content")


def _reject_control_text(value: str, field_name: str) -> None:
    if "\x00" in value or any(
        (ord(character) < 32 and character not in {"\n", "\r", "\t"}) or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"{field_name} contains unsupported control characters.")


def _normalize_header(value: str) -> str:
    normalized = "_".join(value.strip().casefold().replace("-", "_").split())
    return normalized[:MAX_IMPORT_NAME_LENGTH]


def _bounded_cell(value: str, field_name: str) -> str:
    _reject_control_text(value, f"CSV {field_name} field")
    if len(value) > MAX_IMPORT_FIELD_LENGTH:
        raise ValueError(f"CSV {field_name} field is too long.")
    return value.strip()


def _value(row: Mapping[str, str], field_name: str) -> str:
    return row.get(field_name, "").strip()


def _clean_text(value: str, field_name: str, limit: int, *, collapse: bool) -> str:
    _reject_control_text(value, field_name)
    normalized = " ".join(value.split()) if collapse else value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be blank.")
    if len(normalized) > limit:
        raise ValueError(f"{field_name} is too long.")
    return normalized


def _parse_task_row(
    row: Mapping[str, str], row_number: int, default_timezone: str
) -> TaskImportItem:
    title = _clean_text(_value(row, "title"), "title", 240, collapse=True)
    timezone_name, zone = _zone(_value(row, "timezone") or default_timezone)
    due_at, due_is_all_day = _task_due(row, zone)
    priority = (_value(row, "priority") or "normal").casefold()
    if priority not in _PRIORITIES:
        raise ValueError("priority must be none, low, normal, or high.")
    tags = _tags(_value(row, "tags"))
    reminders = _reminders(_value(row, "reminders"))
    if reminders and due_at is None:
        raise ValueError("reminders require a due date.")
    notes = _clean_optional(_value(row, "notes"), "notes", 20_000, collapse=False)
    list_name = _clean_optional(_value(row, "list"), "list", 160, collapse=True)
    stable_key = _row_key(
        "task",
        _value(row, "id"),
        title,
        notes,
        "" if due_at is None else due_at.isoformat(),
    )
    return TaskImportItem(
        stable_key=stable_key,
        title=title,
        notes=notes,
        due_at=due_at,
        due_is_all_day=due_is_all_day,
        timezone=timezone_name,
        priority=priority,
        list_name=list_name,
        tags=tags,
        reminder_offsets_minutes=reminders,
        row_number=row_number,
    )


def _parse_calendar_row(
    row: Mapping[str, str],
    row_number: int,
    default_timezone: str,
    *,
    default_calendar_date: date | None,
    default_start_time: time,
    default_duration_minutes: int,
    default_all_day: bool,
) -> CalendarImportItem:
    title = _clean_text(_value(row, "title"), "title", 240, collapse=True)
    timezone_name, _ = _zone(_value(row, "timezone") or default_timezone)
    raw_all_day = _value(row, "all_day")
    all_day = (
        _parse_bool(raw_all_day, "all_day", default=default_all_day)
        if raw_all_day
        else default_all_day
    )
    starts_at, ends_at = _calendar_window(
        start_date=_parse_date_value(_value(row, "start_date"), "start_date")
        if _value(row, "start_date")
        else default_calendar_date,
        start_time=_parse_time_value(
            _value(row, "start_time"), field_name="start_time", allow_empty=True
        )
        or default_start_time,
        end_date=(
            _parse_date_value(_value(row, "end_date"), "end_date")
            if _value(row, "end_date")
            else None
        ),
        end_time=_parse_time_value(
            _value(row, "end_time"), field_name="end_time", allow_empty=True
        ),
        all_day=all_day,
        timezone_name=timezone_name,
        default_duration_minutes=default_duration_minutes,
        start_at=_value(row, "start_at") or None,
        end_at=_value(row, "end_at") or None,
    )
    location = _clean_optional(_value(row, "location"), "location", 512, collapse=True)
    description = _clean_optional(_value(row, "description"), "description", 20_000, collapse=False)
    deep_link = _safe_url(_value(row, "url"))
    stable_key = _row_key(
        "calendar",
        _value(row, "id"),
        title,
        starts_at.isoformat(),
        ends_at.isoformat(),
    )
    return CalendarImportItem(
        stable_key=stable_key,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        timezone=timezone_name,
        location=location,
        description=description,
        deep_link=deep_link,
        row_number=row_number,
    )


def _task_due(row: Mapping[str, str], zone: ZoneInfo) -> tuple[datetime | None, bool]:
    raw_due_at = _value(row, "due_at")
    raw_date = _value(row, "due_date")
    raw_time = _value(row, "due_time")
    raw_all_day = _value(row, "all_day")
    all_day_value = _parse_bool(raw_all_day, "all_day", default=False) if raw_all_day else None
    if raw_due_at:
        due = _parse_datetime_value(raw_due_at, "due_at", zone)
        if all_day_value:
            local_date = due.astimezone(zone).date()
            due = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)
        return due, bool(all_day_value)
    if not raw_date:
        if raw_time:
            raise ValueError("due_time requires due_date.")
        return None, False
    due_date = _parse_date_value(raw_date, "due_date")
    parsed_time = _parse_time_value(raw_time, field_name="due_time", allow_empty=True)
    is_all_day = parsed_time is None if all_day_value is None else all_day_value
    selected_time = time.min if is_all_day else parsed_time or DEFAULT_CALENDAR_START_TIME
    due = datetime.combine(due_date, selected_time, tzinfo=zone).astimezone(UTC)
    return due, is_all_day


def _calendar_window(
    *,
    start_date: date | None,
    start_time: time,
    end_date: date | None,
    end_time: time | None,
    all_day: bool,
    timezone_name: str,
    default_duration_minutes: int,
    start_at: str | None,
    end_at: str | None,
) -> tuple[datetime, datetime]:
    _, zone = _zone(timezone_name)
    if start_at:
        starts = _parse_datetime_value(start_at, "start_at", zone)
    else:
        if start_date is None:
            raise ValueError("calendar rows require start_date or start_at.")
        starts = datetime.combine(
            start_date,
            time.min if all_day else start_time,
            tzinfo=zone,
        ).astimezone(UTC)
    if end_at:
        ends = _parse_datetime_value(end_at, "end_at", zone)
    elif all_day:
        selected_end_date = end_date or (starts.astimezone(zone).date() + timedelta(days=1))
        ends = datetime.combine(selected_end_date, time.min, tzinfo=zone).astimezone(UTC)
    elif end_date is not None and end_time is None:
        raise ValueError("timed calendar rows need end_time when end_date is provided.")
    elif end_date is not None:
        ends = datetime.combine(end_date, end_time, tzinfo=zone).astimezone(UTC)
    else:
        ends = starts + timedelta(minutes=default_duration_minutes)
    if ends <= starts:
        raise ValueError("calendar end must be after calendar start.")
    return starts, ends


def _calendar_text_prefix(line: str) -> tuple[date | None, time | None, str, bool]:
    if "|" not in line:
        return None, None, line, False
    prefix, title = line.split("|", 1)
    prefix = prefix.strip()
    if not prefix or not title.strip():
        return None, None, line, False
    try:
        parsed_date = _parse_date_value(prefix, "calendar list date")
    except ValueError:
        try:
            parsed_datetime = _parse_datetime_value(prefix, "calendar list date", ZoneInfo("UTC"))
        except ValueError:
            return None, None, line, False
        return parsed_datetime.date(), parsed_datetime.time().replace(tzinfo=None), title, False
    return parsed_date, None, title, True


def _parse_bool(value: str, field_name: str, *, default: bool) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return default
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{field_name} must be true or false.")


def _parse_date_value(value: str, field_name: str) -> date:
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        for pattern in ("%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(normalized, pattern).date()
            except ValueError:
                continue
    raise ValueError(f"{field_name} must use YYYY-MM-DD.")


def _parse_time_value(
    value: time | str | None,
    *,
    field_name: str,
    allow_empty: bool,
) -> time | None:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    normalized = "" if value is None else str(value).strip()
    if not normalized:
        return None if allow_empty else (_raise_value(f"{field_name} is required."))
    try:
        return time.fromisoformat(normalized).replace(tzinfo=None)
    except ValueError as error:
        raise ValueError(f"{field_name} must use HH:MM or HH:MM:SS.") from error


def _parse_datetime_value(value: str, field_name: str, zone: ZoneInfo) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{field_name} must use an ISO date and time.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def _zone(value: str) -> tuple[str, ZoneInfo]:
    normalized = value.strip()
    if not normalized:
        raise ValueError("timezone cannot be blank.")
    try:
        return normalized, ZoneInfo(normalized)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown timezone: {normalized}.") from error


def _duration_minutes(value: int) -> int:
    if type(value) is not int or not 5 <= value <= 1_440:
        raise ValueError("calendar duration must be between 5 and 1440 minutes.")
    return value


def _tags(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    result: list[str] = []
    for candidate in value.replace(";", ",").split(","):
        normalized = candidate.strip().casefold()
        if not normalized:
            continue
        if (
            len(normalized) > 32
            or not normalized[0].isalnum()
            or any(not (character.isalnum() or character in "_-") for character in normalized)
        ):
            raise ValueError("tags must be simple names of 32 characters or fewer.")
        if normalized not in result:
            result.append(normalized)
    if len(result) > 32:
        raise ValueError("an item can have at most 32 tags.")
    return tuple(sorted(result))


def _reminders(value: str) -> tuple[int, ...]:
    if not value:
        return ()
    result: set[int] = set()
    for candidate in value.replace(";", ",").split(","):
        normalized = candidate.strip()
        if not normalized:
            continue
        try:
            offset = int(normalized)
        except ValueError as error:
            raise ValueError("reminders must be comma-separated minute values.") from error
        if not 0 <= offset <= 43_200:
            raise ValueError("reminders must be between 0 and 43200 minutes.")
        result.add(offset)
    if len(result) > 16:
        raise ValueError("an item can have at most 16 reminders.")
    return tuple(sorted(result))


def _clean_optional(value: str, field_name: str, limit: int, *, collapse: bool) -> str:
    if not value:
        return ""
    return _clean_text(value, field_name, limit, collapse=collapse)


def _safe_url(value: str) -> str:
    if not value:
        return ""
    if len(value) > 1_024 or any(
        ord(character) < 33 or ord(character) == 127 for character in value
    ):
        raise ValueError("url must be a safe HTTP or HTTPS URL.")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("url must be a safe HTTP or HTTPS URL.") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("url must be a safe HTTP or HTTPS URL without credentials.")
    return value


def _stable_key(kind: str, *parts: str) -> str:
    canonical = "\x1f".join(part.strip().casefold() for part in parts)
    return f"{kind}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _row_key(kind: str, explicit_id: str, *parts: str) -> str:
    cleaned_id = explicit_id.strip()
    if cleaned_id:
        if len(cleaned_id) > 240 or any(
            ord(character) < 32 or ord(character) == 127 for character in cleaned_id
        ):
            raise ValueError("id must be 240 characters or fewer without control characters.")
        return f"{kind}:id:{cleaned_id.casefold()}"
    return _stable_key(kind, *parts)


def _unique_item_key(item: ImportItem, counts: dict[str, int]) -> ImportItem:
    count = counts.get(item.stable_key, 0) + 1
    counts[item.stable_key] = count
    if count == 1:
        return item
    return replace(item, stable_key=f"{item.stable_key}:{count}")


def _strip_list_marker(value: str) -> str:
    normalized = value.strip()
    for marker in ("- [ ] ", "- [x] ", "- [X] ", "- ", "* ", "• "):
        if normalized.startswith(marker):
            return normalized[len(marker) :].strip()
    return normalized


def _raise_value(message: str) -> None:
    raise ValueError(message)


# Descriptive aliases keep the import API easy to discover for callers that
# prefer verb-oriented names.
parse_import_csv = parse_csv
parse_import_text = parse_text_list
get_task_import_template = task_template_csv
get_calendar_import_template = calendar_template_csv


__all__ = [
    "CALENDAR_IMPORT_HEADERS",
    "CalendarImportItem",
    "BulkImportResult",
    "DEFAULT_CALENDAR_DURATION_MINUTES",
    "DEFAULT_CALENDAR_START_TIME",
    "ImportFileError",
    "ImportFormat",
    "ImportIssue",
    "ImportItem",
    "ImportParseResult",
    "ImportTarget",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_COLUMNS",
    "MAX_IMPORT_FIELD_LENGTH",
    "MAX_IMPORT_ROWS",
    "TaskImportItem",
    "TASK_IMPORT_HEADERS",
    "calendar_template_csv",
    "get_calendar_import_template",
    "get_task_import_template",
    "normalize_import_name",
    "normalize_import_target",
    "parse_csv",
    "parse_import",
    "parse_import_csv",
    "parse_import_text",
    "parse_text_list",
    "read_import_file",
    "task_template_csv",
    "template_csv",
    "text_list_instructions",
]
