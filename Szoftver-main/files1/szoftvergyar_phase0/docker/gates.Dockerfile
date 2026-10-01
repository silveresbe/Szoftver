# A determinisztikus kapuk (lint, típus) eszközkészlete. Építés HÁLÓZATTAL, egyszer, a gazdagépen:
#   sh docker/build_gates_image.sh
# Futásidőben a sandboxban NINCS hálózat, ezért az eszközöknek a képben kell lenniük (11.1, 11.5).
FROM python:3.11-slim
RUN pip install --no-cache-dir "ruff==0.6.9" "mypy==1.11.2" \
 && python -m ruff --version && python -m mypy --version
# Futtató felhasználó: a sandbox `nobody`-ként indítja a konténert, itt nincs szükség külön felhasználóra.
