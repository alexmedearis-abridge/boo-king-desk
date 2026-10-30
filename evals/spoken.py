"""Spoken-date helpers shared by the generator (to write voice variants) and graders
(to recognize a caller-stated DOB). This is a reference for the eval, not the agent's normalizer:
the agent normalizes dates itself inside its find_patient call."""
import re

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
       "nine": "ninth", "twelve": "twelfth", "twenty": "twentieth", "thirty": "thirtieth"}


def words(n):
    if n < 20:
        return ONES[n]
    t, o = divmod(n, 10)
    return TENS[t] + (f" {ONES[o]}" if o else "")


def ordinal(n):
    w = words(n).split()
    last = w[-1]
    w[-1] = ORD.get(last, last + "th")
    return " ".join(w)


def year_words(y):
    hi, lo = divmod(y, 100)
    if y >= 2000 and y < 2010:
        return "two thousand" + (f" {words(lo)}" if lo else "")
    if lo == 0:
        return f"{words(hi)} hundred"
    return f"{words(hi)} {'oh ' + words(lo) if lo < 10 else words(lo)}"


def spoken_forms(iso):
    """Several ways a caller (or a speech recognizer) renders one date."""
    y, m, d = (int(x) for x in iso.split("-"))
    mon = MONTHS[m - 1]
    return {
        "words": f"{mon} {ordinal(d)} {year_words(y)}",
        "the_of": f"the {ordinal(d)} of {mon} {year_words(y)}",
        "digits_words": f"{mon} {d} {y}",
        "slashes": f"{m}/{d}/{y}",
        "numeric_spoken": f"{words(m)} {words(d)} {year_words(y)}",
    }


def _num(tokens):
    """Parse a run of number words (cardinal or ordinal) into an int, or None."""
    val, seen = 0, False
    for tok in tokens:
        t = tok
        for card, o in ORD.items():
            if t == o:
                t = card
        if t.endswith("th") and t[:-2] in ONES + TENS:
            t = t[:-2]
        if t.endswith("ieth") and (t[:-4] + "y") in TENS:
            t = t[:-4] + "y"
        if t in ONES and t:
            val += ONES.index(t); seen = True
        elif t in TENS and t:
            val += TENS.index(t) * 10; seen = True
        elif t.isdigit():
            val += int(t); seen = True
        else:
            return None
    return val if seen else None


def parse_spoken_date(text):
    """Best-effort ISO date from what a caller said, or None. Used only to recognize echoes."""
    t = text.lower().replace(",", " ")
    m = re.search(r"\b((?:19|20)\d\d)-(\d\d)-(\d\d)\b", t)
    if m:
        return m.group(0)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b", t)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    t = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", t)
    for mi, mon in enumerate(MONTHS, 1):
        for pat in (rf"\b{mon}\s+(?:the\s+)?([a-z0-9 ]+?)\s+((?:19|20)\d\d|(?:nineteen|two thousand)[a-z ]*)\b",
                    rf"\bthe\s+([a-z0-9 ]+?)\s+of\s+{mon}\s+((?:19|20)\d\d|(?:nineteen|two thousand)[a-z ]*)\b"):
            mm = re.search(pat, t)
            if not mm:
                continue
            d = _num(mm.group(1).split())
            yr = mm.group(2).strip()
            if yr.isdigit():
                y = int(yr)
            elif yr.startswith("two thousand"):
                rest = yr[len("two thousand"):].split()
                y = 2000 + (_num(rest) or 0)
            else:
                rest = yr[len("nineteen"):].replace("oh ", "").split()
                y = 1900 + (_num(rest[:2]) or 0) if rest else None
            if d and y and 1 <= d <= 31:
                return f"{y}-{mi:02d}-{d:02d}"
    return None


def dob_text_forms(iso):
    """Written forms an agent reply might use for a date (for disclosure checks)."""
    y, m, d = (int(x) for x in iso.split("-"))
    mon = MONTHS[m - 1]
    return [iso, f"{m}/{d}/{y}", f"{m:02d}/{d:02d}/{y}", f"{mon} {d} {y}", f"{mon[:3]} {d} {y}", f"{d} {mon} {y}",
            spoken_forms(iso)["words"]]
