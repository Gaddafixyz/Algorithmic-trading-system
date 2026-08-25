FROM python:3.13-slim AS builder

# Build-time only: as of the pinned versions in requirements.txt, every
# dependency ships a prebuilt manylinux wheel for Python 3.13 (verified:
# `pip install --only-binary=:all: -r requirements.txt` succeeds with zero
# source builds), so gcc/libpq-dev are not actually required today. They
# are kept here — in a stage that is discarded and never reaches the
# final image — purely as defense-in-depth in case a future dependency
# bump reintroduces a package without a prebuilt wheel for this platform.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Build an isolated venv so the final stage can copy it wholesale without
# needing pip, setuptools, or any build tooling at all.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# --- Runtime image: no compiler, no dev headers, no pip cache ---
FROM python:3.13-slim AS app

# Security: run as non-root
RUN groupadd --gid 1001 trader && \
    useradd --uid 1001 --gid trader --shell /bin/bash --create-home trader

# Runtime-only shared libraries:
# - libgomp1, libstdc++6: required by xgboost's compiled extension
#   (src/machine_learning/trainer.py imports xgboost.XGBClassifier at
#   model-training time). python:*-slim is a minimal Debian image and
#   does not guarantee these are present as transitive dependencies of
#   python3 alone across all base-image revisions, so they are installed
#   explicitly rather than assumed.
# - curl: used by operators for manual container diagnostics.
# No compiler (gcc) or dev headers (libpq-dev) ship in this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libstdc++6 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the fully-built venv from the builder stage — no pip/setuptools
# install step runs in this stage at all.
#
# IMPORTANT: the venv's own scripts (e.g. bin/pip) have this exact
# absolute path baked into their shebang lines at creation time. It MUST
# be copied to the identical path (/opt/venv) here as it was created at
# in the builder stage above — changing one without the other will break
# every script inside the venv.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY --chown=trader:trader src/ ./src/
COPY --chown=trader:trader scripts/ ./scripts/
COPY --chown=trader:trader configs/ ./configs/
COPY --chown=trader:trader pyproject.toml .

# Create runtime directories
RUN mkdir -p /app/data /app/logs /app/backups /app/models && \
    chown -R trader:trader /app/data /app/logs /app/backups /app/models

USER trader

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

EXPOSE 8080 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python scripts/healthcheck.py || exit 1

CMD ["python", "-m", "src.main"]
