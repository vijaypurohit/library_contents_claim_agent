"""Fuzzy scoring of book titles, authors and publishers, shared by identify and price.

Spines and catalogues write the same book differently: "Mathematics Class X" against
"Mathematics for Class 10", "R.D. Sharma" against "R. D. Sharma". Both sides are normalised first
(case, punctuation, Roman class numbers, initials), then compared with RapidFuzz. Scores are 0-100.

The combined score is deliberately strict:
- a title alone can't reach the review threshold, because generic titles such as "Social Science"
  or "Inorganic Chemistry" exist from many authors; without an agreeing author it stays a spine reading
- an author that clearly disagrees caps the score low: it is a different book with a similar title
- numbers in the title read from the spine (class, part, volume) must all appear in the catalogue
  title: "Mathematics XI" is not "Mathematics Class 8", however similar the words
Non-Latin scripts (e.g. Devanagari) pass through normalisation unchanged apart from case and punctuation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from rapidfuzz import fuzz

IDENTIFIED = 90.0          # at or above: the catalogue record is taken as this book
REVIEW = 75.0              # between REVIEW and IDENTIFIED: taken, but a person should check
TITLE_ONLY_CAP = 70.0      # best score without an author on both sides: below REVIEW, so never "identified"
AUTHOR_CONFLICT = 50.0     # author similarity below this means a different book ...
CONFLICT_CAP = 60.0        # ... and the score is capped here
PUBLISHER_MATCH = 85.0     # publisher similarity needed to tie a spine to one catalogue edition
TITLE_WEIGHT = 0.8         # the title decides; an agreeing author confirms it

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
          "ix": "9", "x": "10", "xi": "11", "xii": "12"}
_STOP = {"the", "a", "an", "for", "of", "by", "and", "&"}


def _strip_punctuation(text: str) -> str:
    """Replace punctuation and symbols with spaces. Uses Unicode categories rather than a regex
    word class, because Devanagari vowel signs are combining marks that a regex word class drops."""
    return "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text)


def normalise(text: str | None) -> str:
    """Lower-case, punctuation-free, Roman class numbers as digits, stop words removed."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower().replace("&", " and ")
    words = _strip_punctuation(text).split()
    return " ".join(_ROMAN.get(w, w) for w in words if w not in _STOP)


def normalise_name(name: str | None) -> str:
    """A person's name with initials joined, so "R. D. Sharma" and "R.D. Sharma" both become "rd sharma"."""
    words = _strip_punctuation(unicodedata.normalize("NFKC", name or "").lower()).split()
    joined: list[str] = []
    previous_was_initial = False
    for word in words:
        initial = len(word) == 1
        if initial and previous_was_initial:
            joined[-1] += word          # a run of single letters is one block of initials
        else:
            joined.append(word)
        previous_was_initial = initial
    return " ".join(joined)


def title_score(read: str | None, candidate: str | None) -> float:
    """Similarity of two titles. Mostly a whole-title comparison, plus a little of a word-set one, so
    an extra word ("for") costs little but a distinguishing word ("ICSE Mathematics" against
    "Mathematics") keeps the match below the identified threshold."""
    a, b = normalise(read), normalise(candidate)
    if not a or not b:
        return 0.0
    return 0.25 * fuzz.token_set_ratio(a, b) + 0.75 * fuzz.token_sort_ratio(a, b)


def author_score(read: str | None, candidates: Sequence[str]) -> float | None:
    """Best similarity between any author read from the spine and any catalogue author; None if either side is empty."""
    # Names that are only punctuation normalise to "" and are dropped.
    read_names = [n for n in (normalise_name(n) for n in re.split(r",|;|\band\b|&", read or "")) if n]
    candidate_names = [n for n in (normalise_name(c) for c in candidates) if n]
    if not read_names or not candidate_names:
        return None
    best = 0.0
    for r in read_names:
        for c in candidate_names:
            surname_match = r.split()[-1] == c.split()[-1]
            best = max(best, fuzz.token_set_ratio(r, c), 90.0 if surname_match else 0.0)
    return best


def level_numbers(text: str | None) -> set[str]:
    """Class, part and volume numbers in a text: Roman numerals ("XI", "Part-I") and numbers right
    after a level word ("Class 10"). Other numbers, such as a code "CHA-002", are ignored."""
    words = unicodedata.normalize("NFKC", text or "").lower().replace("-", " ")
    words = _strip_punctuation(words).split()
    found = set()
    for i, word in enumerate(words):
        after_level_word = i > 0 and words[i - 1] in _LEVEL_WORDS
        if word in _ROMAN and (word not in _AMBIGUOUS_ROMAN or after_level_word):
            found.add(_ROMAN[word])
        elif word.isdigit() and after_level_word:
            found.add(word)
    return found


def numbers_agree(read: str | None, candidate: str | None) -> bool:
    """True when every class/part number read from the spine also appears in the candidate title."""
    return level_numbers(read) <= ({w for w in normalise(candidate).split() if w.isdigit()} | level_numbers(candidate))


_LEVEL_WORDS = {"class", "std", "standard", "grade", "part", "vol", "volume", "edition", "new"}
_AMBIGUOUS_ROMAN = {"i", "v", "x"}   # also a word, an initial or a letter: a number only after a level word


def core_title(title: str | None) -> str:
    """A title without class, part and edition labels or numbers, for a broader catalogue search:
    "MATHEMATICS CLASS XI" -> "mathematics". The full title is still used for scoring."""
    return " ".join(w for w in normalise(title).split() if w not in _LEVEL_WORDS and not w.isdigit())


def book_score(read_title: str | None, read_author: str | None, title: str | None, authors: Sequence[str],
               read_text: str | None = None) -> float:
    """How likely a catalogue record is the book read from the spine, 0-100.

    `read_text` is all text read from the spine; class and part numbers in it must agree too, since a
    reader may put "XI" in the text but leave it out of the title."""
    t = title_score(read_title, title)
    if not numbers_agree(f"{read_title or ''} / {read_text or ''}", title):
        t = min(t, CONFLICT_CAP)
    a = author_score(read_author, authors)
    if a is None:
        return min(t, TITLE_ONLY_CAP)
    if a < AUTHOR_CONFLICT:
        return min(t, CONFLICT_CAP)
    return TITLE_WEIGHT * t + (1 - TITLE_WEIGHT) * a


def publisher_matches(read: str | None, candidate: str | None) -> bool:
    a, b = normalise(read), normalise(candidate)
    return bool(a and b) and fuzz.token_set_ratio(a, b) >= PUBLISHER_MATCH
