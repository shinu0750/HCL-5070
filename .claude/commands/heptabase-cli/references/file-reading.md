# File Reading

## Common Usage Pattern

1. List exportable files from a PDF or media card:

```bash
heptabase file list --card-id <cardId>
```

Unsupported card types return an empty `files` array.

2. Create a scratch directory:

```bash
mktemp -d
```

3. Export the file to the scratch directory:

```bash
heptabase file export <fileId> --output-dir <scratchDir>
```

4. Read only the returned `path` with your native file-reading tool.

5. Delete the scratch directory after finishing.

## Before Reading

Check file metadata (size, MIME type, filename) before reading large files. For textual PDF reads, prefer `references/pdf-reading.md` and `heptabase pdf read` rather than exporting the raw PDF binary.

## Troubleshooting

- If `file list` returns an empty array, the card type may not be supported or the file may not be available locally.
- If export fails, ask the user to sync the file in Heptabase before retrying.
- Never read internal Heptabase file paths directly — only use the path returned by `heptabase file export`.
