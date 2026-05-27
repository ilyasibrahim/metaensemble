"""MetaEnsemble core package.

Project-agnostic substrate: schemas, SQLite Ledger, identity generation,
and Manifest validation. See ARCHITECTURE.md and PERFORMANCE.md at the
repo root for the binding design and engineering contracts.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("metaensemble")
except PackageNotFoundError:
    __version__ = "0.1.0"
