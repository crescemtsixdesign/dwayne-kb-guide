#!/usr/bin/env python
"""Structured health logger for the GitHub Pages dashboard.

Canonical storage lives in data/health-log.json as an append-only event log.
state.json is a generated compatibility snapshot for the static dashboard and any
older consumers. Callers still pass already-interpreted health events explicitly;
this script does not parse chat transcripts.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
LOG_SCHEMA_VERSION = 1
DAILY_CALORIE_TARGET = 2100
WEEKLY_WORKOUT_TARGET = 4
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = REPO_ROOT / "state.json"
DEFAULT_STORE = REPO_ROOT / "data" / "health-log.json"
VALID_TONES = {"good", "warn", "bad"}
VALID_WORKOUT_STATUSES = {"done", "modified", "missed", "not_logged"}
EVENT_TYPES = {"food", "workout", "weight", "sleep", "note"}


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
    d = datetime.strptime(day, "%Y-%m-%d") if day else now().replace(tzinfo=None)
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


def blank_log() -> dict[str, Any]:
    return {
        "schemaVersion": LOG_SCHEMA_VERSION,
        "updated": now_iso(),
        "targets": {"dailyCalories": DAILY_CALORIE_TARGET, "weeklyWorkouts": WEEKLY_WORKOUT_TARGET},
        "entries": [],
    }


def require_tone(tone: str) -> str:
    if tone not in VALID_TONES:
        raise SystemExit(f"Invalid tone {tone!r}; choose one of {', '.join(sorted(VALID_TONES))}")
    return tone


def slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return clean[:48] or "item"


def food_id(date: str, name: str, index: int | None = None) -> str:
    suffix = f"-{index}" if index is not None else ""
    return f"food-{date}-{slug(name)}{suffix}"


def event_id(kind: str) -> str:
    return f"evt-{now().strftime('%Y%m%d%H%M%S')}-{kind}-{uuid.uuid4().hex[:8]}"


def normalize_targets(raw: dict[str, Any] | None) -> dict[str, Any]:
    targets = raw if isinstance(raw, dict) else {}
    return {
        "dailyCalories": targets.get("dailyCalories", DAILY_CALORIE_TARGET),
        "weeklyWorkouts": targets.get("weeklyWorkouts", WEEKLY_WORKOUT_TARGET),
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


def normalize_snapshot(data: dict[str, Any] | None) -> dict[str, Any]:
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
    state["targets"] = normalize_targets(raw.get("targets"))
    state.setdefault("days", {})
    state["days"].setdefault(today, empty_day())

    for key, day in list(state["days"].items()):
        if not isinstance(day, dict):
            state["days"][key] = empty_day()
            continue
        defaults = empty_day()
        day.setdefault("workout", defaults["workout"])
        day.setdefault("foods", [])
        day.setdefault("weight", None)
        day.setdefault("sleep", defaults["sleep"])
        day.setdefault("notes", [])

    refresh_week(state)
    return state


def normalize_log(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = blank_log()
    if not isinstance(raw, dict):
        return data
    data["updated"] = raw.get("updated") or data["updated"]
    data["targets"] = normalize_targets(raw.get("targets"))
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        raise SystemExit("health log entries must be a list")
    clean_entries = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SystemExit(f"health log entry {index} must be an object")
        date = entry.get("date")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"health log entry {index} has invalid date: {date!r}") from exc
        kind = entry.get("type")
        if kind not in EVENT_TYPES:
            raise SystemExit(f"health log entry {index} has invalid type: {kind!r}")
        clean = dict(entry)
        clean.setdefault("id", f"evt-migrated-{index:04d}")
        clean.setdefault("recordedAt", data["updated"])
        clean.setdefault("action", "set" if kind in {"workout", "weight", "sleep"} else "add")
        clean_entries.append(clean)
    data["entries"] = clean_entries
    return data


def make_event(kind: str, date: str, payload: dict[str, Any], action: str = "set", source: str = "health_log.py") -> dict[str, Any]:
    return {
        "id": event_id(kind),
        "recordedAt": now_iso(),
        "date": date,
        "type": kind,
        "action": action,
        "source": source,
        **payload,
    }


def migrate_snapshot_to_log(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = normalize_snapshot(state)
    log = blank_log()
    log["updated"] = snapshot.get("updated") or log["updated"]
    log["targets"] = normalize_targets(snapshot.get("targets"))
    entries: list[dict[str, Any]] = []
    for date, day in sorted(snapshot.get("days", {}).items()):
        recorded_at = snapshot.get("updated") or now_iso()
        workout = day.get("workout") if isinstance(day, dict) else None
        if isinstance(workout, dict) and workout.get("status") != "not_logged":
            entries.append({
                "id": f"evt-migrated-{date}-workout",
                "recordedAt": recorded_at,
                "date": date,
                "type": "workout",
                "action": "set",
                "source": "state.json migration",
                "workout": workout,
            })
        for index, food in enumerate(day.get("foods", []) if isinstance(day, dict) else []):
            if not isinstance(food, dict):
                food = {"name": str(food), "tone": "warn", "calories": None, "note": "Migrated from old state."}
            name = food.get("name", f"Food item {index + 1}")
            entries.append({
                "id": f"evt-migrated-{date}-food-{index:02d}",
                "recordedAt": recorded_at,
                "date": date,
                "type": "food",
                "action": "upsert",
                "source": "state.json migration",
                "foodId": food.get("id") or food_id(date, name, index),
                "food": food,
            })
        weight = day.get("weight") if isinstance(day, dict) else None
        if isinstance(weight, dict) and weight.get("label"):
            entries.append({
                "id": f"evt-migrated-{date}-weight",
                "recordedAt": recorded_at,
                "date": date,
                "type": "weight",
                "action": "set",
                "source": "state.json migration",
                "weight": weight,
            })
        sleep = day.get("sleep") if isinstance(day, dict) else None
        if isinstance(sleep, dict) and sleep.get("label") and sleep.get("label") != "Not logged":
            entries.append({
                "id": f"evt-migrated-{date}-sleep",
                "recordedAt": recorded_at,
                "date": date,
                "type": "sleep",
                "action": "set",
                "source": "state.json migration",
                "sleep": sleep,
            })
        for index, note in enumerate(day.get("notes", []) if isinstance(day, dict) else []):
            entries.append({
                "id": f"evt-migrated-{date}-note-{index:02d}",
                "recordedAt": recorded_at,
                "date": date,
                "type": "note",
                "action": "add",
                "source": "state.json migration",
                "note": note,
            })
    log["entries"] = entries
    return log


def sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(enumerate(entries), key=lambda pair: (pair[1].get("date", ""), pair[1].get("recordedAt", ""), pair[0]))


def materialize(log: dict[str, Any], today: str | None = None) -> dict[str, Any]:
    current = today or day_key("today")
    state = blank_state(current)
    state["updated"] = log.get("updated") or state["updated"]
    state["targets"] = normalize_targets(log.get("targets"))
    state["days"] = {current: empty_day()}

    for _, entry in sort_entries(log.get("entries", [])):
        date = entry["date"]
        day = state["days"].setdefault(date, empty_day())
        kind = entry["type"]
        action = entry.get("action", "set")
        if kind == "food":
            item = deepcopy(entry.get("food") or {})
            if not item:
                continue
            item.setdefault("tone", "warn")
            item.setdefault("calories", None)
            item.setdefault("note", "Logged from canonical health store.")
            item_id = entry.get("foodId") or item.get("id") or food_id(date, item.get("name", "food"))
            item["id"] = item_id
            foods = [f for f in day.get("foods", []) if isinstance(f, dict)]
            existing_index = next((i for i, old in enumerate(foods) if old.get("id") == item_id), None)
            if action == "delete":
                day["foods"] = [old for old in foods if old.get("id") != item_id]
            elif existing_index is not None:
                foods[existing_index] = item
                day["foods"] = foods
            else:
                foods.append(item)
                day["foods"] = foods
        elif kind == "workout":
            day["workout"] = deepcopy(entry.get("workout") or empty_day()["workout"])
        elif kind == "weight":
            day["weight"] = deepcopy(entry.get("weight"))
        elif kind == "sleep":
            day["sleep"] = deepcopy(entry.get("sleep") or empty_day()["sleep"])
        elif kind == "note" and action != "delete":
            day.setdefault("notes", []).append(entry.get("note", ""))

    refresh_week(state)
    return state


def refresh_week(state: dict[str, Any]) -> None:
    today = state.get("currentDate") or day_key("today")
    anchor = week_anchor(today)
    completed = sum(
        1
        for key, day in state.get("days", {}).items()
        if key >= anchor and isinstance(day, dict) and day.get("workout", {}).get("status") == "done"
    )
    state["week"] = {"anchor": anchor, "completedWorkouts": completed}


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def load_store(store_path: Path, state_path: Path) -> dict[str, Any]:
    raw_log = load_json(store_path)
    if raw_log is not None:
        return normalize_log(raw_log)
    raw_state = load_json(state_path)
    return migrate_snapshot_to_log(raw_state or blank_state())


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))


def save_store(store_path: Path, state_path: Path, log: dict[str, Any]) -> dict[str, Any]:
    clean_log = normalize_log(log)
    clean_log["updated"] = now_iso()
    state = materialize(clean_log)
    validate_state(state)
    write_json(store_path, clean_log)
    write_json(state_path, state)
    return state


def add_food(log: dict[str, Any], args: argparse.Namespace) -> str:
    date = day_key(args.date)
    item_id = args.id or food_id(date, args.name)
    payload = {
        "foodId": item_id,
        "food": {
            "id": item_id,
            "name": args.name,
            "tone": require_tone(args.tone),
            "calories": args.calories,
            "note": args.note or "Logged from chat.",
        },
    }
    if args.raw_text:
        payload["rawText"] = args.raw_text
    if args.session_id:
        payload["sessionId"] = args.session_id
    entry = make_event(
        "food",
        date,
        payload,
        action="upsert" if args.replace else "add",
        source=args.source,
    )
    if not args.replace:
        current = materialize(log, today=date).get("days", {}).get(date, empty_day())
        if any(f.get("id") == item_id or f.get("name", "").strip().lower() == args.name.strip().lower() for f in current.get("foods", [])):
            return f"no change; food already exists for {date}: {args.name}"
    log.setdefault("entries", []).append(entry)
    return f"logged food event for {date}: {args.name}"


def set_workout(log: dict[str, Any], args: argparse.Namespace) -> str:
    status = args.status
    if status not in VALID_WORKOUT_STATUSES:
        raise SystemExit(f"Invalid workout status {status!r}; choose done, modified, missed, not_logged")
    date = day_key(args.date)
    tone = {"done": "good", "modified": "warn", "missed": "bad", "not_logged": "warn"}[status]
    label = args.label or {"done": "Done", "modified": "Modified / cut short", "missed": "Missed workout", "not_logged": "No workout logged yet"}[status]
    detail = args.detail or "Workout saved from direct health logger."
    log.setdefault("entries", []).append(make_event("workout", date, {"workout": {"status": status, "label": label, "tone": tone, "detail": detail}}))
    return f"logged workout event for {date}: {status}"


def set_weight(log: dict[str, Any], args: argparse.Namespace) -> str:
    date = day_key(args.date)
    value = float(args.value)
    log.setdefault("entries", []).append(
        make_event(
            "weight",
            date,
            {"weight": {"value": value, "label": f"{value:.1f} lb", "tone": require_tone(args.tone), "detail": args.detail or "Weight saved from direct health logger."}},
        )
    )
    return f"logged weight event for {date}: {value:.1f} lb"


def set_sleep(log: dict[str, Any], args: argparse.Namespace) -> str:
    date = day_key(args.date)
    log.setdefault("entries", []).append(make_event("sleep", date, {"sleep": {"label": args.label, "tone": require_tone(args.tone), "detail": args.detail or args.label}}))
    return f"logged sleep event for {date}: {args.label}"


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


def validate_log_health(log: dict[str, Any]) -> dict[str, int]:
    """Validate food event payloads and summarize missing estimates."""
    food_events = 0
    chat_food_events = 0
    missing_calorie_estimates = 0
    for index, entry in enumerate(log.get("entries", [])):
        if entry.get("type") != "food":
            continue
        food_events += 1
        food = entry.get("food")
        if not isinstance(food, dict) or not str(food.get("name", "")).strip():
            raise SystemExit(f"health log food entry {index} must include food.name")
        if food.get("tone", "warn") not in VALID_TONES:
            raise SystemExit(f"health log food entry {index} has invalid food.tone: {food.get('tone')!r}")
        calories = food.get("calories", food.get("estimatedCalories"))
        if calories is None:
            missing_calorie_estimates += 1
        elif not isinstance(calories, int) or calories < 0:
            raise SystemExit(f"health log food entry {index} calories must be a non-negative integer or null")
        if entry.get("source") == "chat-health-sync hook":
            chat_food_events += 1
            if not str(entry.get("rawText", "")).strip():
                raise SystemExit(f"chat-synced food entry {index} must include rawText")
    return {
        "food_events": food_events,
        "chat_food_events": chat_food_events,
        "missing_calorie_estimates": missing_calorie_estimates,
    }


def food_total(day: dict[str, Any]) -> tuple[int, int]:
    total = 0
    missing = 0
    for food in day.get("foods", []):
        calories = food.get("calories", food.get("estimatedCalories")) if isinstance(food, dict) else None
        if isinstance(calories, (int, float)):
            total += int(calories)
        else:
            missing += 1
    return total, missing


def history_payload(state: dict[str, Any], date: str) -> dict[str, Any]:
    day = state.get("days", {}).get(date, empty_day())
    total, missing = food_total(day)
    return {
        "date": date,
        "foodItems": len(day.get("foods", [])),
        "knownCalories": total,
        "missingCalorieEstimates": missing,
        "workoutStatus": day.get("workout", {}).get("status", "not_logged"),
        "weight": day.get("weight"),
        "sleep": day.get("sleep"),
        "day": day,
    }


def print_history(state: dict[str, Any], args: argparse.Namespace) -> None:
    dates = [day_key(args.date)] if args.date else sorted(state.get("days", {}))
    payloads = [history_payload(state, date) for date in dates]
    if args.json:
        print(json.dumps(payloads[0] if args.date else payloads, indent=2, ensure_ascii=False))
        return
    for payload in payloads:
        missing = payload["missingCalorieEstimates"]
        missing_text = f" + {missing} missing estimates" if missing else ""
        weight_text = payload["weight"]["label"] if payload["weight"] else "not logged"
        print(
            f"{payload['date']}: {payload['foodItems']} food items, "
            f"{payload['knownCalories']} known calories"
            f"{missing_text}, "
            f"workout {payload['workoutStatus']}, "
            f"weight {weight_text}"
        )


def git_sync(repo: Path, commit_message: str) -> None:
    paths = ["state.json", "data/health-log.json"]
    add = subprocess.run(["git", "add", *paths], cwd=repo, capture_output=True, text=True, check=False)
    if add.returncode != 0:
        raise SystemExit(add.stderr or add.stdout or "git add failed")
    status = subprocess.run(["git", "status", "--porcelain", *paths], cwd=repo, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        raise SystemExit(status.stderr or status.stdout or "git status failed")
    if not status.stdout.strip():
        print("git: no health data changes to commit")
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
    parser = argparse.ArgumentParser(description="Append-only structured writer for the health dashboard data store")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="Path to generated state.json snapshot")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="Path to canonical data/health-log.json event store")
    parser.add_argument("--no-sync", action="store_true", help="Write files but do not git commit/push")
    parser.add_argument("--dry-run", action="store_true", help="Print resulting JSON without writing")
    parser.add_argument("--message", default="chore: direct health log update", help="Git commit message when syncing")
    sub = parser.add_subparsers(dest="command", required=True)

    food = sub.add_parser("food", help="Add or update a food entry")
    food.add_argument("--date", default="today")
    food.add_argument("--name", required=True)
    food.add_argument("--calories", type=int)
    food.add_argument("--tone", default="warn", choices=sorted(VALID_TONES))
    food.add_argument("--note", default="")
    food.add_argument("--id", default="", help="Stable food id for same-day updates")
    food.add_argument("--raw-text", default="", help="Original chat text that produced this food event")
    food.add_argument("--session-id", default="", help="Hermes session id for chat-synced food events")
    food.add_argument("--source", default="health_log.py", help="Event source label")
    food.add_argument("--replace", action="store_true", help="Append a versioned replacement event for an existing item")
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

    history = sub.add_parser("history", help="Read day-level history from the canonical store")
    history.add_argument("--date", default="", help="today, yesterday, or YYYY-MM-DD; omit for all days")
    history.add_argument("--json", action="store_true")

    sub.add_parser("validate", help="Validate canonical store and regenerate state.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_path = Path(args.state).resolve()
    store_path = Path(args.store).resolve()
    log = load_store(store_path, state_path)

    if args.command == "history":
        print_history(materialize(log), args)
        return 0

    if args.command == "validate":
        state = materialize(log)
        validate_state(state)
        stats = validate_log_health(log)
        if args.dry_run:
            print(json.dumps({"store": normalize_log(log), "state": state}, indent=2, ensure_ascii=False))
            return 0
        save_store(store_path, state_path, log)
        print(
            f"validated {store_path} and regenerated {state_path}; "
            f"{stats['food_events']} food events "
            f"({stats['chat_food_events']} chat-synced), "
            f"{stats['missing_calorie_estimates']} missing calorie estimates"
        )
    else:
        result = args.func(log, args)
        state = materialize(log)
        validate_state(state)
        validate_log_health(log)
        if args.dry_run:
            print(result)
            print(json.dumps({"store": normalize_log(log), "state": state}, indent=2, ensure_ascii=False))
            return 0
        save_store(store_path, state_path, log)
        print(result)

    if not args.no_sync and not args.dry_run:
        git_sync(state_path.parent, args.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
