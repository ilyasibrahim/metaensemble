"""Behavioral tests for ``legacy.big_module``.

Every import goes through the public API surface listed in
``api_manifest.json``; nothing here reaches for underscore names, so
the suite survives an internal reorganization that preserves the API.
"""
import pytest

from legacy.big_module import (
    FieldSpec,
    ParseError,
    Record,
    Schema,
    coerce_value,
    from_dict,
    parse_document,
    parse_record,
    render_record,
    render_table,
    to_dict,
    validate_document,
    validate_record,
)

INVOICE_SCHEMA = Schema(
    kind="invoice",
    fields=(
        FieldSpec(name="id", type="string", pattern=r"INV-\d+"),
        FieldSpec(name="total", type="float", min_value=0),
        FieldSpec(name="paid", type="bool"),
        FieldSpec(name="issued", type="date", required=False),
        FieldSpec(name="status", type="enum", required=False,
                  choices=("draft", "sent", "settled")),
        FieldSpec(name="memo", type="string", required=False, max_length=80),
    ),
)

DOCUMENT = """\
# two records, one comment
record invoice
  id: INV-0041
  total: 129.95
  paid: yes
end

record customer
  id: C-9
  name: Acme Corp
end
"""


def test_parse_document_reads_two_records_in_order():
    records = parse_document(DOCUMENT)
    assert [r.kind for r in records] == ["invoice", "customer"]
    assert records[0].fields == {"id": "INV-0041", "total": "129.95", "paid": "yes"}
    assert records[1].fields == {"id": "C-9", "name": "Acme Corp"}


def test_parse_error_reports_line_of_unterminated_block():
    with pytest.raises(ParseError) as excinfo:
        parse_record("record invoice\n  id: INV-1\n")
    assert excinfo.value.line == 1
    assert "unterminated" in str(excinfo.value)


def test_parse_rejects_duplicate_field():
    text = "record invoice\n  id: INV-1\n  id: INV-2\nend\n"
    with pytest.raises(ParseError) as excinfo:
        parse_record(text)
    assert excinfo.value.line == 3
    assert "duplicate" in str(excinfo.value)


def test_comments_and_blank_lines_are_ignored():
    text = "\n# heading\nrecord invoice  # trailing\n  id: INV-7\n\nend\n"
    record = parse_record(text)
    assert record.kind == "invoice"
    assert record.fields == {"id": "INV-7"}


def test_quoted_values_preserve_hash_and_padding():
    text = 'record invoice\n  memo: " padded # not a comment \\"q\\" "\nend\n'
    record = parse_record(text)
    assert record.fields["memo"] == ' padded # not a comment "q" '


def test_validate_record_flags_missing_required_field():
    record = parse_record("record invoice\n  id: INV-1\n  paid: no\nend\n")
    issues = validate_record(record, INVOICE_SCHEMA)
    assert [(i.field_name, i.code) for i in issues] == [("total", "missing")]


def test_validate_record_checks_float_range_and_enum_choice():
    record = parse_record(
        "record invoice\n  id: INV-1\n  total: -3\n  paid: yes\n"
        "  status: archived\nend\n"
    )
    issues = validate_record(record, INVOICE_SCHEMA)
    assert {(i.field_name, i.code) for i in issues} == {
        ("total", "range"),
        ("status", "choice"),
    }


def test_validate_document_reports_unknown_kind():
    records = parse_document(DOCUMENT)
    issues = validate_document(records, [INVOICE_SCHEMA])
    assert [(i.code, i.record_line) for i in issues] == [("unknown_kind", 8)]


def test_coerce_value_covers_every_type():
    assert coerce_value("42", "int") == 42
    assert coerce_value("129.95", "float") == pytest.approx(129.95)
    assert coerce_value("yes", "bool") is True
    assert coerce_value("2025-11-30", "date").isoformat() == "2025-11-30"
    assert coerce_value("as-is", "string") == "as-is"
    with pytest.raises(ValueError):
        coerce_value("4x2", "int")


def test_render_record_round_trips_through_parse():
    original = Record(
        kind="invoice",
        fields={"id": "INV-9", "memo": ' tricky # "value" \t', "total": "5.00"},
    )
    reparsed = parse_record(render_record(original))
    assert reparsed.kind == original.kind
    assert reparsed.fields == original.fields


def test_render_table_aligns_columns():
    records = parse_document(DOCUMENT)
    table = render_table(records, ["id", "total"])
    lines = table.splitlines()
    assert lines[0].split() == ["id", "total"]
    assert set(lines[1]) <= {"-", " "}
    # Both data rows start their `total` column at the same offset.
    assert lines[2].index("129.95") == len("INV-0041") + 2


def test_to_dict_from_dict_round_trip():
    record = parse_document(DOCUMENT)[0]
    rebuilt = from_dict(to_dict(record))
    assert rebuilt == record
    with pytest.raises(ValueError):
        from_dict({"kind": "Bad Kind", "fields": {}})
