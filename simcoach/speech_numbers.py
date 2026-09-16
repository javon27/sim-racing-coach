"""Number-to-words for the coach's SPOKEN text (Kokoro path).

Two jobs: `expand(text)` rewrites the numeric shapes the coach actually says into
words BEFORE grapheme-to-phoneme (misaki drops the colon sub-token in "1:52.3", so lap
times must be words by the time it sees them), and `num2words(n, to=...)` provides the
call signature misaki itself uses, so the LGPL `num2words` package is never installed:
`simcoach.tts_kokoro` registers this module under that name. Pure; no dependencies.
"""
from __future__ import annotations

import re

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORD_ONES = {"one": "first", "two": "second", "three": "third", "five": "fifth",
             "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def _below_thousand(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    return f"{_ONES[hundreds]} hundred" + (f" {_below_thousand(rest)}" if rest else "")


def _cardinal(n: int) -> str:
    if n < 0:
        return "minus " + _cardinal(-n)
    if n < 1000:
        return _below_thousand(n)
    parts = []
    for scale, name in _SCALES:
        q, n = divmod(n, scale)
        if q:
            parts.append(f"{_below_thousand(q)} {name}")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def _ordinal(n: int) -> str:
    words = _cardinal(n)
    head, sep, last = words.rpartition("-") if "-" in words.rsplit(" ", 1)[-1] else words.rpartition(" ")
    if last in _ORD_ONES:
        last_ord = _ORD_ONES[last]
    elif last.endswith("y"):
        last_ord = last[:-1] + "ieth"
    else:
        last_ord = last + "th"
    return f"{head}{sep}{last_ord}"


def _year(n: int) -> str:
    if 1000 <= n <= 9999 and n % 100:
        hi, lo = divmod(n, 100)
        return f"{_cardinal(hi)} {_cardinal(lo) if lo >= 10 else 'oh-' + _ONES[lo]}"
    if 1000 <= n <= 9999:
        return f"{_cardinal(n // 100)} hundred"
    return _cardinal(n)


def num2words(n: float, to: str = "cardinal") -> str:
    """misaki-compatible: `to` in {"cardinal", "ordinal", "year"}; floats read the
    digits after the point one by one ("52.3" -> "fifty-two point three")."""
    if isinstance(n, float) and not n.is_integer():
        whole, _, frac = repr(n).partition(".")
        return f"{_cardinal(int(whole))} point " + " ".join(_ONES[int(d)] for d in frac)
    n = int(n)
    if to == "ordinal":
        return _ordinal(n)
    if to == "year":
        return _year(n)
    return _cardinal(n)


def _lap_time(m: re.Match) -> str:
    minutes, seconds, tenths = int(m.group(1)), int(m.group(2)), m.group(3)
    if seconds == 0:
        secs = "minutes"
    else:
        secs = _cardinal(seconds) if seconds >= 10 else f"oh-{_ONES[seconds]}"
    out = f"{_cardinal(minutes)} {secs}"
    if tenths is not None:
        out += " point " + " ".join(_ONES[int(d)] for d in tenths)
    return out


def _decimal(m: re.Match) -> str:
    sign = "minus " if m.group(1) else ""
    suffix = " percent" if m.group(4) else ""
    return sign + f"{_cardinal(int(m.group(2)))} point " + " ".join(_ONES[int(d)] for d in m.group(3)) + suffix


def _turn(m: re.Match) -> str:
    out = f"turn {_cardinal(int(m.group(1)))}"
    if m.group(2):
        out += f" to {_cardinal(int(m.group(2)))}"
    return out


# Order matters: lap times before decimals, T-prefix and ranges before bare
# integers. Lookaheads accept a sentence-final number: not followed by a word
# char, nor by "." or ":" followed by a digit (so "sector 2." and "1:53.1."
# match).
_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?:\.(\d+))?(?!\w|[.:]\d)"), _lap_time),
    (re.compile(r"(?<![\w.])(-?)(\d+)\.(\d+)(%)?(?!\d|[.:]\d)"), _decimal),
    (re.compile(r"(?<![\w-])(\d+)(st|nd|rd|th)\b"), lambda m: _ordinal(int(m.group(1)))),
    (re.compile(r"\bT(\d{1,2})(?:-(\d{1,2}))?\b"), _turn),
    (re.compile(r"(?<![\w.])(-?)(\d+)%"), lambda m: ("minus " if m.group(1) else "") + f"{_cardinal(int(m.group(2)))} percent"),
    (re.compile(r"(?<![\w.:-])(\d+)-(\d+)(?!\w|[.:]\d)"), lambda m: f"{_cardinal(int(m.group(1)))} to {_cardinal(int(m.group(2)))}"),
    (re.compile(r"(?<![\w.:-])(-?)(\d+)(?!\w|[.:]\d)"), lambda m: ("minus " if m.group(1) else "") + _cardinal(int(m.group(2)))),
]


def expand(text: str) -> str:
    """Rewrite the numeric shapes the coach speaks into words; leave any other shape
    (version strings, hh:mm:ss) untouched."""
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
