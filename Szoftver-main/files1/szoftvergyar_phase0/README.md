# Szoftvergyár – 0. fázis

Többágenses fejlesztőrendszer váza a `docs/spec_v1_5.md` alapján. **Először a `HANDOFF.md`-t olvasd.**

    python3 -m unittest discover -s tests -t .
    python3 -m factory.cli check

Követelmény: Python 3.10+, `pyyaml`. Élő módhoz `GROQ_API_KEY` és `egress_redaction.data_egress_acknowledged: true`.
