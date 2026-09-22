# Agent instructions — celuma-backend

## Scope

This repository owns the Céluma API, persistence, migrations, authorization, audit events, notifications, usage accounting, and report/PDF services. Read the relevant contract in `celuma-engineering` before changing clinical or release behavior.

## Required validation

- Install development dependencies with `make install-dev` when needed.
- Run `make lint`.
- Run `make test-unit` for application changes; use narrower focused tests while iterating, then the required suite before review.
- Review the Alembic chain and migration behavior for every schema change.

## Safety boundaries

1. Preserve tenant isolation on every read and write path.
2. Clinical approval requires role, capability, assignment, lifecycle state, and tenant scope. Administrator status is not clinical approval authority.
3. Do not include PHI, secrets, credentials, production dumps, or real clinical reports in prompts, fixtures, logs, or documentation.
4. Do not weaken authorization, audit, signed-report immutability, migration guards, or tests to make a change pass.
5. Do not commit, push, merge, tag, deploy, migrate a shared database, enable email, or run destructive recovery unless Rafael explicitly requests that exact action.
6. Report the commands run, their results, and any untested boundary.
