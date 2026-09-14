
# Céluma 1.3 Fase 2, Bloque E: pinned to Debian 12 "bookworm" (not the
# floating `python:3.12-slim`, which currently resolves to Debian 13
# "trixie") because `playwright install --with-deps` for playwright==1.49.*
# ships an apt package list (ttf-ubuntu-font-family, ttf-unifont, ...) that
# does not exist yet on trixie — build fails with "no installation
# candidate". Revisit this pin together with any future playwright version
# bump.
#
# Two targets:
#   base / runtime  the production API image — runtime dependencies only
#   dev             base + requirements-dev.txt, for local docker compose
#
# `runtime` is the LAST stage, so a plain `docker build` (as CI runs, with no
# --target) produces the production image. `docker-compose.yml` asks for
# `target: dev` so `docker compose exec api pytest` keeps working locally
# without putting pytest in anything deployable.
FROM python:3.14-slim-bookworm AS base

# Install system dependencies
RUN apt-get update && apt-get install -y \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Céluma 1.3 Fase 2, Bloque E: headless Chromium for official PDF generation
# (ReportPdfGenerationService). Uses Playwright's own managed Chromium build
# (not the system apt one) so the version is pinned to what `playwright==1.49.*`
# in requirements.txt expects; `--with-deps` installs the OS-level libraries
# (fonts, libnss3, libatk, etc.) Chromium needs on this slim base image.
# Accepted trade-off, see
# docs/celuma-1.3/phase-2-block-e/phase-2-block-e-architecture-decision.md.
#
# Céluma 1.3 Phase 2 closure: `--only-shell`. From Playwright 1.49, `install
# chromium` lays down TWO browser builds — the full headed Chromium
# (chromium-<rev>, ~542 MB) and the headless shell
# (chromium_headless_shell-<rev>, ~304 MB) — plus an ffmpeg build used only
# for test video recording. `chromium.launch()` defaults to headless=True and
# spawns the headless shell, verified by reading /proc/<pid>/exe while
# _render_pdf()'s exact launch call was running. The headed build was dead
# weight in every image we have shipped (the 3.3 MB ffmpeg build survives
# --only-shell and is left alone). See
# docs/celuma-1.3/phase-2-closure/backend-image-size-analysis.md.
RUN playwright install --with-deps --only-shell chromium

COPY . .

# Provenance: set to the full commit SHA at build time (Block G, D-5).
ARG CELUMA_VERSION=dev
ENV CELUMA_VERSION=${CELUMA_VERSION}

# H-0c: the human-readable release identity (e.g. "v1.3.0"), independent of
# the SHA above. Empty by default so a local build's /health output is
# unchanged. Both are baked into the image, so tagging the validated digest
# `v1.3.0` still requires no rebuild.
ARG CELUMA_RELEASE=
ENV CELUMA_RELEASE=${CELUMA_RELEASE}

# Make start script executable
RUN chmod +x start.sh

EXPOSE 8000

# Use start script instead of direct uvicorn
CMD ["./start.sh"]


# --- dev -------------------------------------------------------------------
# Local development only. Adds pytest/pytest-cov/httpx on top of the runtime
# image so the compose container can run the suite. Never built by CI.
FROM base AS dev
RUN pip install --no-cache-dir -r requirements-dev.txt


# --- runtime ---------------------------------------------------------------
# The production image. Last stage on purpose: `docker build` with no
# --target resolves here, so CI cannot accidentally ship the dev target.
FROM base AS runtime
