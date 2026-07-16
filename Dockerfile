# PBC Email Agent — Streamlit UI (spawns the agent runner as a subprocess).
# Deploys to Railway; the app persists to the shared Postgres in $DATABASE_URL
# (tables are prefixed pbc_ so they don't clash with other apps on that DB).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PORT=8501

WORKDIR /app

# procps provides `ps` (used as a fallback when probing runner liveness).
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY requirements-prod.txt .
RUN pip install -r requirements-prod.txt

COPY . .

# data/ holds the run log, benchmark scratch DB, and saved attachments. On
# Railway's ephemeral filesystem these don't survive redeploys — mount a volume
# at /app/data if you need attachment files to persist (tracker state lives in
# Postgres regardless).
RUN mkdir -p data

EXPOSE 8501

# Railway injects $PORT; bind to it (falling back to 8501 for local `docker run`).
CMD ["sh", "-c", "streamlit run ui.py --server.port ${PORT:-8501} --server.address 0.0.0.0"]
