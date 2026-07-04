# legacy

A small library for the `recfile` record format. All of it currently
lives in `legacy/big_module.py`, which has grown three distinct
responsibilities over time:

1. **Parsing** — recfile text to `Record` values.
2. **Validation** — `Record` values against a `Schema`.
3. **Rendering** — `Record` values back to recfile text, dicts, and
   aligned tables.

The public API surface is declared in `api_manifest.json`; consumers
import exclusively via `from legacy.big_module import ...`.

Run the tests with:

```bash
pytest
```
