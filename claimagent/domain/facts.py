"""Facts the user states during a sweep, which change how the packet treats a line.

The live agent records them (tool `record_user_fact`) into runs/<id>/facts.json; they can also be
written by hand. The packet stage applies them:
    skip_shelf     "don't include the top shelf"       -> that shelf's books are left out, with a review entry
    first_edition  "that one is a first edition"       -> the book goes to appraisal, unpriced
    signed         "it's signed by the author"         -> the book goes to appraisal, unpriced
    print          "the painting is just a print"      -> an art item may be priced like any item
    note           anything else worth keeping         -> copied to the review queue
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

FactKind = Literal["skip_shelf", "first_edition", "signed", "print", "note"]


class UserFact(BaseModel):
    kind: FactKind
    target: str | None = None     # a book id ("A1-3"), a shelf ("A1"), an item id ("I2"), or None
    text: str                     # what the user said, as recorded


def load_facts(path: Path) -> list[UserFact]:
    """The sweep's facts, or none if the file does not exist."""
    if not path.exists():
        return []
    return TypeAdapter(list[UserFact]).validate_python(json.loads(path.read_text()))
