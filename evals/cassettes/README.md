# Eval Cassettes

`bootstrap.jsonl` is a v0.1.0 replay fixture pack. It exists so the
zero-cost replay tier exercises task loading, cell selection, metrics,
and report rendering in a clean checkout.

It is not empirical benchmark evidence. Each record is marked
`source: bootstrap_fixture_not_empirical`; the first live smoke/full
cycle should replace or supplement this pack with recorded cassette
outputs from real runs.
