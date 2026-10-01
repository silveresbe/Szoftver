"""Redactor telefon-regex mérőkorpusz (HANDOFF 9. hiány). NEM_MASZKOLANDÓ: valós kódban gyakori számsorok;
MASZKOLANDÓ: valódi telefonszám-formák. Használat: python3 -m tools.redactor_corpus"""
NEM_MASZKOLANDO = [
    "created_at = 1727558400123", "TIMEOUT_MS = 30000", "MAX_TOKENS = 1234567", "x = 0.123456789",
    "date = '2026-09-28'", "ts = '2026-09-28 21:18:45'", "version = '1.5.20260928'", "ip = '192.168.100.200'",
    "commit 9f2c4e8a7b1d3f5a6c8e0b2d4f6a8c0e2b4d6f8a", "uuid = '550e8400-e29b-41d4-a716-446655440000'",
    "sha256:3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b", "0x7fffffffffff", "id = 123456789012",
    "ORDER-2026-000123456", "range(1000000, 1999999)", "seed = 20260928", "port = 8080", "rpm: 30, tpm: 12000, rpd: 1000",
    "lines 120-450 of 12345", "size = 1_048_576", "checksum: 4294967295", "build 2026.09.28.1234", "SELECT * FROM t LIMIT 100000",
    "iso = 20260928T211845Z", "pi = 3.14159265358979", "isbn 9789634567890", "zip 2700 Cegléd",
    "np.random.seed(123456789)", "epoch 1727558400", "n = 100 200 300 400",
    "d = '06/30/2026'", "d = '06-30-2026'", "off = +12345678", "pad = '0000123456789'", "v = '+1.5.20260928'",
    "python_requires = '>=3.11'", "rows = 3 000 000", "a = 1 2 3 4 5 6 7 8 9 10", "hex = '0x0123456789abcdef'",
    "price = 1 234 567 Ft", "width: 1024px; height: 768px", "coords = 47.4979, 19.0402",
]
MASZKOLANDO = [
    "+36 30 123 4567", "+36301234567", "06 30 123 4567", "06-30-123-4567", "+36 1 234 5678", "(06) 30 123 4567",
    "+1 (555) 123-4567", "+1 555 123 4567", "555-123-4567", "(555) 123-4567", "+44 20 7946 0958", "+49 30 12345678",
    "06 20/123-4567", "+36 (30) 123-4567", "+36-30-123-4567", "0036 30 123 4567", "hívj: +36 70 987 6543.", "tel:+36201234567",
]
