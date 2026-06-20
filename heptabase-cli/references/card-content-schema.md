# Card Content Schema

## Overview

Note and journal content uses ProseMirror JSON format. Prefer Markdown for ordinary writing and appending. Use ProseMirror JSON only when you need to preserve existing structure or create schema nodes/marks that Markdown cannot express.

## Top-Level Shape

```json
{
  "type": "doc",
  "content": [/* at least one block node */]
}
```

The document must contain at least one block. `{"type":"doc","content":[]}` is invalid.

## ID Management

- When editing existing content, preserve `id` values from previous reads.
- For new blocks, omit `id` or set it to `null` — the CLI generates valid identifiers on save.
- Never create custom string IDs or invent UUIDs.

## Markdown Syntax Support

Standard Markdown maps to ProseMirror nodes automatically:

| Markdown | ProseMirror node |
| --- | --- |
| `# Heading` | `heading` (level 1–6) |
| `> quote` | `blockquote` |
| `- item` | `bullet_list` / `list_item` |
| `1. item` | `ordered_list` / `list_item` |
| `- [ ] todo` | `todo_list` / `todo_item` |
| `- <toggle>` | `toggle_list` / `toggle_item` |
| ` ```lang ``` ` | `code_block` |
| `\|table\|` | `table` |
| `**bold**` | `strong` mark |
| `_italic_` | `em` mark |
| `` `code` `` | `code` mark |

## Block Node Types

Supported block nodes include: `paragraph`, `heading`, `blockquote`, `bullet_list`, `ordered_list`, `todo_list`, `toggle_list`, `code_block`, `table`, `math_display`, `image`, `video`, `audio`, `file`, `bookmark`, and `embed`.

Block media nodes cannot appear inside a paragraph. Use inline mention nodes for inline references.

## Code Block Configuration

Code blocks use a specialized `params` format: `[!]<language>[:displayMode]`

- `!` flag enables line wrapping
- Display modes (`code`, `preview`, `split`) apply to Mermaid diagrams only

Example: `params: "!javascript"` or `params: "mermaid:preview"`

## Inline Nodes and Marks

Inline nodes: `text`, `math_inline`, `hard_break`, and mention types (card, date, whiteboard, section, tag, highlight, chat).

Marks: `strong`, `em`, `strikethrough`, `underline`, `code`, `link`, and `textColor` / `textBackgroundColor` (gray, brown, orange, yellow, green, blue, purple, pink, red).

Timestamps use ISO 8601 format, e.g. `2026-05-26T00:00:00.000Z`.

## Restrictions

- Do not create empty documents
- Do not place text in `attrs.text` — use the `text` property on text nodes
- Do not generate custom string IDs or invent UUIDs for card references
- Do not use deprecated `people` mentions
- Do not add `highlight` or `anchor` marks to new content
- Do not directly edit Heptabase local database files
- Media `reference` attributes are read-only — preserve them when editing existing JSON, never create them manually
