# 13. munkamenet – változások (autofix, puha kapu, adósság-nyilvántartás)

Új fájlok
- factory/autofix.py – Autofixer: ruff format + ruff check --fix (csak safe) a sandboxban, ellenőrzött, atomi visszaírás
- factory/debt.py – DebtRegister (state/tech_debt.jsonl, csak rögzít, duplikátumszűrés, max_open_items)
- tests/test_autofix.py – 59 új teszt

Módosított fájlok
- factory/gates.py – kind: soft kapu, parse_findings, result["soft"]
- factory/supervisor.py – drive/run_pipeline(autofix=, debt=), _soft_gate, GREEN_WITH_DEBT, created halmozás
- factory/master_coder.py – PATCH_APPLIED eredmény: "created" lista
- factory/config.py – soft kapu, autofix, soft_gate validálás (nem gyengíthető)
- factory/tool_gateway.py – "autofix" identitás (repo_read, repo_write)
- factory/sandbox.py – kimeneti plafon 20 000 -> 1 000 000 karakter
- factory.yaml – lint (kemény F,E9,B,S,PLE), lint_soft (E,W,C90,N,SIM,UP,I), autofix, soft_gate
- tests/test_gates.py – a yaml kapusorrend-teszt frissítve
- HANDOFF.md (13. munkamenet), AGENT_STATUS.md

Eredmény: 410 teszt zöld; mutációs ellenőrzés 28 mutáns, mind bukik.

Eltérések a spec szó szerinti listájától (indoklás a HANDOFF-ban)
- D (docstring) nincs a puha csoportban; S101 ki van véve a kemény S-ből.

Teendő embernek
1. sh docker/build_gates_image.sh
2. python3 -m factory.cli sandbox-selftest
3. egy valódi kapufutás autofixszel és puha kapuval (ruff-kapcsolók ellenőrzése)
