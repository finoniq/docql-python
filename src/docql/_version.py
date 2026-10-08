"""Package version, read from installed metadata (D-08).

The version is never a hand-edited constant: an installed distribution answers
through importlib.metadata, and a source checkout without metadata falls back
to a clearly non-release string.
"""

from importlib.metadata import PackageNotFoundError, version

__version__: str

try:
    __version__ = version("docql")
except PackageNotFoundError:  # pragma: no cover - source checkout without metadata
    __version__ = "0+unknown"
