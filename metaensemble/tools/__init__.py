"""MetaEnsemble Principal-facing CLI tools.

Each tool is a small Python script that queries the Ledger through the
named-API in `metaensemble/lib/ledger.py` and renders Markdown for the Coordinator
to relay to the Principal. Tools are invoked by slash commands in
`metaensemble/commands/`.
"""
