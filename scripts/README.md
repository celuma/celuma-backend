# Verification Scripts

This directory contains scripts to verify system functionality.

## verify_billing_flow.sh

Script to verify the full billing flow: order → automatic invoice with catalog price → payment → balance.

### Requirements

- Backend running (Docker or local)
- Valid authentication token
- `jq` installed (for JSON processing)
- `bc` installed (for decimal math)

### Usage

```bash
# With the default token (embedded in the script)
./verify_billing_flow.sh

# With a custom token
./verify_billing_flow.sh "Bearer YOUR_TOKEN_HERE"

# With a custom URL
BASE_URL=http://localhost:8000 ./verify_billing_flow.sh "Bearer YOUR_TOKEN"
```

### What does the script do?

1. **Fetches active study types** and picks one for the test
2. **Checks catalog prices** for the selected study type
3. **Fetches patients** and picks one for the test
4. **Creates an order** without specifying `order_code` (auto-generated, e.g. IHQ-1)
5. **Verifies the auto-generated invoice** has the correct catalog price
6. **Records a partial payment** and checks the balance
7. **Completes the remaining payment**
8. **Verifies final state**: balance 0, invoice PAID, order unlocked

### Sample output

```
=====================================
Verificación de flujo de facturación
=====================================

1. Obteniendo tipos de estudio...
Inmunohistoquímica (IHQ) - ID: abc-123
Biopsia (BIOPSIA) - ID: def-456
✓ Usando tipo de estudio: IHQ (ID: abc-123)

2. Verificando catálogo de precios activos...
✓ Precio encontrado: $1500.00 MXN

3. Obteniendo pacientes...
✓ Usando paciente: Juan Pérez (ID: patient-123)

4. Creando orden con tipo de estudio IHQ...
✓ Orden creada: IHQ-1 (ID: order-123)

5. Verificando factura auto-generada...
✓ Factura generada: INV-IHQ-1
  Total: $1500.00 MXN
  Items:
    - Inmunohistoquímica: $1500.00 x 1 = $1500.00
✓ El precio de la factura coincide con el catálogo

6. Registrando pago parcial de $750.00 MXN...
✓ Pago registrado (ID: pay-123)

7. Verificando balance de la orden...
✓ Balance pendiente: $750.00 MXN

8. Completando pago con $750.00 MXN...
✓ Pago final registrado (ID: pay-124)

9. Verificando estado final...
Estado final:
  Balance: $0.00 MXN
  Orden bloqueada: false
  Estado factura: PAID
✓✓✓ Factura pagada completamente
✓✓✓ Orden desbloqueada (acceso a reporte permitido)

=====================================
Verificación completada
=====================================

Resumen:
  - Orden: IHQ-1
  - Factura: INV-IHQ-1
  - Total: $1500.00 MXN
  - Pagado: $1500.00 MXN
  - Balance: $0.00 MXN
```

### Notes

- The script creates test data (order, samples, payments) in the system
- Useful to verify the full flow still works after changes
- Can be run at any time against a working system
- Created data remains in the database (not deleted automatically)

## capture_schema_snapshot.py

Captures a normalized, comparable snapshot of a PostgreSQL schema as JSON —
tables, columns (with ordinal position, type, nullability, default),
constraints (`pg_get_constraintdef`) and indexes (`pg_indexes.indexdef`).
Deliberately excludes OIDs, sizes, statistics and `alembic_version`.

Written for the Céluma 1.3 pre-Phase-5 migration squash, whose central claim
is that the squashed `v1_3_0` produces exactly the schema the pre-squash
`v1_10_0 … v1_13_0` chain produced. It is read-only: no DDL, no DML.

### Usage

```bash
# Snapshot an ephemeral database to a file
docker compose exec api python scripts/capture_schema_snapshot.py \
  --database celuma_migration_test \
  --output tests/fixtures/schema/some_snapshot.json

# Or print to stdout
docker compose exec api python scripts/capture_schema_snapshot.py --database celumadb
```

Host and credentials come from `DATABASE_URL`; only the database *name* is
taken from `--database`.

### Who uses it

- `tests/test_alembic_migrations.py::TestSchemaEquivalence` imports `capture()`
  directly, so the committed fixture and the comparison can never diverge in
  how they normalize.
- `docs/celuma-1.3/pre-phase-5-migration-squash/migration-local-database-transition-guide.md`
  uses it as the precondition check before stamping a local database.

> The committed fixture `tests/fixtures/schema/v1_13_0_pre_squash_schema.json`
> is the frozen evidence for the migration squash. Regenerating it is not a
> routine action.
