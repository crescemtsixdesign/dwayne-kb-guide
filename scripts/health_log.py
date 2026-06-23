#!/usr/bin/env python
"""Structured health logger for the GitHub Pages dashboard.

This script is the primary writer for state.json. It avoids passive transcript
parsing: callers pass the already-interpreted health event explicitly.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
DAILY_CALORIE_TARGET = 2100
WEEKLY_WORKOUT_TARGET = 4
DEFAULT_STATE = Path(__file__).resolve().parents[1] / "state.json"
VALID_TONES = {"good", "warn", "bad"}
VALID_WORKOUT_STATUSES = {"done", "modified", "missed", "not_logged"}


def now() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def day_key(value: str | None) -> str:
    if not value or value == "today":
        return now().strftime("%Y-%m-%d")
    if value == "yesterday":
        return (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"Invalid --date {value!r}; use today, yesterday, or YYYY-MM-DD") from exc
    return value


def week_anchor(day: str | None = None) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").astimezone() if day else now()
    if d.weekday() == 6:  # Sunday
        start = d
    else:
        start = d - timedelta(days=d.weekday() + 1)
    return start.strftime("%Y-%m-%d")


def empty_day() -> dict[str, Any]:
    return {
        "workout": {
            "status": "not_logged",
            "label": "No workout logged yet",
            "tone": "warn",
            "detail": "Use chat to log done, missed, or modified.",
        },
        "foods": [],
        "weight": None,
        "sleep": {"label": "Not logged", "tone": "warn", "detail": "No sleep update yet."},
        "notes": [],
    }


def blank_state(today: str | None = None) -> dict[str, Any]:
    current = today or day_key("today")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "currentDate": current,
        "updated": now_iso(),
        "targets": {"dailyCalories": DAILY_CALORIE_TARGET, "weeklyWorkouts": WEEKLY_WORKOUT_TARGET},
        "week": {"anchor": week_anchor(current), "completedWorkouts": 0},
        "days": {current: empty_day()},
    }


def legacy_day_from_state(data: dict[str, Any], today: str) -> dict[str, Any]:
    day = empty_day()
    if isinstance(data.get("workout"), dict):
        day["workout"] = {"status": data["workout"].get("status", "legacy"), **data["workout"]}
    if isinstance(data.get("foods"), list):
        foods = []
        for item in data["foods"]:
            if isinstance(item, dict):
                foods.append(item)
            else:
                foods.append({"name": str(item), "tone": "warn", "calories": None, "note": "Migrated from old state."})
        day["foods"] = foods
    if isinstance(data.get("weight"), dict) and data["weight"].get("label"):
        day["weight"] = {
            "value": data["weight"].get("value"),
            "label": data["weight"]["label"],
            "tone": data["weight"].get("tone", "warn"),
            "detail": data["weight"].get("detail", ""),
        }
    if isinstance(data.get("sleep"), dict):
        day["sleep"] = data["sleep"]
    return day


def normalize(data: dict[str, Any] | None) -> dict[str, Any]:
    today = day_key("today")
    raw = data or {}
    state = blank_state(today)

    if isinstance(raw.get("days"), dict):
        state["days"] = raw["days"]
    elif raw:
        state["days"] = {today: legacy_day_from_state(raw, today)}

    state["schemaVersion"] = SCHEMA_VERSION
    state["currentDate"] = today
    state["updated"] = raw.get("updated") or state["updated"]
    state["targets"] = raw.get("targets") if isinstance(raw.get("targets"), dict) else state["targets"]
    state["targets"].setdefault("dailyCalories", DAILY_CALORIE_TARGET)
    state["targets"].setdefault("weeklyWorkouts", WEEKLY_WORKOUT_TARGET)
    state.setdefault("days", {})
    state["days"].setdefault(today, empty_day())

    for key, day in list(state["days"].items()):
        if not isinstance(day, dict):
            state["days"][key] = empty_day()
            continue
        day.setdefault("workout", empty_day()["workout"])
        day.setdefault("foods", [])
        day.setdefault("weight", None)
        day.setdefault("sleep", empty_day()["sleep"])
        day.setdefault("notes", [])

    anchor = week_anchor(today)
    completed = sum(
        1
        for key, day in state["days"].items()
        if key >= anchor and isinstance(day, dict) and day.get("workout", {}).get("status") == "done"
    )
    state["week"] = {"anchor": anchor, "completedWorkouts": completed}

    # Intentionally do not preserve legacy top-level fields like workout/foods/score.
    # The dashboard derives summary values from days[currentDate].
    return state


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return blank_state()
    try:
        return normalize(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    clean = normalize(state)
    clean["updated"] = now_iso()
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))


def require_tone(tone: str) -> str:
    if tone not in VALID_TONES:
        raise SystemExit(f"Invalid tone {tone!r}; choose one of {', '.join(sorted(VALID_TONES))}")
    return tone


def add_food(state: dict[str, Any], args: argparse.Namespace) -> str:
    date = day_key(args.date)
    day = state["days"].setdefault(date, empty_day())
    entry = {
        "name": args.name,
        "tone": require_tone(args.tone),
        "calories": args.calories,
        "note": args.note or "Logged from chat.",
    }
    existing = [f for f in day.get("foods", []) if isinstance(f, dict)]
    for idx, old in enumerate(existing):
        if old.get("name", "").strip().lower() == args.name.strip().lower():
            if args.replace:
                existing[idx] = entry
                day["foods"] = existing
                return f"updated food for {date}: {args.name}"
            return f"no change; food already exists for {date}: {args.name}"
    day.setdefault("foods", []).append(entry)
    return f"added food for {date}: {args.name}"


def set_workout(state: dict[str, Any], args: argparse.Namespace) -> str:
    status = args.status
    if status not in VALID_WORKOUT_STATUSES:
        raise SystemExit(f"Invalid workout status {status!r}; choose done, modified, missed, not_logged")
    date = day_key(args.date)
    day = state["days"].setdefault(date, empty_day())
    tone = {"done": "good", "modified": "warn", "missed": "bad", "not_logged": "warn"}[status]
    label = args.label or {"done": "Done", "modified": "Modified / cut short", "missed": "Missed workout", "not_logged": "No workout logged yet"}[status]
    detail = args.detail or "Workout saved from direct health logger."
    day["workout"] = {"status": status, "label": label, "tone": tone, "detail": detail}
    return f"set workout for {date}: {status}"


def set_weight(state: dict[str, Any], args: argparse.Namespace) -> str:
    date = day_key(args.date)
    day = state["days"].setdefault(date, empty_day())
    value = float(args.value)
    day["weight"] = {
        "value": value,
        "label": f"{value:.1f} lb",
        "tone": require_tone(args.tone),
        "detail": args.detail or "Weight saved from direct health logger.",
    }
    return f"set weight for {date}: {value:.1f} lb"


def set_sleep(state: dict[str, Any], args: argparse.Namespace) -> str:
    date = day_key(args.date)
    day = state["days"].setdefault(date, empty_day())
    day["sleep"] = {"label": args.label, "tone": require_tone(args.tone), "detail": args.detail or args.label}
    return f"set sleep for {date}: {args.label}"


def validate_state(state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != SCHEMA_VERSION:
        raise SystemExit("state schemaVersion must be 3")
    if not isinstance(state.get("days"), dict):
        raise SystemExit("state.days must be an object")
    for key, day in state["days"].items():
        try:
            datetime.strptime(key, "%Y-%m-%d")
        except ValueError as exc:
            raise SystemExit(f"invalid date key in state.days: {key}") from exc
        if not isinstance(day.get("foods"), list):
            raise SystemExit(f"days[{key}].foods must be a list")
        workout = day.get("workout", {})
        if workout.get("status") not in VALID_WORKOUT_STATUSES | {"legacy"}:
            raise SystemExit(f"days[{key}].workout.status is invalid: {workout.get('status')!r}")


def git_sync(repo: Path, commit_message: str) -> None:
    commands = [
        ["git", "add", "state.json"],
        ["git", "status", "--porcelain", "state.json"],
    ]
    add = subprocess.run(commands[0], cwd=repo, capture_output=True, text=True, check=False)
    if add.returncode != 0:
        raise SystemExit(add.stderr or add.stdout or "git add failed")
    status = subprocess.run(commands[1], cwd=repo, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        raise SystemExit(status.stderr or status.stdout or "git status failed")
    if not status.stdout.strip():
        print("git: no state.json changes to commit")
        return
    for cmd in (["git", "commit", "-m", commit_message], ["git", "push", "origin", "main"]):
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=False)
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct structured writer for the health dashboard state.json")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="Path to state.json")
    parser.add_argument("--no-sync", action="store_true", help="Write state.json but do not git commit/push")
    parser.add_argument("--dry-run", action="store_true", help="Print resulting JSON without writing")
    parser.add_argument("--message", default="chore: direct health log update", help="Git commit message when syncing")
    sub = parser.add_subparsers(dest="command", required=True)

    food = sub.add_parser("food", help="Add a food entry")
    food.add_argument("--date", default="today")
    food.add_argument("--name", required=True)
    food.add_argument("--calories", type=int)
    food.add_argument("--tone", default="warn", choices=sorted(VALID_TONES))
    food.add_argument("--note", default="")
    food.add_argument("--replace", action="store_true")
    food.set_defaults(func=add_food)

    workout = sub.add_parser("workout", help="Set workout status")
    workout.add_argument("--date", default="today")
    workout.add_argument("--status", required=True, choices=sorted(VALID_WORKOUT_STATUSES))
    workout.add_argument("--label", default="")
    workout.add_argument("--detail", default="")
    workout.set_defaults(func=set_workout)

    weight = sub.add_parser("weight", help="Set weight")
    weight.add_argument("--date", default="today")
    weight.add_argument("--value", required=True, type=float)
    weight.add_argument("--tone", default="good", choices=sorted(VALID_TONES))
    weight.add_argument("--detail", default="")
    weight.set_defaults(func=set_weight)

    sleep = sub.add_parser("sleep", help="Set sleep status")
    sleep.add_argument("--date", default="today")
    sleep.add_argument("--label", required=True)
    sleep.add_argument("--tone", default="warn", choices=sorted(VALID_TONES))
    sleep.add_argument("--detail", default="")
    sleep.set_defaults(func=set_sleep)

    sub.add_parser("validate", help="Validate and normalize state.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    path = Path(args.state).resolve()
    state = load_state(path)

    if args.command == "validate":
        validate_state(state)
        if args.dry_run:
            print(json.dumps(normalize(state), indent=2, ensure_ascii=False))
            return 0
        save_state(path, state)
        print(f"validated {path}")
    else:
        result = args.func(state, args)
        validate_state(normalize(state))
        if args.dry_run:
            print(result)
            print(json.dumps(normalize(state), indent=2, ensure_ascii=False))
            return 0
        save_state(path, state)
        print(result)

    if not args.no_sync and not args.dry_run:
        git_sync(path.parent, args.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
