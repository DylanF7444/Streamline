"""CSV and text-list import contracts for local tasks and calendar items."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from personal_hub.imports import (
    CALENDAR_IMPORT_HEADERS,
    MAX_IMPORT_FIELD_LENGTH,
    TASK_IMPORT_HEADERS,
    calendar_template_csv,
    parse_csv,
    parse_text_list,
    read_import_file,
    task_template_csv,
)


def test_import_templates_are_header_only_and_stable() -> None:
    assert task_template_csv().strip() == ",".join(TASK_IMPORT_HEADERS)
    assert calendar_template_csv().strip() == ",".join(CALENDAR_IMPORT_HEADERS)


def test_task_csv_parses_optional_fields_and_normalizes_values() -> None:
    result = parse_csv(
        """id,title,notes,due_date,due_time,all_day,timezone,priority,list,tags,reminders
42,Submit report,"Keep it local",2026-09-14,15:30,false,UTC,high,Work,"Release, urgent","60;0"
""",
        target="tasks",
    )

    assert not result.issues
    item = result.task_items[0]
    assert item.stable_key == "task:id:42"
    assert item.title == "Submit report"
    assert item.notes == "Keep it local"
    assert item.due_at == datetime(2026, 9, 14, 15, 30, tzinfo=UTC)
    assert not item.due_is_all_day
    assert item.priority == "high"
    assert item.list_name == "Work"
    assert item.tags == ("release", "urgent")
    assert item.reminder_offsets_minutes == (0, 60)


def test_text_task_list_ignores_comments_and_disambiguates_duplicates() -> None:
    result = parse_text_list(
        "# Weekly tasks\n- Buy milk\n* Call the bank\nBuy milk\n",
        target="tasks",
    )

    assert not result.issues
    assert [item.title for item in result.task_items] == [
        "Buy milk",
        "Call the bank",
        "Buy milk",
    ]
    assert result.task_items[0].stable_key != result.task_items[2].stable_key
    assert result.task_items[2].stable_key.endswith(":2")


def test_calendar_csv_supports_timed_and_all_day_rows() -> None:
    result = parse_csv(
        """id,title,start_date,start_time,end_date,end_time,all_day,timezone,location,description,url
meeting,Planning,2026-09-14,10:30,2026-09-14,11:15,false,UTC,Room 4,Agenda,https://example.test/planning
holiday,Holiday,2026-09-15,,,,true,UTC,,, 
""",
        target="calendar",
    )

    assert not result.issues
    timed, all_day = result.calendar_items
    assert timed.starts_at == datetime(2026, 9, 14, 10, 30, tzinfo=UTC)
    assert timed.ends_at == datetime(2026, 9, 14, 11, 15, tzinfo=UTC)
    assert timed.location == "Room 4"
    assert timed.deep_link == "https://example.test/planning"
    assert all_day.all_day
    assert all_day.ends_at == datetime(2026, 9, 16, tzinfo=UTC)


def test_calendar_text_list_accepts_optional_date_prefix_and_defaults() -> None:
    result = parse_text_list(
        "2026-09-14 10:30 | Planning\n2026-09-15 | Holiday\nLoose event\n",
        target="calendar",
        default_timezone="UTC",
        default_calendar_date=date(2026, 9, 16),
        default_start_time=time(8, 0),
        default_duration_minutes=30,
    )

    assert not result.issues
    timed, all_day, defaulted = result.calendar_items
    assert timed.starts_at == datetime(2026, 9, 14, 10, 30, tzinfo=UTC)
    assert timed.ends_at == datetime(2026, 9, 14, 11, tzinfo=UTC)
    assert all_day.all_day
    assert all_day.starts_at == datetime(2026, 9, 15, tzinfo=UTC)
    assert defaulted.starts_at == datetime(2026, 9, 16, 8, tzinfo=UTC)
    assert defaulted.ends_at == datetime(2026, 9, 16, 8, 30, tzinfo=UTC)


def test_invalid_rows_are_reported_without_aborting_valid_rows() -> None:
    too_long = "x" * (MAX_IMPORT_FIELD_LENGTH + 1)
    result = parse_csv(
        f"title,priority\nGood,normal\n{too_long},normal\nBad,urgent\n",
        target="tasks",
    )

    assert [item.title for item in result.task_items] == ["Good"]
    assert len(result.issues) == 2
    assert result.issues[0].row_number == 3
    assert "too long" in result.issues[0].message
    assert "priority" in result.issues[1].message


def test_import_file_requires_utf8_and_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "items.txt"
    path.write_text("One\nTwo\n", encoding="utf-8")
    assert read_import_file(path).splitlines() == ["One", "Two"]
    with pytest.raises(ValueError, match="does not exist"):
        read_import_file(tmp_path / "missing.txt")
