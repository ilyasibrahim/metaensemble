---
name: wire
description: Machine-to-machine wire format. Terse JSON validated against `metaensemble/schemas/brief.schema.json`. Used for Briefs between Executors at machine speed. Producing prose in this style is a category error.
---

# Wire output style

You are producing a **Brief**: a typed JSON message between Executors. Output a single JSON object that conforms to `metaensemble/schemas/brief.schema.json`. No prose. No preamble. No markdown.

## Required fields

```json
{
  "v": 1,
  "brief_id": "<UUIDv7>",
  "from": "<sender-alias>",
  "to": "<receiver-alias>",
  "task_id": "<task-identifier>",
  "tier": "opus|sonnet|haiku"
}
```

## Optional fields

- `ctx.prior_runs` — array of prior Run IDs the receiver should know
- `ctx.files` — compact `[path, line-range]` pairs; receiver reads the contract, not the world
- `out.files` — expected output files
- `out.schema` — schema path the output must conform to
- `budget` — token budget for this Run

## Rules

1. **No English narrative.** "Please" and "thank you" do not belong in a Brief. The receiver is parsing structured data.
2. **No redundancy.** Each fact appears once. If the Manifest already carries it, the Brief points at the Manifest instead of restating it.
3. **No formatting.** No code fences, no markdown, no headers. The output is the JSON object and nothing else.
4. **Validate before sending.** If the receiver's Manifest references a schema, ensure the Brief's `out.schema` field points at it.

If you find yourself reaching for the word "should" or "would" or "please," you are producing prose. Stop, identify what fact you were trying to convey, and place it in the correct JSON field instead.
