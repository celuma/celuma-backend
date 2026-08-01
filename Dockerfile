
# Céluma 1.3 Fase 2, Bloque E: pinned to Debian 12 "bookworm" (not the
# floating `python:3.12-slim`, which currently resolves to Debian 13
# "trixie") because `playwright install --with-deps` for playwright==1.49.*
# ships an apt package list (ttf-ubuntu-font-family, ttf-unifont, ...) that
# does not exist yet on trixie — build fails with "no installation
# candidate". Revisit this pin together with any future playwright version
# bump.
FROM python:3.12-slim-bookworm

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
# Grows the image substantially — accepted trade-off, see
# docs/celuma-1.3/phase-2-block-e/phase-2-block-e-architecture-decision.md.
RUN playwright install --with-deps chromium

COPY . .

ARG CELUMA_VERSION=dev
ENV CELUMA_VERSION=${CELUMA_VERSION}

# Make start script executable
RUN chmod +x start.sh

EXPOSE 8000

# Use start script instead of direct uvicorn
CMD ["./start.sh"]
