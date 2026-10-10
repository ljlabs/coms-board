"""Recurring prompts dispatched to registered Coms Board agents."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import api
from .workspace import DEFAULT_WORKSPACE, workspace_path

SCHEDULE_TYPES = ("cron", "interval", "weekly")
INTERVALS = {"minutes": 60, "hours": 3600, "days": 86400}
RUN_STATUSES = ("queued", "running", "waiting", "completed", "failed", "cancelled")


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("schedule time must include a timezone")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("timezone is required (for example Europe/London)")
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {name}") from exc


def _field(raw: str, low: int, high: int, *, sunday: bool = False) -> tuple[set[int], bool]:
    if not isinstance(raw, str) or not raw:
        raise ValueError("cron expression must have five fields")
    unrestricted = raw == "*"
    values: set[int] = set()
    for part in raw.split(","):
        if not part:
            raise ValueError(f"invalid cron field: {raw}")
        base, slash, step_raw = part.partition("/")
        if slash:
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise ValueError(f"invalid cron step: {part}")
            step = int(step_raw)
        else:
            step = 1
        if base == "*":
            start, end = low, high
        elif "-" in base:
            sides = base.split("-")
            if len(sides) != 2 or not all(side.isdigit() for side in sides):
                raise ValueError(f"invalid cron range: {part}")
            start, end = map(int, sides)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise ValueError(f"invalid cron value: {part}")
        if sunday and end == 7:
            if start > 7:
                raise ValueError(f"cron value outside {low}-{high}: {part}")
            expanded = range(start, end + 1, step)
            values.update(0 if value == 7 else value for value in expanded)
            continue
        if start < low or end > high or start > end:
            if not (sunday and start == 7 and end == 7):
                raise ValueError(f"cron value outside {low}-{high}{' or 7 for Sunday' if sunday else ''}: {part}")
        if sunday and start == end == 7:
            values.add(0)
        else:
            values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"cron field has no values: {raw}")
    return values, unrestricted


def _cron_fields(expression: str):
    if not isinstance(expression, str):
        raise ValueError("cron expression must be text")
    pieces = expression.split()
    if len(pieces) != 5:
        raise ValueError("cron expression must have five fields: minute hour day-of-month month day-of-week")
    minute = _field(pieces[0], 0, 59)
    hour = _field(pieces[1], 0, 23)
    dom = _field(pieces[2], 1, 31)
    month = _field(pieces[3], 1, 12)
    dow = _field(pieces[4], 0, 6, sunday=True)
    fields = minute, hour, dom, month, dow
    # A Gregorian calendar repeats its weekday/date pattern every 400 years.
    # Reject impossible combinations without scanning millions of minutes.
    start = date(2000, 1, 1)
    possible = False
    for offset in range(146097):
        day = start + timedelta(days=offset)
        if _cron_day_matches(day, fields):
            possible = True
            break
    if not possible:
        raise ValueError("cron day, month and weekday fields can never match")
    return fields


def _cron_day_matches(day: date, fields) -> bool:
    dom, month, dow = fields[2], fields[3], fields[4]
    if day.month not in month[0]:
        return False
    cron_dow = (day.weekday() + 1) % 7
    day_matches = day.day in dom[0]
    weekday_matches = cron_dow in dow[0]
    if dom[1] and dow[1]:
        return True
    if dom[1]:
        return weekday_matches
    if dow[1]:
        return day_matches
    return day_matches or weekday_matches


def _cron_matches(local: datetime, fields) -> bool:
    minute, hour, dom, month, dow = fields
    if local.minute not in minute[0] or local.hour not in hour[0]:
        return False
    return _cron_day_matches(local.date(), fields)


def _time(raw: str) -> tuple[int, int]:
    if not isinstance(raw, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw):
        raise ValueError("weekly time must be in 24-hour HH:MM format")
    hour, minute = map(int, raw.split(":"))
    return hour, minute


def validate_config(schedule_type: str, config: dict) -> dict:
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError(f"schedule_type must be one of {', '.join(SCHEDULE_TYPES)}")
    if not isinstance(config, dict):
        raise ValueError("schedule_config must be an object")
    if schedule_type == "cron":
        expression = config.get("expression")
        _cron_fields(expression)
        return {"expression": " ".join(expression.split())}
    if schedule_type == "interval":
        every = config.get("every")
        unit = config.get("unit")
        if type(every) is not int or every < 1 or unit not in INTERVALS:
            raise ValueError("interval schedule needs every >= 1 and unit minutes, hours or days")
        if every * INTERVALS[unit] > 7 * 86400:
            raise ValueError("interval cannot be longer than 7 days")
        return {"every": every, "unit": unit}
    days = config.get("days")
    if not isinstance(days, list) or not days or any(type(day) is not int or day < 0 or day > 6 for day in days):
        raise ValueError("weekly schedule needs one or more weekdays from 0 (Monday) to 6 (Sunday)")
    _time(config.get("time"))
    return {"days": sorted(set(days)), "time": config["time"]}


def next_cron(expression: str, timezone_name: str, after: datetime | None = None) -> datetime:
    fields = _cron_fields(expression)
    zone = _zone(timezone_name)
    current = _utc(after)
    minute, hour, _dom, month, _dow = fields
    local_day = current.astimezone(zone).date()
    # Search local dates and only configured hours/minutes. The 400-year bound
    # is the full Gregorian weekday cycle and handles sparse leap-day rules.
    for offset in range(146097):
        day = local_day + timedelta(days=offset)
        if day.month not in month[0] or not _cron_day_matches(day, fields):
            continue
        for hour_value in sorted(hour[0]):
            for minute_value in sorted(minute[0]):
                naive = datetime(day.year, day.month, day.day, hour_value, minute_value)
                for fold in (0, 1):
                    local_candidate = naive.replace(tzinfo=zone, fold=fold)
                    candidate = local_candidate.astimezone(timezone.utc)
                    # Ignore nonexistent local times during the spring DST jump.
                    if candidate.astimezone(zone).replace(tzinfo=None) != naive:
                        continue
                    if candidate > current:
                        return candidate
    raise ValueError("cron expression has no occurrence in the next 400 years")


def next_run(schedule_type: str, config: dict, timezone_name: str,
             after: datetime | None = None) -> datetime:
    zone = _zone(timezone_name)
    current = _utc(after)
    config = validate_config(schedule_type, config)
    if schedule_type == "cron":
        return next_cron(config["expression"], timezone_name, current)
    if schedule_type == "interval":
        return current + timedelta(seconds=config["every"] * INTERVALS[config["unit"]])
    hour, minute = _time(config["time"])
    local_today = current.astimezone(zone).date()
    for offset in range(15):
        day = local_today + timedelta(days=offset)
        if day.weekday() not in config["days"]:
            continue
        local_candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        # Skip wall-clock times that do not exist during the spring DST jump.
        if local_candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local_candidate.replace(tzinfo=None):
            continue
        candidate = local_candidate.astimezone(timezone.utc)
        if candidate > current:
            return candidate
    raise ValueError("weekly schedule has no valid occurrence in the next two weeks")


def _workspace(conn, value: str | None) -> str:
    if value in (None, ""):
        row = conn.execute("SELECT value FROM runner_settings WHERE key='workspace'").fetchone()
        value = row[0] if row else ("/workspace" if os.environ.get("COMS_IN_DOCKER") == "1" else str(DEFAULT_WORKSPACE))
    return workspace_path({"workspace": value}, create_default=False)


def _validate_task(conn, data: dict, current: dict | None = None) -> dict:
    current = current or {}
    merged = {**current, **data}
    name = merged.get("name")
    prompt = merged.get("prompt")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise ValueError("name is required and must be 120 characters or fewer")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 50000:
        raise ValueError("prompt is required and must be 50,000 characters or fewer")
    agent_slug = merged.get("agent_slug")
    if not isinstance(agent_slug, str) or not agent_slug.strip():
        raise ValueError("choose a registered agent")
    agent = conn.execute("SELECT role FROM agents WHERE slug=?", (agent_slug.strip(),)).fetchone()
    if not agent:
        raise ValueError(f"agent {agent_slug!r} is not registered")
    if (agent["role"] or "").lower() == "human":
        raise ValueError("scheduled tasks must be assigned to an agent, not a human")
    schedule_type = merged.get("schedule_type")
    config = validate_config(schedule_type, merged.get("schedule_config"))
    zone = merged.get("timezone")
    _zone(zone)
    folder = _workspace(conn, merged.get("workspace"))
    return {"name": name.strip(), "prompt": prompt.strip(), "schedule_type": schedule_type,
            "schedule_config": config, "timezone": zone.strip(), "agent_slug": agent_slug.strip(),
            "workspace": folder}


def _decode(row: sqlite3.Row | dict) -> dict:
    task = dict(row)
    task["schedule_config"] = json.loads(task.pop("schedule_config_json"))
    task["enabled"] = task.get("status") == "active"
    return task


def list_tasks(conn, *, include_deleted: bool = False) -> list[dict]:
    where = "" if include_deleted else "WHERE s.status!='deleted'"
    rows = conn.execute(f"""SELECT s.*,
        a.name AS agent_name,
        (SELECT COUNT(*) FROM scheduled_task_runs r WHERE r.task_id=s.id) AS run_count,
        (SELECT status FROM scheduled_task_runs r WHERE r.task_id=s.id ORDER BY r.id DESC LIMIT 1) AS last_run_status
        FROM scheduled_tasks s LEFT JOIN agents a ON a.slug=s.agent_slug {where}
        ORDER BY CASE s.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                 s.next_run_at,s.id""").fetchall()
    return [_decode(row) for row in rows]


def get_task(conn, task_id: int) -> dict:
    row = conn.execute("SELECT s.*,a.name AS agent_name FROM scheduled_tasks s LEFT JOIN agents a ON a.slug=s.agent_slug WHERE s.id=?", (int(task_id),)).fetchone()
    if not row:
        raise api.NotFound(f"scheduled task #{task_id} not found")
    return _decode(row)


def create_task(conn, data: dict, actor: str) -> dict:
    valid = _validate_task(conn, data)
    ts = api.now()
    next_at = _stamp(next_run(valid["schedule_type"], valid["schedule_config"], valid["timezone"]))
    cur = conn.execute("""INSERT INTO scheduled_tasks(name,prompt,schedule_type,schedule_config_json,timezone,
        agent_slug,workspace,status,next_run_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
        (valid["name"], valid["prompt"], valid["schedule_type"], json.dumps(valid["schedule_config"], separators=(",", ":")),
         valid["timezone"], valid["agent_slug"], valid["workspace"], next_at, ts, ts))
    task_id = cur.lastrowid
    api.log_activity(conn, actor, "schedule.create", f"schedule:{task_id}", f"created scheduled task “{valid['name']}”")
    conn.commit()
    return get_task(conn, task_id)


def update_task(conn, task_id: int, data: dict, actor: str) -> dict:
    current = get_task(conn, task_id)
    if current["status"] == "deleted":
        raise ValueError("deleted scheduled tasks cannot be edited")
    allowed = {"name", "prompt", "schedule_type", "schedule_config", "timezone", "agent_slug", "workspace"}
    changes = {key: value for key, value in data.items() if key in allowed}
    if not changes:
        return current
    valid = _validate_task(conn, changes, current)
    schedule_changed = any(key in changes for key in ("schedule_type", "schedule_config", "timezone"))
    next_at = (_stamp(next_run(valid["schedule_type"], valid["schedule_config"], valid["timezone"]))
               if schedule_changed else current["next_run_at"])
    conn.execute("""UPDATE scheduled_tasks SET name=?,prompt=?,schedule_type=?,schedule_config_json=?,timezone=?,
        agent_slug=?,workspace=?,next_run_at=?,last_error='',updated_at=? WHERE id=?""",
        (valid["name"], valid["prompt"], valid["schedule_type"], json.dumps(valid["schedule_config"], separators=(",", ":")),
         valid["timezone"], valid["agent_slug"], valid["workspace"], next_at, api.now(), int(task_id)))
    api.log_activity(conn, actor, "schedule.update", f"schedule:{task_id}", f"updated scheduled task “{valid['name']}”")
    conn.commit()
    return get_task(conn, task_id)


def set_status(conn, task_id: int, status: str, actor: str) -> dict:
    current = get_task(conn, task_id)
    if current["status"] == "deleted":
        raise ValueError("deleted scheduled tasks cannot be resumed or paused")
    if status not in ("active", "paused"):
        raise ValueError("scheduled task status must be active or paused")
    next_at = current["next_run_at"]
    if status == "active" and current["status"] != "active":
        next_at = _stamp(next_run(current["schedule_type"], current["schedule_config"], current["timezone"]))
    conn.execute("UPDATE scheduled_tasks SET status=?,next_run_at=?,last_error='',updated_at=? WHERE id=?",
                 (status, next_at, api.now(), int(task_id)))
    api.log_activity(conn, actor, f"schedule.{status}", f"schedule:{task_id}", f"{status} scheduled task “{current['name']}”")
    conn.commit()
    return get_task(conn, task_id)


def delete_task(conn, task_id: int, actor: str) -> dict:
    task = get_task(conn, task_id)
    conn.execute("UPDATE scheduled_tasks SET status='deleted',updated_at=? WHERE id=?", (api.now(), int(task_id)))
    api.log_activity(conn, actor, "schedule.delete", f"schedule:{task_id}", f"deleted scheduled task “{task['name']}”")
    conn.commit()
    return {"deleted": int(task_id)}


def _new_ticket(conn, task: dict, actor: str, scheduled_for: str, *, manual: bool = False) -> int:
    name = task["name"]
    title = (f"{name} — scheduled run" if not manual else f"{name} — manual run")[:240]
    body = task["prompt"]
    if not body.strip():
        raise ValueError("scheduled task prompt is empty")
    ts = api.now()
    cur = conn.execute("""INSERT INTO tickets(title,body,type,status,priority,department,assignee,requester,
        tags,due_at,parent_id,board_id,ready_for_execution,agent_template_id,created_at,updated_at,
        recommended_workers,collaboration_required,workspace)
        VALUES(?,?, 'task','open','p2','',?,?, 'scheduled',NULL,NULL,1,1,NULL,?,?,NULL,0,?)""",
        (title, body, task["agent_slug"], actor or "scheduler", ts, ts, task["workspace"]))
    ticket_id = cur.lastrowid
    ticket = api._one(conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)))
    api._index_ticket(conn, ticket)
    api.log_activity(conn, actor or "scheduler", "ticket.create", f"ticket:{ticket_id}",
                     f"opened scheduled ticket #{ticket_id} “{title}” → {task['agent_slug']}")
    cur = conn.execute("""INSERT INTO scheduled_task_runs(task_id,ticket_id,scheduled_for,queued_at,status,is_manual)
        VALUES(?,?,?,?, 'queued', ?)""", (task["id"], ticket_id, scheduled_for, ts, int(manual)))
    conn.execute("UPDATE scheduled_tasks SET last_run_at=?,updated_at=? WHERE id=?", (ts, ts, task["id"]))
    return cur.lastrowid


def run_now(conn, task_id: int, actor: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read after acquiring the write lock so a concurrent edit cannot dispatch stale values.
        task = get_task(conn, task_id)
        if task["status"] == "deleted":
            raise ValueError("deleted scheduled tasks cannot be run")
        run_id = _new_ticket(conn, task, actor, _stamp(datetime.now(timezone.utc)), manual=True)
        api.log_activity(conn, actor, "schedule.run", f"schedule:{task_id}", f"ran scheduled task “{task['name']}” now")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_run(conn, run_id)


def get_run(conn, run_id: int) -> dict:
    row = conn.execute("""SELECT r.*,t.title AS ticket_title,t.status AS ticket_status,t.result AS ticket_result,
        (SELECT status FROM agent_runs a WHERE a.ticket_id=r.ticket_id AND a.stage!='collaborate' ORDER BY a.id DESC LIMIT 1) AS agent_run_status
        FROM scheduled_task_runs r LEFT JOIN tickets t ON t.id=r.ticket_id WHERE r.id=?""", (int(run_id),)).fetchone()
    if not row:
        raise api.NotFound(f"scheduled execution #{run_id} not found")
    return dict(row)


def list_runs(conn, task_id: int, limit: int = 100) -> list[dict]:
    get_task(conn, task_id)
    limit = max(1, min(500, int(limit)))
    rows = conn.execute("""SELECT r.*,t.title AS ticket_title,t.status AS ticket_status,t.result AS ticket_result,
        (SELECT status FROM agent_runs a WHERE a.ticket_id=r.ticket_id AND a.stage!='collaborate' ORDER BY a.id DESC LIMIT 1) AS agent_run_status
        FROM scheduled_task_runs r LEFT JOIN tickets t ON t.id=r.ticket_id
        WHERE r.task_id=? ORDER BY r.id DESC LIMIT ?""", (int(task_id), limit)).fetchall()
    return [dict(row) for row in rows]


def mark_ticket(conn, ticket_id: int, status: str, error: str = "") -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown scheduled run status: {status}")
    now = api.now()
    finished = now if status in ("completed", "failed", "cancelled") else ""
    conn.execute("""UPDATE scheduled_task_runs SET status=?,error=?,
        started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,
        finished_at=CASE WHEN ?='running' THEN '' WHEN ?!='' THEN ? ELSE finished_at END
        WHERE ticket_id=? AND status NOT IN ('completed','cancelled')""",
        (status, error or "", status, now, status, finished, finished, int(ticket_id)))


def dispatch_due(conn, *, current: datetime | None = None, limit: int = 100) -> int:
    current = _utc(current)
    current_stamp = _stamp(current)
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT * FROM scheduled_tasks WHERE status='active' AND next_run_at<=? ORDER BY next_run_at,id LIMIT ?",
                            (current_stamp, max(1, min(500, int(limit))))).fetchall()
        dispatched = 0
        for row in rows:
            task = _decode(row)
            # Reschedule from now to coalesce missed intervals after downtime.
            try:
                upcoming = _stamp(next_run(task["schedule_type"], task["schedule_config"], task["timezone"], current))
            except ValueError as exc:
                conn.execute("UPDATE scheduled_tasks SET status='paused',last_error=?,updated_at=? WHERE id=?",
                             (str(exc), api.now(), row["id"]))
                api.log_activity(conn, "scheduler", "schedule.error", f"schedule:{row['id']}",
                                 f"paused scheduled task “{task['name']}”: {exc}")
                continue
            _new_ticket(conn, task, "scheduler", row["next_run_at"])
            conn.execute("UPDATE scheduled_tasks SET next_run_at=?,updated_at=? WHERE id=? AND status='active'",
                         (upcoming, api.now(), row["id"]))
            api.log_activity(conn, "scheduler", "schedule.dispatch", f"schedule:{row['id']}",
                             f"dispatched scheduled task “{task['name']}”")
            dispatched += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dispatched
