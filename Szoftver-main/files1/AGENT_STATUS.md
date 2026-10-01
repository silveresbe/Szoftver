# Szoftvergyár – ágens- és komponensállapot (2026-09-30, 13. munkamenet után)

Forrás: `docs/spec_v1_5.md` (12.1 fázisos bevezetés, 1.2 komponensek) összevetve a kóddal és a `HANDOFF.md`-vel.
Minden "kész" itt azt jelenti: **mock/hamis sandbox mellett tesztelve (410 teszt zöld)**. Valódi LLM-mel egyik ágens sem futott; valódi Dockerrel csak a sandbox önteszt és a `DockerBackend` futott.

## Röviden: NEM kész az összes ágens
A specifikációban 9 ágens van (2.1–2.9). Kész: **3** (Supervisor, Product Owner, Master Coder). A 1. fázis négy ágenséből (PO, Coder, QA, Git/DevOps) **kettő** van meg.

## Ágensek

| Ágens (spec) | Fázis | Állapot | Megjegyzés |
|---|---|---|---|
| Supervisor / Orchestrator (2.1) | 0 | KÉSZ (kód, nem LLM) | `drive`, `run_pipeline`, `code_task`, `accept_spec/patch`, checkpointok |
| Product Owner (2.6) | 1 | KÉSZ (mock) | valódi modellel nem mérve |
| Master Coder (2.2) | 1 | KÉSZ (mock) | valódi modellel nem mérve; diffet nem ad vissza |
| QA és Reviewer (2.4) | 1 | HIÁNYZIK | csak a determinisztikus kapuk vannak meg, a QA-LLM nincs |
| Git / DevOps (2.5) | 1 | HIÁNYZIK | nincs munkafa/commit/checkout; ez a patch-írás összeomlás elleni mentőhálója is |
| Security Officer (2.3) | 2 | HIÁNYZIK | S3 sáv nélküle `ESCALATE_HUMAN` (szándékos) |
| DB & Schema Architect (2.7) | 2 | HIÁNYZIK | |
| Technical Writer (2.8) | 2 | HIÁNYZIK | |
| UX/UI Designer (2.9) | 3 | HIÁNYZIK (szándékos) | `ui: true` jelzést tárolja a Supervisor |

## 1. fázis – nem-ágens komponensek

| Komponens | Állapot |
|---|---|
| Circuit Breaker, iterációs plafon, haladásfigyelő | KÉSZ (diff-hasonlósági jel nem működik) |
| Ingress Filter (szabályréteg) | KÉSZ |
| Determinisztikus kapuk | RÉSZBEN: `syntax`, `lint` (ruff), `types` (mypy), `unit_tests` bekötve (12. munkamenet, mock; a valódi `fsz-gates:1` képpel még nem futott); SAST nincs |
| Lane Classifier (S1/S2) | HIÁNYZIK (a `lane` csak paraméter/mező) |
| Autofix + puha kapu (11.2) | KÉSZ kódban (13. munkamenet, mock; valódi `ruff`-fal nem futott). Hiányzik: `chore(style)` commit (Git/DevOps), pontos „diffen kívüli sor” |
| Auto-Rollback (4.3) | HIÁNYZIK (önálló megvalósítás nincs) |
| Adósság-nyilvántartás (csak rögzít) | KÉSZ (`factory/debt.py`, `state/tech_debt.jsonl`); a `DEBT_TASK` sorba állítás (10.4) nincs |
| Backup ütemezés | HIÁNYZIK (a `backup.py` megvan, az ütemezés nem) |
| Mérőszámok (lane_distribution, no_progress_events, tokens_per_task) | RÉSZBEN (`metrics.py`) |
| **Kilépési feltétel: 10 valós feladat** | NEM TELJESÜLT |

## 0. fázis váz: kész
Message Bus, Tool Gateway, LLM Gateway + Rate Limiter, Sandbox Runner + önteszt (valódi Dockerrel, `runc`), állapottár, Audit Chain, kill switch, Redactor, mock/replay mód, config validáció.

## 2. és 3. fázis
Még nem indult (Context Retriever, Fix/Review Cache, kapuosztályok, Ingress osztályozó, test-first S3; 3. fázis: Growth Manager, Ops Watch, IaC, helyi modell stb.). A 3. fázis tiltása szándékos.

## Javasolt sorrend
1. ~~Kapuk eszközkészlete~~ KÉSZ kódban (12. munkamenet): kép + `requires` előellenőrzés. TEENDŐ EMBERNEK: `sh docker/build_gates_image.sh`, majd `sandbox-selftest` és egy valódi kapufutás.
2. ~~Autofix + puha kapu~~ KÉSZ kódban (13. munkamenet).
3. Lane Classifier S1/S2.
4. Git/DevOps (munkafa, commit, Auto-Rollback) – determinisztikus rész előbb.
5. QA-LLM.
6. Első valódi LLM-futás (PO + Coder), majd a 10 valós feladat és a mérőszámok.
