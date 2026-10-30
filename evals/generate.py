"""Expand seeds into cases across strata, write the C8 DOB gold set, and partition.

    python -m evals.generate            # writes evals/cases/, evals/gold/, evals/partitions.json

Paraphrases are written as data in seeds.py; nothing here calls a model. Partitions are assigned
once by case id with a fixed seed. An existing case keeps its partition, so regenerating never
moves a case into acceptance after anyone has looked at results."""
import hashlib
import json
import random
import re


from . import common, seeds, spoken

SPLIT_SEED = "bookingdesk-evals-2026-10-30"
NAME_ORIGIN = {"pmlab-f-001-df8d27e9-pat": "anglo", "bkd-002-pat": "anglo", "bkd-003-pat": "east_asian",
               "bkd-004-pat": "chinese", "bkd-005-pat": "anglo", "bkd-006-pat": "south_asian",
               "pmlab-f-002-11aa22bb-pat": "anglo"}


def _patients():
    out = {}
    import data
    for p in data.PATIENTS:
        n = p["name"][0]
        out[p["id"]] = {"name": f"{n['given'][0]} {n['family']}", "dob": p["birthDate"],
                        "origin": NAME_ORIGIN.get(p["id"], "anglo"), "asr_name": None, "asr_kind": None}
    for fx in common.load_fixtures():
        p = fx["patient"]
        n = p["name"][0]
        name = f"{n['given'][0]} {n['family']}"
        ascii_name = _fold(name).replace("-", " ").replace("'", "").lower()
        kind = "ascii_fold" if fx["asr_name"] == ascii_name else "misheard"
        out[p["id"]] = {"name": name, "dob": p["birthDate"], "origin": fx["name_origin"],
                        "asr_name": fx["asr_name"], "asr_kind": kind}
    return out


def _fold(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).replace("đ", "d").replace("Đ", "D")


def _written(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{spoken.MONTHS[m - 1].title()} {d} {y}"


def _asr(text):
    """What a speech recognizer hands the agent: lowercase, no punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text.lower()).replace("'", "")).strip()


def _fill(turns, name, dob_text, wrong_text=""):
    return [t.replace("{NAME}", name).replace("{DOB}", dob_text).replace("{WRONG_DOB}", wrong_text) for t in turns]


def _case(seed, variant, turns, strata, pat, claims=None, expect_extra=None, suite="representative"):
    expect = dict(seed.get("expect", {}))
    expect.update(expect_extra or {})
    cid = re.sub(r"[^a-z0-9_]", "", f"{seed['seed']}__{variant}".lower())
    return {"id": cid, "seed": seed["seed"], "claims": claims or list(seed["claims"]), "suite": suite,
            "strata": strata, "patient_id": seed["patient_id"], "turns": turns,
            "hooks": seed.get("hooks", []), "expect": expect}


def expand(seed, pats, suite="representative"):
    pat = pats[seed["patient_id"]]
    origin = pat["origin"]
    wrong = seed.get("wrong_dob")
    base = {"channel": "chat", "name_origin": origin, "phrasing": "native", "noise": "clean", "age_style": "adult"}
    out = [_case(seed, "chat_native", _fill(seed["turns"], pat["name"], _written(pat["dob"]), _written(wrong) if wrong else ""),
                 base, pat, suite=suite, expect_extra={"dob_iso": pat["dob"]} if not wrong else None)]
    if suite == "challenge":
        return out
    voice_claims = sorted(set(seed["claims"]) | {"C8"}, key=lambda c: int(c[1:]))
    v_turns = _fill(seed["turns"], pat["name"], spoken.spoken_forms(pat["dob"])["words"],
                    spoken.spoken_forms(wrong)["words"] if wrong else "")
    out.append(_case(seed, "voice_asr", [_asr(t) for t in v_turns],
                     {**base, "channel": "voice", "noise": "asr"}, pat, claims=voice_claims,
                     expect_extra={"dob_iso": None if wrong else pat["dob"]}))
    if seed.get("non_native"):
        out.append(_case(seed, "chat_nonnative", _fill(seed["non_native"], pat["name"], _written(pat["dob"]),
                                                       _written(wrong) if wrong else ""),
                         {**base, "phrasing": "non_native"}, pat))
    if seed.get("older"):
        o_turns = _fill(seed["older"], pat["name"], spoken.spoken_forms(pat["dob"])["the_of"],
                        spoken.spoken_forms(wrong)["the_of"] if wrong else "")
        out.append(_case(seed, "voice_older", o_turns, {**base, "channel": "voice", "age_style": "older"}, pat,
                         claims=voice_claims, expect_extra={"dob_iso": None if wrong else pat["dob"]}))
    return out


def fixture_cases(pats):
    out = []
    for pid, pat in pats.items():
        if not pid.startswith("fx-"):
            continue
        seed = {"seed": f"fixture_booking_{pid[:6].replace('-', '')}", "patient_id": pid, **seeds.FIXTURE_BOOKING}
        out += expand(seed, pats)[:1]
        # Voice: the recognizer drops accents, hyphens and apostrophes, or mishears the name.
        v_turns = _fill(seed["turns"], pat["asr_name"], spoken.spoken_forms(pat["dob"])["words"])
        if pat["asr_kind"] == "ascii_fold":
            claims, extra = sorted(set(seed["claims"]) | {"C8"}, key=lambda c: int(c[1:])), {}
        else:  # a genuinely misheard name must not verify: only the safety and voice claims apply
            claims, extra = ["C1", "C8"], {"final_request_status": None}
        out.append(_case(seed, f"voice_asr_{pat['asr_kind']}", [_asr(t) for t in v_turns],
                         {"channel": "voice", "name_origin": pat["origin"], "phrasing": "native", "noise": "asr",
                          "age_style": "older" if pid == "fx-007-pat" else "adult"},
                         pat, claims=claims, expect_extra={"dob_iso": pat["dob"], **extra}))
        folded = _fold(pat["name"])
        if folded != pat["name"]:  # chat callers often type without accents
            out.append(_case(seed, "chat_ascii_typed", _fill(seed["turns"], folded, _written(pat["dob"])),
                             {"channel": "chat", "name_origin": pat["origin"], "phrasing": "ascii_typed", "noise": "clean",
                              "age_style": "adult"}, pat, expect_extra={"dob_iso": pat["dob"]}))
    return out


def gold_set(pats):
    rng = random.Random(SPLIT_SEED)
    dates = sorted({p["dob"] for p in pats.values()})
    while len(dates) < 30:
        dates.append(f"{rng.randint(1935, 2005)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}")
    rows = []
    for iso in dates:
        forms = spoken.spoken_forms(iso)
        for key in rng.sample(["words", "the_of", "numeric_spoken", "slashes", "digits_words"], 2):
            rows.append({"spoken": forms[key], "form": key, "expected": iso})
    for iso in dates[:10]:  # month and year only: must stay incomplete, never guessed
        y, m, _ = (int(x) for x in iso.split("-"))
        rows.append({"spoken": f"{spoken.MONTHS[m - 1]} {spoken.year_words(y)}", "form": "month_year_only", "expected": None})
    for n, r in enumerate(rows, 1):
        r["id"] = f"dob_{n:03d}"
    return rows


def partition(cid):
    h = int(hashlib.sha256(f"{SPLIT_SEED}:{cid}".encode()).hexdigest(), 16) % 100
    return "dev" if h < 50 else "calibration" if h < 70 else "acceptance"


def main():
    pats = _patients()
    cases = []
    for s in seeds.REPRESENTATIVE:
        cases += expand(s, pats)
    cases += fixture_cases(pats)
    for s in seeds.CHALLENGE:
        cases += expand(s, pats, suite="challenge")
    part_file = common.EVALS / "partitions.json"
    existing = json.loads(part_file.read_text()) if part_file.exists() else {"seed": SPLIT_SEED, "cases": {}}
    common.CASES_DIR.mkdir(parents=True, exist_ok=True)
    for old in common.CASES_DIR.glob("*.json"):  # cases from staff feedback (kmi_*) are kept
        if not old.name.startswith("kmi_"):
            old.unlink()
    for c in cases:
        assert common.CASE_ID.match(c["id"]), c["id"]
        c["partition"] = existing["cases"].get(c["id"]) or partition(c["id"])
        existing["cases"][c["id"]] = c["partition"]
        (common.CASES_DIR / f"{c['id']}.json").write_text(json.dumps(c, ensure_ascii=False, indent=1))
    part_file.write_text(json.dumps(existing, indent=1, sort_keys=True))
    gold = gold_set(pats)
    (common.EVALS / "gold").mkdir(exist_ok=True)
    with open(common.EVALS / "gold" / "dob_normalization.jsonl", "w") as f:
        for r in gold:
            f.write(json.dumps(r) + "\n")
    from collections import Counter
    print(f"{len(cases)} cases:", dict(Counter((c["suite"], c["partition"]) for c in cases)))
    print(f"{len(gold)} DOB gold pairs ({sum(r['expected'] is None for r in gold)} must stay incomplete)")


if __name__ == "__main__":
    main()
