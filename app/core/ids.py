"""Identifier generation.

Primary keys are minted in Python, not by Postgres, so a repository knows a row's
id before the INSERT — it goes into the log line and the response without a
``RETURNING`` round-trip. The columns still carry ``gen_random_uuid()`` as a
server default so a hand-written ``INSERT`` in psql is never invalid.

UUIDv4 is deliberate. UUIDv7's time-ordering would pack the B-tree better, but
it also leaks row creation times to anyone holding an id, and none of the tables
here are write-heavy enough for the index locality to matter.
"""

from __future__ import annotations

import uuid
from uuid import UUID


def new_uuid() -> UUID:
    """A fresh random identifier for a new row."""
    return uuid.uuid4()
