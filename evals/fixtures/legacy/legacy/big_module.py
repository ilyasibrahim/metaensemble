"""Parse, validate, and render the ``recfile`` record format.

A recfile is a line-oriented text format for small typed records::

    # Inventory snapshot, one block per record.
    record invoice
      id: INV-0041
      total: 129.95
      paid: yes
      issued: 2025-11-30
      memo: "quarterly true-up  # not a comment inside quotes"
    end

    record customer
      id: C-9
      name: Acme Corp
    end

Grammar, in brief:

- A record block opens with ``record <kind>`` and closes with ``end``.
  Kinds match ``[a-z][a-z0-9_-]*``.
- Each field line inside a block is ``<name>: <value>``. Names match
  ``[a-z][a-z0-9_]*`` and may not repeat within one block.
- Values are taken verbatim after the colon, trimmed. A value may be
  double-quoted; quoted values support the escapes ``\\n``, ``\\t``,
  ``\\r``, ``\\"`` and ``\\\\`` and may contain ``#`` and leading or
  trailing spaces.
- ``#`` starts a comment unless it appears inside a quoted value.
  Blank lines are ignored.
- Blocks do not nest, fields cannot appear outside a block, and an
  unterminated block is an error.

This module has grown three distinct responsibilities:

1. Parsing — text to :class:`Record` values (:func:`parse_document`,
   :func:`parse_record`).
2. Validation — :class:`Record` values against a :class:`Schema`
   (:func:`validate_record`, :func:`validate_document`,
   :func:`coerce_value`).
3. Rendering — :class:`Record` values back to recfile text, dicts, and
   tables (:func:`render_record`, :func:`render_document`,
   :func:`render_table`, :func:`render_summary`, :func:`to_dict`,
   :func:`from_dict`).

Everything importable by callers is listed in ``__all__``; underscore
names are internal and may change without notice.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

__all__ = [
    "FieldSpec",
    "ParseError",
    "Record",
    "Schema",
    "ValidationIssue",
    "coerce_value",
    "from_dict",
    "parse_document",
    "parse_record",
    "render_document",
    "render_record",
    "render_summary",
    "render_table",
    "to_dict",
    "validate_document",
    "validate_record",
]


# ---------------------------------------------------------------------------
# Parsing: recfile text -> Record values.
# ---------------------------------------------------------------------------

_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_HEADER_RE = re.compile(r"^record\s+(\S+)\s*$")
_FIELD_RE = re.compile(r"^([^\s:][^:]*?)\s*:\s*(.*)$")

_DECODE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


class ParseError(ValueError):
    """Raised when input text violates the recfile grammar.

    ``line`` is the 1-based line number of the offending input line so
    callers can point an editor at the problem.
    """

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass
class Record:
    """One parsed record block.

    ``fields`` preserves the order the fields appeared in the source
    text. ``line`` is the 1-based line number of the ``record`` header,
    or 0 for records built programmatically.
    """

    kind: str
    fields: dict[str, str] = field(default_factory=dict)
    line: int = 0


def _strip_comment(raw: str) -> str:
    """Drop a ``#`` comment that is not inside a double-quoted value.

    The scan tracks quote state character by character so a ``#``
    inside a quoted value survives, and honors backslash escapes so a
    quoted ``\\"`` does not toggle the quote state.
    """
    in_quotes = False
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if in_quotes and char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == "#" and not in_quotes:
            return raw[:index]
    return raw


def _decode_value(raw: str, line: int) -> str:
    """Decode one field value.

    Bare values are returned trimmed. Quoted values are unwrapped and
    their escapes resolved; a malformed quoted value is a
    :class:`ParseError` rather than a silently mangled string.
    """
    text = raw.strip()
    if not text.startswith('"'):
        return text
    if len(text) < 2 or not text.endswith('"'):
        raise ParseError("unterminated quoted value", line)
    body = text[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == '"':
            raise ParseError("unescaped quote inside quoted value", line)
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(body):
            raise ParseError("dangling escape at end of quoted value", line)
        code = body[index + 1]
        if code not in _DECODE_ESCAPES:
            raise ParseError(f"unknown escape \\{code}", line)
        out.append(_DECODE_ESCAPES[code])
        index += 2
    return "".join(out)


def parse_document(text: str) -> list[Record]:
    """Parse recfile text into a list of :class:`Record` values.

    Records are returned in source order. Raises :class:`ParseError`
    on the first grammar violation.
    """
    records: list[Record] = []
    current: Record | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line == "end":
            if current is None:
                raise ParseError("'end' outside a record block", number)
            records.append(current)
            current = None
            continue
        header = _HEADER_RE.match(line)
        if header is not None:
            if current is not None:
                raise ParseError("nested 'record' block", number)
            kind = header.group(1)
            if not _KIND_RE.match(kind):
                raise ParseError(f"invalid record kind {kind!r}", number)
            current = Record(kind=kind, fields={}, line=number)
            continue
        if current is None:
            raise ParseError("field outside a record block", number)
        matched = _FIELD_RE.match(line)
        if matched is None:
            raise ParseError(f"unparseable field line {line!r}", number)
        name = matched.group(1)
        if not _NAME_RE.match(name):
            raise ParseError(f"invalid field name {name!r}", number)
        if name in current.fields:
            raise ParseError(f"duplicate field {name!r}", number)
        current.fields[name] = _decode_value(matched.group(2), number)
    if current is not None:
        raise ParseError(
            "unterminated record block at end of input", current.line
        )
    return records


def parse_record(text: str) -> Record:
    """Parse text expected to contain exactly one record block."""
    records = parse_document(text)
    if len(records) != 1:
        raise ParseError(
            f"expected exactly one record, found {len(records)}", 1
        )
    return records[0]


# ---------------------------------------------------------------------------
# Validation: Record values against a Schema.
# ---------------------------------------------------------------------------

_FIELD_TYPES = ("string", "int", "float", "bool", "date", "enum")

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_TRUE_WORDS = frozenset({"yes", "true", "on", "1"})
_FALSE_WORDS = frozenset({"no", "false", "off", "0"})


@dataclass(frozen=True)
class FieldSpec:
    """Declares one field's type and constraints inside a :class:`Schema`.

    ``min_value``/``max_value`` apply to ``int`` and ``float`` fields;
    ``min_length``/``max_length`` and ``pattern`` apply to ``string``
    fields; ``choices`` is required for ``enum`` fields.
    """

    name: str
    type: str = "string"
    required: bool = True
    min_value: float | None = None
    max_value: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    choices: tuple[str, ...] | None = None
    pattern: str | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid field name {self.name!r}")
        if self.type not in _FIELD_TYPES:
            raise ValueError(f"unknown field type {self.type!r}")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"enum field {self.name!r} needs choices")
        if self.pattern is not None:
            re.compile(self.pattern)


@dataclass(frozen=True)
class Schema:
    """Declares the expected shape of every record of one kind."""

    kind: str
    fields: tuple[FieldSpec, ...] = ()
    allow_extra: bool = False

    def __post_init__(self) -> None:
        if not _KIND_RE.match(self.kind):
            raise ValueError(f"invalid record kind {self.kind!r}")
        seen: set[str] = set()
        for spec in self.fields:
            if spec.name in seen:
                raise ValueError(f"duplicate field spec {spec.name!r}")
            seen.add(spec.name)

    def field_map(self) -> dict[str, FieldSpec]:
        """Field specs keyed by field name."""
        return {spec.name: spec for spec in self.fields}


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found while validating a record.

    ``code`` is machine-readable (``missing``, ``type``, ``range``,
    ``length``, ``choice``, ``pattern``, ``unknown_field``,
    ``unknown_kind``, ``kind``); ``message`` is for humans.
    """

    record_line: int
    field_name: str
    code: str
    message: str


def _coerce_int(raw: str) -> int:
    """Parse a strict base-10 integer; no underscores, no whitespace tricks."""
    text = raw.strip()
    if not _INT_RE.match(text):
        raise ValueError(f"not an integer: {raw!r}")
    return int(text)


def _coerce_float(raw: str) -> float:
    """Parse a decimal or scientific-notation float."""
    text = raw.strip()
    if not _FLOAT_RE.match(text):
        raise ValueError(f"not a number: {raw!r}")
    return float(text)


def _coerce_bool(raw: str) -> bool:
    """Parse the recfile boolean words, case-insensitively."""
    text = raw.strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


def _coerce_date(raw: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` calendar date."""
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"not an ISO date: {raw!r}") from exc


def coerce_value(raw: str, type_name: str) -> object:
    """Convert a raw field value into its typed Python representation.

    ``string`` and ``enum`` values pass through unchanged; ``int``,
    ``float``, ``bool`` and ``date`` values are parsed strictly.
    Raises ``ValueError`` when the value does not fit the type.
    """
    if type_name in ("string", "enum"):
        return raw
    if type_name == "int":
        return _coerce_int(raw)
    if type_name == "float":
        return _coerce_float(raw)
    if type_name == "bool":
        return _coerce_bool(raw)
    if type_name == "date":
        return _coerce_date(raw)
    raise ValueError(f"unknown field type {type_name!r}")


def _issue(record: Record, spec: FieldSpec, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        record_line=record.line,
        field_name=spec.name,
        code=code,
        message=message,
    )


def _check_string(record: Record, spec: FieldSpec, raw: str) -> list[ValidationIssue]:
    """Apply length and pattern constraints to a string field."""
    issues: list[ValidationIssue] = []
    if spec.min_length is not None and len(raw) < spec.min_length:
        issues.append(_issue(
            record, spec, "length",
            f"{spec.name} is {len(raw)} chars; minimum is {spec.min_length}",
        ))
    if spec.max_length is not None and len(raw) > spec.max_length:
        issues.append(_issue(
            record, spec, "length",
            f"{spec.name} is {len(raw)} chars; maximum is {spec.max_length}",
        ))
    if spec.pattern is not None and re.fullmatch(spec.pattern, raw) is None:
        issues.append(_issue(
            record, spec, "pattern",
            f"{spec.name}={raw!r} does not match /{spec.pattern}/",
        ))
    return issues


def _check_int(record: Record, spec: FieldSpec, raw: str) -> list[ValidationIssue]:
    """Type-check an int field, then apply the numeric range."""
    try:
        value = _coerce_int(raw)
    except ValueError as exc:
        return [_issue(record, spec, "type", str(exc))]
    issues: list[ValidationIssue] = []
    if spec.min_value is not None and value < spec.min_value:
        issues.append(_issue(
            record, spec, "range",
            f"{spec.name}={value} is below minimum {spec.min_value}",
        ))
    if spec.max_value is not None and value > spec.max_value:
        issues.append(_issue(
            record, spec, "range",
            f"{spec.name}={value} is above maximum {spec.max_value}",
        ))
    return issues


def _check_float(record: Record, spec: FieldSpec, raw: str) -> list[ValidationIssue]:
    """Type-check a float field, then apply the numeric range."""
    try:
        value = _coerce_float(raw)
    except ValueError as exc:
        return [_issue(record, spec, "type", str(exc))]
    issues: list[ValidationIssue] = []
    if spec.min_value is not None and value < spec.min_value:
        issues.append(_issue(
            record, spec, "range",
            f"{spec.name}={value} is below minimum {spec.min_value}",
        ))
    if spec.max_value is not None and value > spec.max_value:
        issues.append(_issue(
            record, spec, "range",
            f"{spec.name}={value} is above maximum {spec.max_value}",
        ))
    return issues


def _check_bool(record: Record, spec: FieldSpec, raw: str) -> list[ValidationIssue]:
    """Type-check a boolean field."""
    try:
        _coerce_bool(raw)
    except ValueError as exc:
        return [_issue(record, spec, "type", str(exc))]
    return []


def _check_date(record: Record, spec: FieldSpec, raw: str) -> list[ValidationIssue]:
    """Type-check an ISO-date field."""
    try:
        _coerce_date(raw)
    except ValueError as exc:
        return [_issue(record, spec, "type", str(exc))]
    return []


def _check_enum(record: Record, spec: FieldSpec, raw: str) -> list[ValidationIssue]:
    """Check an enum field against its declared choices."""
    choices = spec.choices or ()
    if raw in choices:
        return []
    allowed = ", ".join(sorted(choices))
    return [_issue(
        record, spec, "choice",
        f"{spec.name}={raw!r} is not one of: {allowed}",
    )]


_CHECKERS = {
    "string": _check_string,
    "int": _check_int,
    "float": _check_float,
    "bool": _check_bool,
    "date": _check_date,
    "enum": _check_enum,
}


def validate_record(record: Record, schema: Schema) -> list[ValidationIssue]:
    """Validate one record against one schema.

    Returns every issue found rather than stopping at the first, so a
    caller can report a complete fix list in one pass. An empty list
    means the record is valid.
    """
    issues: list[ValidationIssue] = []
    if record.kind != schema.kind:
        issues.append(ValidationIssue(
            record_line=record.line,
            field_name="",
            code="kind",
            message=(
                f"record kind {record.kind!r} does not match "
                f"schema kind {schema.kind!r}"
            ),
        ))
        return issues
    specs = schema.field_map()
    for spec in schema.fields:
        raw = record.fields.get(spec.name)
        if raw is None:
            if spec.required:
                issues.append(_issue(
                    record, spec, "missing",
                    f"required field {spec.name!r} is absent",
                ))
            continue
        issues.extend(_CHECKERS[spec.type](record, spec, raw))
    if not schema.allow_extra:
        for name in record.fields:
            if name not in specs:
                issues.append(ValidationIssue(
                    record_line=record.line,
                    field_name=name,
                    code="unknown_field",
                    message=f"field {name!r} is not declared by schema {schema.kind!r}",
                ))
    return issues


def validate_document(
    records: Iterable[Record],
    schemas: Iterable[Schema],
) -> list[ValidationIssue]:
    """Validate a parsed document against a set of schemas.

    Each record is validated against the schema whose ``kind`` matches
    its own; a record with no matching schema yields a single
    ``unknown_kind`` issue.
    """
    by_kind: dict[str, Schema] = {}
    for schema in schemas:
        if schema.kind in by_kind:
            raise ValueError(f"duplicate schema for kind {schema.kind!r}")
        by_kind[schema.kind] = schema
    issues: list[ValidationIssue] = []
    for record in records:
        schema = by_kind.get(record.kind)
        if schema is None:
            issues.append(ValidationIssue(
                record_line=record.line,
                field_name="",
                code="unknown_kind",
                message=f"no schema declared for record kind {record.kind!r}",
            ))
            continue
        issues.extend(validate_record(record, schema))
    return issues


# ---------------------------------------------------------------------------
# Rendering: Record values -> recfile text, dicts, and tables.
# ---------------------------------------------------------------------------

_ENCODE_ESCAPES = (
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("\n", "\\n"),
    ("\t", "\\t"),
    ("\r", "\\r"),
)

_NEEDS_QUOTES_RE = re.compile(r'^\s|\s$|[#"\\\n\t\r]')


def _encode_value(value: str) -> str:
    """Encode one field value for recfile output.

    Values that would not survive a bare round-trip — empty strings,
    values with leading or trailing whitespace, and values containing
    ``#``, quotes, backslashes, or control characters — are quoted and
    escaped. Everything else is emitted verbatim.
    """
    if value == "" or _NEEDS_QUOTES_RE.search(value):
        encoded = value
        for plain, escaped in _ENCODE_ESCAPES:
            encoded = encoded.replace(plain, escaped)
        return f'"{encoded}"'
    return value


def render_record(record: Record) -> str:
    """Render one record as a recfile block, ending with a newline.

    The output parses back to an equal ``kind`` and ``fields`` mapping:
    ``parse_record(render_record(r))`` round-trips.
    """
    if not _KIND_RE.match(record.kind):
        raise ValueError(f"invalid record kind {record.kind!r}")
    lines = [f"record {record.kind}"]
    for name, value in record.fields.items():
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid field name {name!r}")
        lines.append(f"  {name}: {_encode_value(value)}")
    lines.append("end")
    return "\n".join(lines) + "\n"


def render_document(records: Sequence[Record]) -> str:
    """Render records as recfile text with one blank line between blocks."""
    return "\n".join(render_record(record) for record in records)


def to_dict(record: Record) -> dict[str, object]:
    """Convert a record to a plain dict for JSON-ish consumers."""
    return {
        "kind": record.kind,
        "line": record.line,
        "fields": dict(record.fields),
    }


def from_dict(data: Mapping[str, object]) -> Record:
    """Build a :class:`Record` from :func:`to_dict` output.

    Validates the kind, field names, and value types so a hand-built
    dict cannot smuggle an unrenderable record into the system.
    """
    kind = data.get("kind")
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise ValueError(f"invalid record kind {kind!r}")
    raw_fields = data.get("fields", {})
    if not isinstance(raw_fields, Mapping):
        raise ValueError("'fields' must be a mapping of name to string value")
    fields: dict[str, str] = {}
    for name, value in raw_fields.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"invalid field name {name!r}")
        if not isinstance(value, str):
            raise ValueError(f"field {name!r} value must be a string")
        fields[name] = value
    line = data.get("line", 0)
    if not isinstance(line, int) or line < 0:
        raise ValueError(f"invalid line number {line!r}")
    return Record(kind=kind, fields=fields, line=line)


def _column_widths(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[int]:
    """Width of each column: the widest of the header and every cell."""
    widths = [len(title) for title in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    return widths


def _format_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Left-align cells to their column widths, two-space gutter."""
    padded = [cell.ljust(width) for cell, width in zip(cells, widths)]
    return "  ".join(padded).rstrip()


def render_table(records: Sequence[Record], columns: Sequence[str]) -> str:
    """Render selected fields of the records as an aligned text table.

    The first line is the header, the second a dash rule, then one line
    per record. A record missing a column renders an empty cell.
    """
    if not columns:
        raise ValueError("render_table needs at least one column")
    header = list(columns)
    rows = [
        [record.fields.get(column, "") for column in columns]
        for record in records
    ]
    widths = _column_widths(header, rows)
    lines = [
        _format_row(header, widths),
        _format_row(["-" * width for width in widths], widths),
    ]
    lines.extend(_format_row(row, widths) for row in rows)
    return "\n".join(lines) + "\n"


def render_summary(records: Sequence[Record]) -> str:
    """One-line census of a document, e.g. ``3 records: 2 invoice, 1 customer``."""
    if not records:
        return "0 records"
    counts: dict[str, int] = {}
    for record in records:
        counts[record.kind] = counts.get(record.kind, 0) + 1
    parts = [f"{count} {kind}" for kind, count in sorted(counts.items())]
    noun = "record" if len(records) == 1 else "records"
    return f"{len(records)} {noun}: " + ", ".join(parts)
