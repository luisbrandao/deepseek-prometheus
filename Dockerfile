FROM python:3.11-slim

# Default to São Paulo; override with the TZ env var at runtime. tzdata is
# required for the log formatter to emit a correct local-time offset — the
# slim image ships without the zoneinfo database.
ENV TZ=America/Sao_Paulo

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Build identity, injected by CI (see .github/workflows/docker-build.yml) and
# read by app/version.py. Declared HERE, after the dependency install, on
# purpose: APP_VERSION changes on every single build, and an ARG placed above
# `pip install` would invalidate that layer every time and turn a 20-second
# build into a full reinstall.
#
# An undefined ARG expands to the empty string rather than being absent, which is
# why version.py treats "" as "not provided" and reports "dev".
ARG APP_VERSION=""
ARG APP_REVISION=""
ENV APP_VERSION=${APP_VERSION} \
    APP_REVISION=${APP_REVISION}

ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
