import pytest

from simcoach.speech_numbers import expand, num2words


@pytest.mark.parametrize("n,words", [
    (0, "zero"), (7, "seven"), (10, "ten"), (13, "thirteen"), (20, "twenty"),
    (21, "twenty-one"), (52, "fifty-two"), (100, "one hundred"), (101, "one hundred one"),
    (120, "one hundred twenty"), (1000, "one thousand"), (1234, "one thousand two hundred thirty-four"),
    (-3, "minus three"),
])
def test_cardinals(n, words):
    assert num2words(n) == words


@pytest.mark.parametrize("n,words", [
    (1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "fifth"), (9, "ninth"),
    (12, "twelfth"), (20, "twentieth"), (21, "twenty-first"), (100, "one hundredth"),
])
def test_ordinals(n, words):
    assert num2words(n, to="ordinal") == words


def test_year_reads_in_pairs():
    assert num2words(1998, to="year") == "nineteen ninety-eight"
    assert num2words(2026, to="year") == "twenty twenty-six"


def test_float_reads_digits_after_the_point():
    assert num2words(0.3) == "zero point three"
    assert num2words(52.3) == "fifty-two point three"
    assert num2words(1.25) == "one point two five"


@pytest.mark.parametrize("text,spoken", [
    ("Brake later into T4.", "Brake later into turn four."),
    ("You're losing 3 tenths there.", "You're losing three tenths there."),
    ("Your best is 1:52.3, this lap was 1:53.1.",
     "Your best is one fifty-two point three, this lap was one fifty-three point one."),
    ("A 2:05.0 lap.", "A two oh-five point zero lap."),
    ("ABS is on 3, TC on 6.", "ABS is on three, TC on six."),
    ("0.3 seconds", "zero point three seconds"),
    ("-0.3 s", "minus zero point three s"),
    ("52 percent", "fifty-two percent"),
    ("52%", "fifty-two percent"),
    ("2nd", "second"), ("21st", "twenty-first"), ("3rd gear", "third gear"),
    ("Turn 10, sector 2.", "Turn ten, sector two."),
    ("T10", "turn ten"),
    ("0.35%", "zero point three five percent"),
    ("laps 3-4", "laps three to four"),
    ("T3-4", "turn three to four"),
    ("2:00.0 lap", "two minutes point zero lap"),
    ("no numbers here", "no numbers here"),
])
def test_expand(text, spoken):
    assert expand(text) == spoken


def test_expand_leaves_unknown_shapes_alone():
    assert expand("v1.0.3 and 12:34:56") == "v1.0.3 and 12:34:56"
