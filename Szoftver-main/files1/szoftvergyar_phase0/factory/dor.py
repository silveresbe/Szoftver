"""Definition of Ready (12.4/A): a SPEC_READY kimenet kódszintű ellenőrzése. Nulla token, nulla hálózat.
Ellenőrzi: Given/When/Then szerkezet, mérőszám nélküli homályos szavak, storynkénti hibaviselkedés és edge-case,
'benne van' / 'nincs benne' lista, feltevések megerősítésre jelölése, legfeljebb 3 nyitott kérdés."""
from __future__ import annotations
import re

# A tő + tetszőleges toldalék ("gyorsan", "megfelelően"). A 'gyorsítótár' valódi szakszó, nem homályos jelző.
VAGUE = {
    "gyors": r"gyors(?!ítótár)", "szép": r"szép", "megfelelő": r"megfelelő", "felhasználóbarát": r"felhasználóbarát",
    "könnyű": r"könny", "stabil": r"stabil", "rugalmas": r"rugalmas",
    "fast": r"fast", "nice": r"nice", "appropriate": r"appropriate", "user-friendly": r"user-friendly",
    "easy": r"easy|easily", "stable": r"stable", "flexible": r"flexible",
}
_VAGUE_RX = {w: re.compile(r"(?<!\w)(?:%s)\w*" % rx, re.I | re.U) for w, rx in VAGUE.items()}
_DIGIT = re.compile(r"\d")
MAX_QUESTIONS = 3

def vague_words(text: str) -> list[str]:
    """A szövegben talált homályos szavak, HA a szöveg nem tartalmaz egyetlen mérőszámot (számjegyet) sem."""
    if _DIGIT.search(text or ""):
        return []
    return sorted(w for w, rx in _VAGUE_RX.items() if rx.search(text or ""))

def _blank(v) -> bool:
    return not isinstance(v, str) or not v.strip()

def check(spec: dict) -> list[str]:
    """Hibalista (üres = kész). A hibaüzenetek rövidek, azonosítóval: ezek mennek a DOR_FAILED üzenetbe."""
    errs: list[str] = []
    stories = spec.get("stories") or []
    acs = spec.get("acceptance_criteria") or []
    if not stories:
        errs.append("nincs story")
    if not acs:
        errs.append("nincs acceptance criteria")
    sid = [s.get("id") for s in stories]
    if len(set(sid)) != len(sid):
        errs.append("a story-azonosítók nem egyediek")
    aid = [a.get("id") for a in acs]
    if len(set(aid)) != len(aid):
        errs.append("az AC-azonosítók nem egyediek")
    for a in acs:
        i = a.get("id", "?")
        for part in ("given", "when", "then"):
            if _blank(a.get(part)):
                errs.append(f"{i}: hiányzik a '{part}' (Given/When/Then szerkezet kell)")
        if a.get("story") not in sid:
            errs.append(f"{i}: ismeretlen story: {a.get('story')}")
        for part in ("given", "when", "then"):      # mezőnként: egy másik mezőben álló szám nem menti fel a homályos szót
            v = vague_words(str(a.get(part, "")))
            if v:
                errs.append(f"{i}: mérőszám nélküli homályos szó a(z) '{part}' mezőben: {', '.join(v)}")
    for s in stories:
        mine = [a for a in acs if a.get("story") == s.get("id")]
        kinds = {a.get("kind") for a in mine}
        if not mine:
            errs.append(f"{s.get('id')}: nincs hozzá AC")
        if mine and "error" not in kinds:
            errs.append(f"{s.get('id')}: hiányzik a hibaviselkedés (error AC)")
        if mine and "edge" not in kinds:
            errs.append(f"{s.get('id')}: hiányzik az edge-case (edge AC)")
        for dep in s.get("depends_on") or []:
            if dep not in sid:
                errs.append(f"{s.get('id')}: ismeretlen függőség: {dep}")
    for n in spec.get("nfr") or []:
        v = vague_words(str(n.get("requirement", "")))
        if v:
            errs.append(f"NFR ({n.get('area', '?')}): mérőszám nélküli homályos szó: {', '.join(v)}")
    if not [x for x in spec.get("scope_in") or [] if not _blank(x)]:
        errs.append("üres a 'benne van' lista")
    if not [x for x in spec.get("scope_out") or [] if not _blank(x)]:
        errs.append("üres a 'nincs benne' lista")
    for k, a in enumerate(spec.get("assumptions") or [], 1):
        if a.get("needs_confirmation") is not True:
            errs.append(f"feltevés #{k}: megerősítésre kell jelölni (needs_confirmation: true)")
    if len(spec.get("open_questions") or []) > MAX_QUESTIONS:
        errs.append(f"legfeljebb {MAX_QUESTIONS} nyitott kérdés lehet")
    return errs
