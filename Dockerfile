# One image, three roles (web / watch / sample) — the role is chosen at runtime
# by $ROLE in docker/entrypoint.sh. See DEPLOY.md §2.
#
# Two stages: (1) a node stage builds the React bundle; (2) a python runtime
# stage installs deps, bakes the bge-large embedding model into the image (so a
# task restart never re-downloads ~1.3GB), and copies the app + built bundle.

# ── stage 1: build the frontend ───────────────────────────────────────────────
FROM node:20-slim AS frontend-build

WORKDIR /frontend

# Deps first so `npm ci` caches unless the lockfile changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Then the sources + the .env.production (VITE_API_URL="" → same-origin API).
COPY frontend/ ./
RUN npm run build   # tsc --noEmit && vite build → /frontend/dist


# ── stage 2: python runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# curl is used by the entrypoint's DuckDNS self-update; nothing else is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Bake the model into a fixed cache dir. LocalEmbedder loads it by name via
# sentence-transformers, which reads this HF cache; setting HF_HOME before the
# download makes the build-time fetch land exactly where the runtime loads from.
ENV HF_HOME=/opt/hf-cache \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Deps before source so the (slow) pip + model layers cache across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download BAAI/bge-large-en-v1.5 into $HF_HOME (matches LocalEmbedder).
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-large-en-v1.5')"

# Application code + the SQL (schema/seed) + the built frontend bundle.
COPY src/ ./src/
COPY sample_project/ ./sample_project/
COPY sql/ ./sql/
COPY --from=frontend-build /frontend/dist ./frontend/dist
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN chmod +x ./docker/entrypoint.sh

# Drop root: the public web/sample roles shouldn't serve as UID 0. The model
# cache was written as root at build time, so hand it (and /app) to the app user.
RUN useradd --system --create-home app \
    && chown -R app:app /app /opt/hf-cache
USER app

ENTRYPOINT ["./docker/entrypoint.sh"]
