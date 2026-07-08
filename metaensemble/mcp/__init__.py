"""Read-only MCP surface for the MetaEnsemble Ledger.

The `queries` submodule provides pure, MCP-free serialization functions
over the Ledger; a thin server layer imports them to expose institutional
memory to any MCP client. Nothing in this package writes to the Ledger —
the query layer opens SQLite read-only, so there is no write surface.
"""
