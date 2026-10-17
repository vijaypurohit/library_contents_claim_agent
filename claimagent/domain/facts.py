"""Facts the user states during a sweep, which change how the packet treats a line.

The live agent records them (tool `record_user_fact`) into runs/<id>/facts.json; they can also be
written by hand. The packet stage applies them:
    skip_shelf     "don't include the top shelf"       -> that shelf's books are left out, with a review entry
    first_edition  "that one is a first edition"       -> the book goes to appraisal, unpriced
    signed         "it's signed by the author"         -> the book goes to appraisal, unpriced
    print          "the painting is just a print"      -> an art item is priced like any item (price stage)
    note           anything else worth keeping         -> copied to the review queue
A fact whose target matches no shelf, book or item is not dropped: it goes to the review queue so
a person can apply it by hand.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter, field_validator

ID_LIKE = re.compile(r"[A-Z]\d+(-\d+)?|I\d+")   # a shelf ("A2"), a book ("A2-3") or an item ("I4")

FactKind = Literal["skip_shelf", "first_edition", "signed", "print", "note"]


class UserFact(BaseModel):
    kind: FactKind
    target: str | None = None     # a book id ("A1-3"), a shelf ("A1"), an item id ("I2"), or None
    text: str                     # what the user said, as recorded

    @field_validator("target")
    @classmethod
    def _tidy_target(cls, value: str | None) -> str | None:
        """Ids are upper case without spaces ("a1 - 3" -> "A1-3"). Anything else ("the bottom shelf")
        is kept as spoken, trimmed, so the review queue can show it; an empty target means none."""
        value = (value or "").strip()
        compact = "".join(value.split()).upper()
        return compact if ID_LIKE.fullmatch(compact) else (value or None)


def load_facts(path: Path) -> list[UserFact]:
    """The sweep's facts, or none if the file does not exist."""
    if not path.exists():
        return []
    return TypeAdapter(list[UserFact]).validate_python(json.loads(path.read_text()))
