"""MetaEnsemble evaluation runners.

Three tiers:
- replay: cassette-based, no API spend. PR gate.
- smoke:  one seed × classification smoke set. Nightly.
- full:   N seeds × every cell × every suite. Release gate.

Modules:
- api: tiered runner dispatch.
- metrics: Wilson CI, pass@budget, quality_per_1k_tokens, overhead ratio.
- replay: cassette reader for the PR tier.
"""
