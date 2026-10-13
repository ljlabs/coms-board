"""Focused tests for recurrence and scheduled-run lifecycle contracts."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from coms import api, db, mcp, schedules


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {
            "COMS_DB": str(root / "board.db"),
            "COMS_AGENT": "schedule-lead",
            "COMS_TICKET_ID": "",
        })
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        api.upsert_agent(self.conn, "schedule-lead", "Schedule lead", role="expert")
        api.upsert_agent(self.conn, "schedule-human", "Schedule human", role="human")
        self.workspace_patch = patch(
            "coms.schedules.workspace_path",
            side_effect=lambda data, create_default=False: str(Path(data["workspace"]).resolve()),
        )
        self.workspace_patch.start()

    def tearDown(self):
        self.workspace_patch.stop()
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            conn.close()
        db._local.__dict__.clear()
        self.env.stop()
        self.tmp.cleanup()

    def task_data(self, **changes):
        return {
            "name": "Daily report",
            "prompt": "Summarize the latest work.",
            "schedule_type": "interval",
            "schedule_config": {"every": 1, "unit": "hours"},
            "timezone": "UTC",
            "agent_slug": "schedule-lead",
            "workspace": str(self.workspace),
            **changes,
        }

    def create_task(self, **changes):
        return schedules.create_task(self.conn, self.task_data(**changes), "admin")

    def test_recurrence_validation_and_next_occurrences(self):
        after = datetime(2026, 10, 13, 9, 5, tzinfo=timezone.utc)
        self.assertEqual(
            schedules.next_run("cron", {"expression": "5 9 * * 1-5"}, "UTC", after),
            datetime(2026, 10, 14, 9, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            schedules.next_run("weekly", {"days": [2], "time": "10:30"}, "UTC", after),
            datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            schedules.next_run("interval", {"every": 15, "unit": "minutes"}, "UTC", after),
            after + timedelta(minutes=15),
        )
        self.assertEqual(
            schedules.validate_config("cron", {"expression": "  */15  9 * * 1-5 "}),
            {"expression": "*/15 9 * * 1-5"},
        )
        for schedule_type, config in (
            ("cron", {"expression": "60 * * * *"}),
            ("interval", {"every": 8, "unit": "days"}),
            ("weekly", {"days": [7], "time": "10:30"}),
            ("weekly", {"days": [1], "time": "24:00"}),
        ):
            with self.subTest(schedule_type=schedule_type, config=config):
                with self.assertRaises(ValueError):
                    schedules.validate_config(schedule_type, config)
        with self.assertRaises(ValueError):
            schedules.next_run("interval", {"every": 1, "unit": "hours"}, "Not/AZone", after)

    def test_creation_validates_agent_and_persists_normalized_schedule(self):
        with self.assertRaisesRegex(ValueError, "not a human"):
            self.create_task(agent_slug="schedule-human")
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.create_task(agent_slug="missing-agent")
        task = self.create_task(name="  Daily report  ", prompt="  Report  ")
        self.assertEqual(task["name"], "Daily report")
        self.assertEqual(task["prompt"], "Report")
        self.assertEqual(task["status"], "active")
        self.assertTrue(task["enabled"])
        self.assertEqual(task["schedule_config"], {"every": 1, "unit": "hours"})
        self.assertEqual(task["agent_name"], "Schedule lead")
        self.assertEqual(task["workspace"], str(self.workspace.resolve()))
        self.assertEqual(schedules.list_tasks(self.conn)[0]["id"], task["id"])
        self.assertEqual(
            self.conn.execute("SELECT value FROM runner_settings WHERE key=?", (schedules.SCHEDULE_BOARD_SETTING,)).fetchone()[0],
            str(self.conn.execute("SELECT id FROM ticket_boards WHERE name=?", (schedules.SCHEDULE_BOARD_NAME,)).fetchone()[0]),
        )

    def test_edit_pause_resume_manual_run_and_delete_keep_history(self):
        task = self.create_task()
        edited = schedules.update_task(self.conn, task["id"], {"name": "Weekly report"}, "admin")
        self.assertEqual(edited["name"], "Weekly report")
        self.assertEqual(edited["next_run_at"], task["next_run_at"])
        paused = schedules.set_status(self.conn, task["id"], "paused", "admin")
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(paused["enabled"])
        run = schedules.run_now(self.conn, task["id"], "admin")
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["is_manual"], 1)
        ticket = api.get_ticket(self.conn, run["ticket_id"])
        self.assertEqual(ticket["status"], "open")
        self.assertEqual(ticket["assignee"], "schedule-lead")
        self.assertTrue(ticket["ready_for_execution"])
        self.assertEqual(ticket["board_id"], int(self.conn.execute(
            "SELECT value FROM runner_settings WHERE key=?", (schedules.SCHEDULE_BOARD_SETTING,)
        ).fetchone()[0]))
        resumed = schedules.set_status(self.conn, task["id"], "active", "admin")
        self.assertEqual(resumed["status"], "active")
        self.assertGreater(resumed["next_run_at"], schedules._stamp(datetime.now(timezone.utc)))
        self.assertEqual(len(schedules.list_runs(self.conn, task["id"])), 1)
        self.assertEqual(schedules.delete_task(self.conn, task["id"], "admin"), {"deleted": task["id"]})
        self.assertEqual(schedules.list_tasks(self.conn), [])
        self.assertEqual(schedules.list_tasks(self.conn, include_deleted=True)[0]["status"], "deleted")
        self.assertEqual(len(schedules.list_runs(self.conn, task["id"])), 1)
        with self.assertRaisesRegex(ValueError, "deleted scheduled tasks"):
            schedules.set_status(self.conn, task["id"], "active", "admin")

    def test_due_dispatch_coalesces_missed_occurrences_and_links_one_run(self):
        task = self.create_task()
        due = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
        current = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        self.conn.execute("UPDATE scheduled_tasks SET next_run_at=? WHERE id=?", (schedules._stamp(due), task["id"]))
        self.conn.commit()
        self.assertEqual(schedules.dispatch_due(self.conn, current=current), 1)
        self.assertEqual(schedules.dispatch_due(self.conn, current=current), 0)
        runs = schedules.list_runs(self.conn, task["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["scheduled_for"], schedules._stamp(due))
        self.assertEqual(runs[0]["is_manual"], 0)
        self.assertGreater(schedules.get_task(self.conn, task["id"])["next_run_at"], schedules._stamp(current))
        ticket = api.get_ticket(self.conn, runs[0]["ticket_id"])
        self.assertIn("scheduled run", ticket["title"])
        self.assertEqual(ticket["workspace"], str(self.workspace.resolve()))

    def test_run_status_transitions_record_terminal_time_and_preserve_completed_rows(self):
        task = self.create_task()
        run = schedules.run_now(self.conn, task["id"], "admin")
        schedules.mark_ticket(self.conn, run["ticket_id"], "running")
        running = schedules.get_run(self.conn, run["id"])
        self.assertEqual(running["status"], "running")
        self.assertTrue(running["started_at"])
        schedules.mark_ticket(self.conn, run["ticket_id"], "failed", "agent failed")
        failed = schedules.get_run(self.conn, run["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "agent failed")
        self.assertTrue(failed["finished_at"])
        schedules.mark_ticket(self.conn, run["ticket_id"], "completed")
        schedules.mark_ticket(self.conn, run["ticket_id"], "running")
        self.assertEqual(schedules.get_run(self.conn, run["id"])["status"], "completed")
        with self.assertRaisesRegex(ValueError, "unknown scheduled run status"):
            schedules.mark_ticket(self.conn, run["ticket_id"], "not-a-status")

    def test_mcp_schedule_tools_create_pause_run_and_read_history(self):
        args = {
            "name": "MCP report",
            "prompt": "Write a short report.",
            "schedule_type": "interval",
            "interval_every": 2,
            "interval_unit": "hours",
            "agent_slug": "schedule-lead",
            "workspace": str(self.workspace),
        }
        created = json.loads(mcp.call_tool("schedule_create", args)["content"][0]["text"])
        self.assertEqual(created["schedule_config"], {"every": 2, "unit": "hours"})
        paused = json.loads(mcp.call_tool("schedule_pause", {"id": created["id"]})["content"][0]["text"])
        self.assertEqual(paused["status"], "paused")
        run = json.loads(mcp.call_tool("schedule_run_now", {"id": created["id"]})["content"][0]["text"])
        self.assertEqual(run["status"], "queued")
        detail = json.loads(mcp.call_tool("schedule_get", {"id": created["id"]})["content"][0]["text"])
        self.assertEqual(len(detail["runs"]), 1)
        resumed = json.loads(mcp.call_tool("schedule_resume", {"id": created["id"]})["content"][0]["text"])
        self.assertEqual(resumed["status"], "active")


if __name__ == "__main__":
    unittest.main()
