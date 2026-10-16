"""Title/author matching and catalogue dimension parsing."""

import pytest

from claimagent.text.match import (IDENTIFIED, REVIEW, book_score, core_title, normalise, normalise_name,
                                   numbers_agree, publisher_matches)
from claimagent.text.units import book_height_cm


def test_normalise_handles_roman_class_numbers_punctuation_and_devanagari():
    assert normalise("Mathematics Class X, Part-I") == "mathematics class 10 part 1"
    assert normalise("सामान्य हिन्दी!") == "सामान्य हिन्दी"   # vowel signs survive
    assert normalise_name("R. D. Sharma") == normalise_name("R.D. Sharma") == "rd sharma"


def test_core_title_drops_level_labels():
    assert core_title("MATHEMATICS CLASS XI") == "mathematics"
    assert core_title("B.Sc. Part-I Inorganic Chemistry") == "b sc inorganic chemistry"


def test_exact_title_and_author_is_identified():
    assert book_score("Word Power Made Easy", "Norman Lewis", "Word Power Made Easy", ["Norman Lewis"]) >= IDENTIFIED


def test_title_alone_never_reaches_review():
    assert book_score("Inorganic Chemistry", None, "Inorganic chemistry", ["James E. Huheey"]) < REVIEW


def test_disagreeing_author_caps_the_score():
    assert book_score("Mathematics XI", "R.D. Sharma", "Mathematics XI", ["R. S. Aggarwal"]) < REVIEW


def test_class_number_must_agree():
    assert not numbers_agree("Mathematics XI", "Mathematics class 8")
    assert numbers_agree("Mathematics", "Mathematics class 8")
    assert book_score("Mathematics XI", "R.D. Sharma", "Mathematics class 8", ["Rd Sharma"]) < REVIEW


def test_extra_distinguishing_word_lands_in_review_not_identified():
    score = book_score("MATHEMATICS", "MANJEET SINGH", "I.C.S.E.Mathematics", ["Manjeet Singh"])
    assert REVIEW <= score < IDENTIFIED


def test_publisher_match():
    assert publisher_matches("Dhanpat Rai Publications", "DHANPAT RAI PUBLICATIONS (P) LTD")
    assert not publisher_matches("Penguin", "Goyal Publishers")
    assert not publisher_matches(None, "Penguin")


@pytest.mark.parametrize("text,cm", [
    ("24.00 cm", 24.0), ("10,5x18 cm", 18.0), ("24 x 16 x 2 centimeters", 24.0),
    ("6.8 x 3.9 x 1.3 inches", 17.3), ("240 mm", 24.0), ("about 2 cm", None), ("big", None), (None, None),
])
def test_book_height_parsing(text, cm):
    assert book_height_cm(text) == cm
