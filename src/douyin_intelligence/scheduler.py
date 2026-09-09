from __future__ import annotations

import getpass
import html
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import project_root, resolve_path


def current_principal() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def task_xml(config: dict[str, Any]) -> str:
    workspace = project_root()
    python = resolve_path(".venv/Scripts/python.exe")
    principal = html.escape(current_principal())
    command = html.escape(str(python))
    arguments = html.escape(f'-m douyin_intelligence.cli --config "{resolve_path("config/content_intelligence.json")}" daily-news --live-douyin --scheduled')
    working = html.escape(str(workspace))
    schedule = str(config["jobs"]["daily_news"].get("schedule_time") or "02:00")
    hour, minute = [int(value) for value in schedule.split(":", 1)]
    start = f"2026-01-01T{hour:02d}:{minute:02d}:00"
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Generate yesterday's verified technology news with Douyin attention signals.</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{principal}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><RestartOnFailure><Interval>PT5M</Interval><Count>2</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{working}</WorkingDirectory></Exec></Actions>
</Task>'''


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["schtasks.exe", *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "schtasks timed out after 15 seconds")


def install(config: dict[str, Any]) -> dict[str, Any]:
    name = str(config["workbench"]["task_name"])
    path = resolve_path("data/state/daily_news_task.xml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(task_xml(config), encoding="utf-16")
    completed = _run(["/Create", "/TN", name, "/XML", str(path), "/F"])
    return {"ok": completed.returncode == 0, "task_name": name, "task_xml": str(path), "returncode": completed.returncode, "output": (completed.stdout + completed.stderr).strip()}


def query(config: dict[str, Any]) -> dict[str, Any]:
    name = str(config["workbench"]["task_name"])
    completed = _run(["/Query", "/TN", name, "/V", "/FO", "LIST"])
    return {"ok": completed.returncode == 0, "task_name": name, "returncode": completed.returncode, "output": (completed.stdout + completed.stderr).strip()}


def run_now(config: dict[str, Any]) -> dict[str, Any]:
    name = str(config["workbench"]["task_name"])
    completed = _run(["/Run", "/TN", name])
    return {"ok": completed.returncode == 0, "task_name": name, "returncode": completed.returncode, "output": (completed.stdout + completed.stderr).strip()}


def uninstall(config: dict[str, Any]) -> dict[str, Any]:
    name = str(config["workbench"]["task_name"])
    completed = _run(["/Delete", "/TN", name, "/F"])
    return {"ok": completed.returncode == 0, "task_name": name, "returncode": completed.returncode, "output": (completed.stdout + completed.stderr).strip()}
