"""ULID utility.

Wraps the maintained ``python-ulid`` package. A ULID is a 26-character Crockford
base32 string that is lexicographically sortable by creation time.
"""

from ulid import ULID


def new_ulid() -> str:
    """Return a new 26-character ULID string."""
    return str(ULID())
