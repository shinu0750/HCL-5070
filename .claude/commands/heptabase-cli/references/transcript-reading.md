# Transcript Reading

## Common Usage Pattern

1. Find audio/video card IDs:

```bash
heptabase card list --card-types audio,video --limit 20
heptabase card list -q "<keyword>" --card-types audio,video --limit 20
```

2. Read metadata before content:

```bash
heptabase audio metadata <audioCardId>
heptabase video metadata <videoCardId>
```

Check `transcriptStatus` and `durationSeconds` before reading.

3. Read transcript in time windows:

```bash
heptabase audio read <audioCardId> --start-seconds 0 --end-seconds 600
heptabase video read <videoCardId> --start-seconds 0 --end-seconds 600
```

## Pagination Guidance

- Always call metadata first.
- Read commands return entries that overlap the requested inclusive range, not only entries that start inside it. A segment from 55–65 seconds will appear when querying 60–120 seconds.
- Read 10-minute windows (600 seconds) by default to avoid burning through tokens.
- Ask the user before requesting significantly more than 1 hour of content at once.

## When To Use `audio/video read` Vs `file export`

- Use `audio read` / `video read` for textual analysis of parsed transcript entries.
- Use `file export` only when you need to inspect the raw media file with a native application. This is rarely needed.

## Troubleshooting

- `transcriptStatus: "processing"`: transcript is still being generated; retry later.
- `transcriptStatus: "failed"`: parsed content is unavailable for this card.
- `transcriptStatus: null`: no transcript exists yet; ask the user to generate one in Heptabase first.
