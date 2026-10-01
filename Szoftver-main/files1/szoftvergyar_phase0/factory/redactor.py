"""Egress Redactor (12.6): kimenő prompt szűrése. Fail-closed: hiba esetén a hívás nem megy ki."""
from __future__ import annotations
import hashlib, re

class RedactorError(Exception):
    pass

class ForbiddenPath(Exception):
    pass

FORBIDDEN_GLOBS = re.compile(r"(^|/)(\.env[^/]*|secrets/|state/|audit/|evals/recordings/)|\.(pem|key|p12)$")

PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    ("conn_string", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@]+@[^\s]+", re.I)),
    ("password", re.compile(r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key)\b\s*[:=]\s*['\"]?([^\s'\"]{4,})['\"]?")),
]
def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)

# Telefonszám: SZERKEZETET követel (előtag és/vagy elválasztók + számjegyszám-ellenőrzés), hogy a kódban gyakori számsorok
# (epoch, seed, verzió, ID, időbélyeg) ne maszkolódjanak. Mérés: tools/redactor_corpus.py, teszt: TestRedactorPhone.
_B = r"(?<![\w.\-/])"                       # előtte nem lehet szó/pont/kötőjel/perjel (nem egy hosszabb szám közepe)
_E = r"(?!\w)(?![\-.]\d)"                     # utána nem folytatódik szám
_PHONE = [
    # nemzetközi + előtaggal: +36 30 123 4567, +36301234567, +1 (555) 123-4567; 9-15 számjegy
    (re.compile(_B + r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?(?:[\s\-/]?\d{1,4}){1,5}" + _E), lambda t: 9 <= _digits(t) <= 15),
    # 00-s nemzetközi előhívó: csak elválasztókkal (a nullákkal kitöltött azonosítók ne találjanak): 0036 30 123 4567
    (re.compile(_B + r"00\d{1,3}[\s\-]\d{1,4}(?:[\s\-/]\d{1,4}){1,5}" + _E), lambda t: 9 <= _digits(t) - 2 <= 15),
    # magyar belföldi 06-os: 06 30 123 4567, 06-30-123-4567, 06301234567, (06) 20/123-4567, 06 1 234 5678
    (re.compile(_B + r"\(?06\)?[\s\-]?\d{1,2}[\s\-/]?\d{3}[\s\-]?\d{3,4}" + _E), lambda t: 10 <= _digits(t) <= 11),
    # amerikai: (555) 123-4567 és 555-123-4567 / 555 123 4567 (elválasztó kötelező)
    (re.compile(_B + r"\(\d{3}\)[\s\-]?\d{3}[\s\-]\d{4}" + _E), lambda t: _digits(t) == 10),
    (re.compile(_B + r"\d{3}[\s\-]\d{3}[\s\-]\d{4}" + _E), lambda t: _digits(t) == 10),
]
PII = {
    "email": [(re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), None)],
    "phone": _PHONE,
}

def _tag(s: str) -> str:
    return "[TITOK:" + hashlib.sha256(s.encode()).hexdigest()[:8] + "]"

class Redactor:
    def __init__(self, cfg: dict | None = None):
        c = (cfg or {}).get("egress_redaction", {})
        self.pii = c.get("pii_patterns", ["email", "phone"])
        self._broken = False  # tesztelhető hibainjektáláshoz

    def check_paths(self, paths):
        for p in paths:
            if FORBIDDEN_GLOBS.search(p.replace("\\", "/")):
                raise ForbiddenPath(p)

    def redact(self, text: str) -> tuple[str, int]:
        """Visszaadja (maszkolt szöveg, találatok száma). Bármilyen hiba -> RedactorError (fail-closed)."""
        try:
            if self._broken:
                raise RuntimeError("injektált hiba")
            n = 0
            for name, rx in PATTERNS:
                def sub(m, name=name):
                    nonlocal n
                    n += 1
                    if name == "password":
                        return m.group(0).replace(m.group(2), _tag(m.group(2)))
                    return _tag(m.group(0))
                text = rx.sub(sub, text)
            for name in self.pii:
                for rx, valid in PII[name]:
                    def psub(m, valid=valid):
                        nonlocal n
                        if valid and not valid(m.group(0)):
                            return m.group(0)             # szerkezetre hasonlít, de nem telefonszám: érintetlenül hagyjuk
                        n += 1
                        return _tag(m.group(0))
                    text = rx.sub(psub, text)
            return text, n
        except Exception as e:  # noqa
            raise RedactorError(str(e)) from e
