# Health data store

`health-log.json` is the canonical durable store for daily health history.

- It is an append-only event log: food, workout, weight, sleep, and note updates are added as entries with a `date`, `type`, `action`, and `recordedAt` timestamp.
- Same-day edits use stable IDs (for example `foodId`) and append a replacement/upsert event instead of rewriting old history.
- `state.json` is generated from this log as a compatibility snapshot for the static GitHub Pages dashboard and older consumers.
- Use `python scripts/health_log.py ...` to write or read the store; do not reconstruct history from Hermes session search.
- The health profile hook `chat-health-sync` calls this logger for explicit food chat (for example "I had...", "Food:", "Lunch:"). Chat-synced food events include `rawText`, `sessionId`, and parsed `food` fields when available; meta chat about the dashboard/hook is ignored unless the message has an explicit food label.

Useful commands:

```sh
python scripts/health_log.py --no-sync validate
python scripts/health_log.py history --date today
python scripts/health_log.py history --date yesterday --json
```
