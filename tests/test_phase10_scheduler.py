"""Tests for Phase 10 Part A: Scheduler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestScheduler:

    def test_get_default_schedules(self):
        from src.scheduler import get_default_schedules
        schedules = get_default_schedules()
        assert len(schedules) >= 4
        names = {s.name for s in schedules}
        assert "morning_scan" in names
        assert "pregame_scan" in names
        assert "nightly_calibrate" in names

    def test_generate_cron(self):
        from src.scheduler import generate_cron, get_default_schedules
        output = generate_cron(get_default_schedules())
        assert "crontab" in output.lower() or "0 9 * * *" in output
        assert "morning_scan" in output.lower() or "0 9 * * *" in output

    def test_generate_cron_disabled_entry(self):
        from src.scheduler import generate_cron, ScheduleEntry
        entries = [ScheduleEntry(
            name="disabled", description="nope", cron_expression="0 0 * * *",
            command="echo fail", working_directory="/tmp", enabled=False,
        )]
        output = generate_cron(entries)
        assert "[DISABLED]" in output

    def test_generate_windows_task_scheduler(self):
        from src.scheduler import generate_windows_task_scheduler, get_default_schedules
        output = generate_windows_task_scheduler(get_default_schedules())
        assert "ScheduledTask" in output or "New-ScheduledTask" in output

    def test_generate_cloud_config(self):
        from src.scheduler import generate_cloud_config, get_default_schedules
        config = generate_cloud_config(get_default_schedules())
        assert "version" in config
        assert "jobs" in config
        assert isinstance(config["jobs"], dict)

    def test_generate_github_actions(self):
        from src.scheduler import generate_github_actions, get_default_schedules
        output = generate_github_actions(get_default_schedules())
        assert "schedule:" in output
        assert "cron:" in output
        assert "jobs:" in output

    def test_schedule_entry_to_dict(self):
        from src.scheduler import ScheduleEntry
        entry = ScheduleEntry(
            name="test", description="desc", cron_expression="0 * * * *",
            command="echo hi", working_directory="/tmp",
        )
        d = entry.to_dict()
        assert d["name"] == "test"
        assert d["cron"] == "0 * * * *"

    def test_cron_dow_conversion(self):
        from src.scheduler import _cron_dow_to_windows
        assert _cron_dow_to_windows("0") == "Sunday"
        assert _cron_dow_to_windows("MON") == "Monday"
        assert _cron_dow_to_windows("*") == "*"

    def test_list_schedules(self, capsys):
        from src.scheduler import main
        exit_code = main(["--list"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "morning_scan" in output

    def test_format_cron_output(self, capsys):
        from src.scheduler import main
        exit_code = main(["--format", "cron"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "MLB" in output

    def test_format_cloud_json(self, capsys):
        from src.scheduler import main
        exit_code = main(["--format", "cloud"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "jobs" in data

    def test_format_github_actions(self, capsys):
        from src.scheduler import main
        exit_code = main(["--format", "github-actions"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "schedule:" in output

    def test_output_to_file(self, tmp_path):
        from src.scheduler import main
        outfile = tmp_path / "schedule.cron"
        exit_code = main(["--format", "cron", "--output", str(outfile)])
        assert exit_code == 0
        assert outfile.exists()
        content = outfile.read_text()
        assert "MLB" in content
