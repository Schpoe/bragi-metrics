"""Unit tests for the BambooHR Who's Out parser."""

from bamboohr import parse_whos_out


def test_parses_timeoff_and_holiday():
    items = [
        {"id": 101, "type": "timeOff", "employeeId": 42, "name": "Jane Doe",
         "start": "2026-02-03", "end": "2026-02-07"},
        {"id": 202, "type": "holiday", "name": "Easter Monday",
         "start": "2026-04-06", "end": "2026-04-06"},
    ]
    rows = parse_whos_out(items)
    assert len(rows) == 2

    timeoff = rows[0]
    assert timeoff["id"] == 101
    assert timeoff["employee_id"] == 42
    assert timeoff["kind"] == "timeOff"
    assert timeoff["start_date"] == "2026-02-03"
    assert timeoff["end_date"] == "2026-02-07"

    holiday = rows[1]
    assert holiday["employee_id"] is None   # company-wide
    assert holiday["kind"] == "holiday"


def test_skips_items_missing_id_or_dates():
    items = [
        {"type": "timeOff", "employeeId": 1, "start": "2026-01-01", "end": "2026-01-02"},  # no id
        {"id": 5, "type": "timeOff", "employeeId": 1, "start": "", "end": "2026-01-02"},   # no start
        {"id": 6, "type": "timeOff", "employeeId": 1, "start": "2026-01-01"},              # no end
    ]
    assert parse_whos_out(items) == []


def test_unknown_type_defaults_to_timeoff():
    rows = parse_whos_out([
        {"id": 7, "employeeId": 9, "start": "2026-03-01", "end": "2026-03-01"},
    ])
    assert rows[0]["kind"] == "timeOff"


def test_empty_employee_id_becomes_none():
    rows = parse_whos_out([
        {"id": 8, "type": "holiday", "employeeId": "", "name": "X",
         "start": "2026-03-01", "end": "2026-03-01"},
    ])
    assert rows[0]["employee_id"] is None
