#!/bin/bash

set -e  # Exit on any error

echo "Starting Celuma Backend..."

# If DATABASE_URL is not provided (e.g. on ECS, where the cluster credentials
# arrive split across DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME), build it
# from those parts. Local docker-compose still works because it sets
# DATABASE_URL directly.
if [ -z "${DATABASE_URL}" ]; then
    if [ -n "${DB_HOST}" ] && [ -n "${DB_USER}" ] && [ -n "${DB_PASSWORD}" ]; then
        DB_PORT="${DB_PORT:-5432}"
        DB_NAME="${DB_NAME:-celumadb}"
        export DATABASE_URL="postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
        echo "DATABASE_URL composed from DB_* environment variables"
    else
        echo "ERROR: DATABASE_URL is not set and DB_* variables are incomplete"
        exit 1
    fi
fi

# Function to check database connection
check_db() {
    echo "Checking database connection..."
    python -c "
import os
import sys
from sqlalchemy import create_engine, text

try:
    database_url = os.environ['DATABASE_URL']
    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    engine.dispose()
    print('Database connection successful!')
except Exception as e:
    print(f'Database connection failed: {e}')
    sys.exit(1)
"
}

# Wait for database to be ready
echo "Waiting for database to be ready..."
max_attempts=30
attempt=1

while [ $attempt -le $max_attempts ]; do
    if check_db; then
        echo "Database is ready!"
        break
    else
        echo "Attempt $attempt/$max_attempts: Database not ready, waiting..."
        sleep 2
        attempt=$((attempt + 1))
    fi
done

if [ $attempt -gt $max_attempts ]; then
    echo "ERROR: Database connection failed after $max_attempts attempts"
    exit 1
fi

# Céluma 1.3 Phase 5, Block F §31 — controlled migration deployment.
#
# This block used to run `alembic upgrade head` on **every** container start.
# Under an ECS rolling deploy that means the first new task to boot migrates
# the database while the old tasks are still serving traffic against the old
# schema — and Block B proved the Céluma 1.3 migration is not safe under
# normal rolling traffic. `--force-new-deployment` (what the GitHub Actions
# deploy job issues) is exactly that shape. Worse, with more than one task the
# `alembic current` check is not a lock: two containers can both observe "not
# at head" and both start upgrading.
#
# So migration is no longer a side effect of starting. `RUN_MIGRATIONS_ON_START`
# decides, and the two paths are:
#
#   true (default)  migrate, then serve. Keeps `docker compose up`,
#                   `docker-compose.test.yml` and every developer workflow
#                   working exactly as before.
#
#   false           do not migrate — but **verify** the schema is at head and
#                   refuse to start if it is not.
#
# The verification half is the point. Simply not migrating would let a task
# come up against a schema it does not match, which is a worse failure than
# the one being fixed: silent, and discovered through corrupt behaviour rather
# than a stopped deploy. Refusing to start makes a skipped or failed migration
# loud, and the ECS deployment circuit breaker rolls it back.
#
# The release procedure that goes with this is in
# block-f-controlled-deployment-runbook.md: drain traffic, snapshot, run the
# migration as one controlled task, verify `v1_3_0`, then start the new
# application.
RUN_MIGRATIONS_ON_START="${RUN_MIGRATIONS_ON_START:-true}"

case "$(echo "${RUN_MIGRATIONS_ON_START}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|y|on)
        echo "Checking migration status..."
        if alembic current 2>/dev/null | grep -q "(head)"; then
            echo "Database is already up to date, skipping migrations."
        else
            echo "Running database migrations..."
            if alembic upgrade head; then
                echo "Migrations completed successfully!"
            else
                echo "ERROR: Migrations failed!"
                echo "You can run migrations manually with: alembic upgrade head"
                exit 1
            fi
        fi
        ;;
    *)
        echo "RUN_MIGRATIONS_ON_START is false; this container will not migrate."
        echo "Verifying the database is already at head..."
        if alembic current 2>/dev/null | grep -q "(head)"; then
            echo "Database is at head; starting."
        else
            echo "ERROR: the database is NOT at the expected Alembic head."
            echo "Current: $(alembic current 2>&1 | tr '\n' ' ')"
            echo ""
            echo "This container will not migrate it, and will not serve against"
            echo "a schema it does not match. Run the migration as a controlled,"
            echo "one-off task first — see"
            echo "docs/celuma-1.3/phase-5-block-f/block-f-controlled-deployment-runbook.md"
            exit 1
        fi
        ;;
esac

# Start the application
echo "Starting application..."

# Céluma 1.3 Phase 5, Block F §27 — E-004: per-IP rate-limit identity.
#
# `app/main.py`'s limiter keys on `request.client.host`. Behind
# CloudFront -> ALB -> ECS that is the ALB's private address, identical for
# every visitor, so the whole internet shares one 100-requests/60s bucket.
#
# The fix is NOT a bare `--proxy-headers`. Uvicorn's default trusted host is
# `127.0.0.1`, and widening it to `*` is actively worse than doing nothing:
# `_TrustedHosts.get_trusted_client_host` then returns the **leftmost**
# X-Forwarded-For entry, which is the one a client can write themselves — so
# any caller could pick their own rate-limit bucket, or someone else's.
# Measured directly against uvicorn 0.35 in Block F.
#
# With an explicit allow-list uvicorn instead walks the header from the right
# and returns the first hop that is *not* trusted, ignoring anything a client
# prepended. Uvicorn 0.35 accepts CIDR networks here, not just literal
# addresses, which is what makes this expressible for a VPC at all.
#
# Trusting the VPC CIDR alone yields the CloudFront POP address — spoof-proof,
# and already far better than one global bucket. Also listing the CloudFront
# origin-facing ranges yields the real viewer address. Both were verified
# empirically; see block-f-security-edge-validation.md.
#
# Unset means unchanged behaviour: no proxy headers are trusted at all. That
# is deliberate — a missing value must fail closed (coarse rate limiting),
# never open (client-controlled identity).
if [ -n "${FORWARDED_ALLOW_IPS}" ]; then
    echo "Trusting proxy headers from: ${FORWARDED_ALLOW_IPS}"
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
        --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
else
    echo "FORWARDED_ALLOW_IPS is unset; not trusting any proxy headers"
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
fi
