# syntax=docker/dockerfile:1
#
# Agent dispečer – obraz aplikácie (webová aplikácia aj agent používajú ten istý obraz).
#
#   docker build -t vlaky-app:latest .                          # produkčný obraz (cieľ "runtime")
#   docker build --target osm-tools -t vlaky-app-osm:latest .   # + osmium na import siete z OSM
#
# Zásady:
#   - oficiálny python:3.12-slim, žiadne kompilátory (všetky závislosti majú binárne balíky),
#     pip bez cache – obraz je malý a nezostávajú v ňom nástroje na zostavovanie,
#   - aplikácia beží ako neprivilegovaný používateľ "vlaky" (UID/GID 10001),
#   - kód a dáta siete patria root-ovi (aplikácia ich nemôže prepísať); zapisovať sa dá len
#     do data/schranka a data/odoslane (v Docker Compose sú to pomenované zväzky),
#   - tajomstvá (.env) sa do obrazu nikdy nekopírujú (pozri .dockerignore).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Neprivilegovaný používateľ – rovnaké UID/GID vytvorí aj deploy/vps/setup.sh na serveri.
RUN groupadd --system --gid 10001 vlaky \
 && useradd --system --uid 10001 --gid vlaky --home-dir /app --no-create-home \
            --shell /usr/sbin/nologin vlaky

# Závislosti zvlášť od kódu (zmena kódu nevyvolá novú inštaláciu balíkov)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Aplikácia, referenčná implementácia a dáta potrebné za behu
# (data/siet z OpenStreetMap – © prispievatelia OpenStreetMap, licencia ODbL)
COPY vlaky/ ./vlaky/
COPY referencia/ ./referencia/
COPY data/ ./data/

# Predkompilovaný bytecode (za behu sa nezapisuje) a zapisovateľné adresáre agenta
RUN python -m compileall -q vlaky referencia \
 && mkdir -p data/schranka data/odoslane \
 && chown vlaky:vlaky data/schranka data/odoslane \
 && chmod 0750 data/schranka data/odoslane

USER vlaky
EXPOSE 8000

# Stav agenta (návratový kód 0 = OK). Webová služba ho v docker-compose.yml nahrádza
# kontrolou GET /health.
HEALTHCHECK --interval=60s --timeout=30s --start-period=120s --retries=3 \
    CMD ["python", "-m", "vlaky", "health"]

CMD ["python", "-m", "vlaky", "web", "--host", "0.0.0.0", "--port", "8000"]


# --- Nástroj na import siete z OpenStreetMap (osmium z requirements-dev.txt) ---------------
# Spúšťa sa len na požiadanie:  docker compose --profile tools run --rm osm-import
FROM base AS osm-tools
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir "$(grep -E '^osmium' requirements-dev.txt)"
USER vlaky
HEALTHCHECK NONE
CMD ["python", "-m", "vlaky", "import-osm", "--help"]


# --- Produkčný obraz (predvolený cieľ – musí zostať posledný) ------------------------------
FROM base AS runtime
LABEL org.opencontainers.image.title="Agent dispečer (vlaky)" \
      org.opencontainers.image.description="Agent dispečer pre nákladné vlaky – webová aplikácia a agent"
