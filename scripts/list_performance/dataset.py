#!/usr/bin/env python3
"""Deterministic synthetic tenants for the list-performance benchmark.

TEST TOOLING ONLY (Céluma 1.4, issues #5/#6/#7). Nothing under
`scripts/list_performance/` is imported from `app/`, and nothing here runs at
application startup.

Builds several tenants of different sizes **in one database**, because the
reported symptom is that a large laboratory is slow while small laboratories
in the same database are not. Every name and code comes from a seeded PRNG over
fixed nonsense-syllable tables plus a short list of deliberately constructed
search probes (identifier neighbours, accented surnames). There is no PHI and
no real person behind any row.

The target database must be a disposable one whose name starts with
`celuma_listperf`; the script refuses anything else, and refuses a database
that already contains tenants unless `--reset` is given (which drops and
recreates it from the real Alembic chain).

Usage (inside the API container, so `db` resolves):

    python scripts/list_performance/dataset.py --database celuma_listperf --reset

The JSON written to stdout records the seed, profile shapes and the ids/tokens
the benchmark needs. Tokens are local JWTs for synthetic users only.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import uuid
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine.url import make_url  # noqa: E402

DB_PREFIX = "celuma_listperf"
DEFAULT_SEED = 20261014
EPOCH = datetime(2025, 1, 6, 8, 0, 0)

_SYL_A = ("va", "lo", "mi", "ren", "tas", "cor", "bel", "nur", "pel", "sid", "qua", "ro")
_SYL_B = ("dra", "ton", "quir", "mel", "vas", "ler", "nis", "cad", "rop", "tem", "lin", "sor")

#: Accent/ñ probes for the human-text matching contract (#7). They are
#: syllable compounds, not a list of real names.
ACCENT_SURNAMES = ("Muñóz", "Núñez", "Íñiguez", "Peña", "Ortíz")

#: Identifier probes for #7. CU276262's neighbours differ by one or two
#: digits, which the pre-fix fuzzy matcher accepted; CU999999 is a far code.
#: BX/IHQ share the digit run across prefixes. CU-011026 is the COD-01-style
#: shape (monthly sequence + MMYY) that must stay searchable.
SAMPLE_CODE_PROBES = (
    "CU276262", "CU276263", "CU276261", "CU276252", "CU276362", "CU276226",
    "CU999999", "BX276262", "IHQ276262", "CU-011026", "CU-021026", "CU-1001026",
)
ORDER_CODE_PROBES = ("CU-011026", "CU-021026", "CU-991026", "CU-1001026", "CU-10001026")

STUDY_TYPES = (("CU", "Citología urinaria"), ("BX", "Biopsia"), ("IHQ", "Inmunohistoquímica"))
SAMPLE_TYPES = ("SANGRE", "BIOPSIA", "LAMINILLA", "TEJIDO", "OTRO")
#: Share of samples left without a catalog type, like the legacy rows of a
#: laboratory upgraded to 1.4 ("Sin tipo").
UNTYPED_SHARE = 0.1


def _sample_type_for(code: str, chosen: str, ids: dict):
    """Deterministic by code (no PRNG draw): ~UNTYPED_SHARE of samples are legacy."""
    return None if (zlib.crc32(code.encode()) % 100) < UNTYPED_SHARE * 100 else ids[chosen]
SAMPLE_STATES = ("RECEIVED", "PROCESSING", "READY", "DAMAGED", "CANCELLED")
ORDER_STATUSES = ("RECEIVED", "PROCESSING", "DIAGNOSIS", "REVIEW", "RELEASED", "CLOSED", "CANCELLED")
REPORT_STATUSES = ("DRAFT", "IN_REVIEW", "APPROVED", "PUBLISHED", "RETRACTED")
LABEL_NAMES = ("Urgente", "Revisión", "Segunda opinión", "Docencia", "Investigación", "Pendiente pago",
               "Control", "Archivo")


@dataclass(frozen=True)
class Profile:
    name: str
    orders: int
    patients: int
    physicians: int
    users: int
    #: First number of this tenant's sequential CU sample codes.
    sample_base: int


PROFILES = {
    "small": Profile("small", orders=120, patients=90, physicians=12, users=4, sample_base=500000),
    "medium": Profile("medium", orders=1200, patients=900, physicians=60, users=8, sample_base=120000),
    "large": Profile("large", orders=6000, patients=4500, physicians=150, users=15, sample_base=270000),
}

#: Shape of each `--filler` tenant: other laboratories sharing the database,
#: never benchmarked themselves. They only grow the shared tables, which is
#: what a tenant-unindexed scan pays for.
FILLER = Profile("filler", orders=6000, patients=4500, physicians=150, users=15, sample_base=600000)


def _guard(url) -> None:
    if not (url.database or "").startswith(DB_PREFIX):
        raise SystemExit(f"refusing: database {url.database!r} does not start with {DB_PREFIX!r}")
    if url.host not in {"db", "localhost", "127.0.0.1"}:
        raise SystemExit(f"refusing: host {url.host!r} is not a local development database")


def _reset(url) -> None:
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)},
    )


class Generator:
    def __init__(self, conn, seed: int) -> None:
        self.conn = conn
        self.rng = random.Random(seed)

    def _name(self) -> tuple[str, str]:
        r = self.rng
        first = (r.choice(_SYL_A) + r.choice(_SYL_B)).capitalize()
        if r.random() < 0.06:
            last = r.choice(ACCENT_SURNAMES)
        else:
            last = (r.choice(_SYL_B) + r.choice(_SYL_A) + r.choice(_SYL_B)).capitalize()
        return first, last

    def _insert(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        quoted = ", ".join(f'"{c}"' for c in cols)
        stmt = text(f'INSERT INTO {table} ({quoted}) VALUES ({", ".join(":" + c for c in cols)})')
        for i in range(0, len(rows), 2000):
            self.conn.execute(stmt, rows[i:i + 2000])

    def build_tenant(self, profile: Profile) -> dict:
        r = self.rng
        tenant_id = uuid.uuid4()
        branch_id = uuid.uuid4()
        self._insert("tenant", [{"id": tenant_id, "name": f"Listperf {profile.name}", "created_at": EPOCH,
                                 "is_active": True, "reports_v2_enabled": True}])
        self._insert("branch", [{"id": branch_id, "tenant_id": tenant_id, "code": "MAIN",
                                 "name": f"Sucursal {profile.name}", "timezone": "America/Mexico_City",
                                 "country": "MX", "is_active": True, "created_at": EPOCH}])

        users = []
        for i in range(profile.users):
            first, last = self._name()
            users.append({
                "id": uuid.uuid4(), "tenant_id": tenant_id, "email": f"user{i}.{profile.name}@listperf.invalid",
                "full_name": f"{first} {last}", "first_name": first, "last_name": last,
                "username": f"{profile.name}{i}", "hashed_password": "!synthetic-no-login",
                "is_active": True, "created_at": EPOCH,
            })
        self._insert("app_user", users)
        self._insert("user_branch", [{"user_id": u["id"], "branch_id": branch_id} for u in users])
        role_ids = dict(self.conn.execute(text("SELECT code, id FROM role WHERE tenant_id IS NULL")).all())
        roles = [{"id": uuid.uuid4(), "user_id": users[0]["id"], "role_id": role_ids["superuser"], "created_at": EPOCH}]
        for u in users[1:]:
            code = r.choice(("pathologist", "lab_tech", "reviewer"))
            roles.append({"id": uuid.uuid4(), "user_id": u["id"], "role_id": role_ids[code], "created_at": EPOCH})
        self._insert("user_role", roles)

        study = {code: uuid.uuid4() for code, _ in STUDY_TYPES}
        self._insert("study_type", [{"id": study[c], "tenant_id": tenant_id, "code": c, "name": n,
                                     "is_active": True, "created_at": EPOCH} for c, n in STUDY_TYPES])

        sample_type_ids = {c: uuid.uuid4() for c in SAMPLE_TYPES}
        self._insert("sample_type", [{"id": sample_type_ids[c], "tenant_id": tenant_id, "code": c,
                                      "name": c.title(), "is_active": True, "created_at": EPOCH}
                                     for c in SAMPLE_TYPES])

        labels = [{"id": uuid.uuid4(), "tenant_id": tenant_id, "name": n, "color": "#49B6AD",
                   "created_at": EPOCH, "updated_at": EPOCH} for n in LABEL_NAMES]
        self._insert("label", labels)

        patients = []
        for i in range(profile.patients):
            first, last = self._name()
            patients.append({
                "id": uuid.uuid4(), "tenant_id": tenant_id, "branch_id": branch_id, "patient_code": f"P-{i + 1}",
                "first_name": first, "last_name": last, "full_name": f"{first} {last}",
                "sex": r.choice(("F", "M")), "created_at": EPOCH + timedelta(minutes=i),
            })
        self._insert("patient", patients)

        physicians = []
        for i in range(profile.physicians):
            first, last = self._name()
            physicians.append({
                "id": uuid.uuid4(), "tenant_id": tenant_id, "branch_id": branch_id, "physician_code": f"MS-{i + 1}",
                "first_name": first, "last_name": last, "full_name": f"Dr. {first} {last}",
                "specialty": r.choice(("Urología", "Ginecología", "Dermatología", "Oncología")),
                "institution": f"Clínica {r.choice(_SYL_A).capitalize()}{r.choice(_SYL_B)}",
                "is_active": r.random() > 0.1, "created_at": EPOCH + timedelta(minutes=i),
            })
        self._insert("requesting_physician", physicians)

        orders, samples, order_labels, sample_labels, assignments = [], [], [], [], []
        reports, versions, reviews = [], [], []
        seq = {c: 0 for c, _ in STUDY_TYPES}
        cu_next = profile.sample_base
        other_next = {"BX": profile.sample_base, "IHQ": profile.sample_base}
        sample_codes: set[str] = set()
        order_probe_iter = iter(ORDER_CODE_PROBES)
        for i in range(profile.orders):
            st_code = r.choices(("CU", "BX", "IHQ"), weights=(70, 20, 10))[0]
            seq[st_code] += 1
            order_code = f"{st_code}-{seq[st_code]}"
            if i % max(1, profile.orders // len(ORDER_CODE_PROBES)) == 7:
                order_code = next(order_probe_iter, order_code)
            physician = r.choice(physicians) if r.random() < 0.7 else None
            patient = r.choice(patients) if r.random() < 0.95 else None
            created = EPOCH + timedelta(minutes=37 * i)
            order_id = uuid.uuid4()
            order = {
                "id": order_id, "tenant_id": tenant_id, "branch_id": branch_id,
                "patient_id": patient["id"] if patient else None,
                "requesting_physician_id": physician["id"] if physician else None,
                "requested_by": None if physician else (f"Dr. {self._name()[1]}" if r.random() < 0.5 else None),
                "order_code": order_code, "status": r.choice(ORDER_STATUSES), "billed_lock": False,
                "notes": None, "study_type_id": study[st_code], "created_by": users[0]["id"], "created_at": created,
            }
            orders.append(order)
            for _ in range(r.choices((1, 2, 3), weights=(35, 40, 25))[0]):
                if st_code == "CU":
                    code = f"CU{cu_next}"
                    cu_next += 1
                else:
                    code = f"{st_code}{other_next[st_code]}"
                    other_next[st_code] += 1
                sample_codes.add(code)
                sid = uuid.uuid4()
                # Same single PRNG draw the enum column used, so every other
                # value of a seeded dataset is unchanged.
                chosen_type = r.choice(SAMPLE_TYPES)
                samples.append({
                    "id": sid, "tenant_id": tenant_id, "branch_id": branch_id, "order_id": order_id,
                    "sample_code": code, "state": r.choice(SAMPLE_STATES),
                    "received_at": created,
                    "sample_type_id": _sample_type_for(code, chosen_type, sample_type_ids),
                })
                if r.random() < 0.15:
                    sample_labels.append({"sample_id": sid, "label_id": r.choice(labels)["id"]})
                if r.random() < 0.3:
                    assignments.append(self._assignment(tenant_id, "sample", sid, r.choice(users), created))
            for label in r.sample(labels, k=r.choices((0, 1, 2), weights=(70, 20, 10))[0]):
                order_labels.append({"order_id": order_id, "label_id": label["id"]})
            for user in r.sample(users, k=r.choices((0, 1, 2), weights=(55, 35, 10))[0]):
                assignments.append(self._assignment(tenant_id, "lab_order", order_id, user, created))
            if r.random() < 0.08:
                gone = self._assignment(tenant_id, "lab_order", order_id, r.choice(users), created)
                gone["unassigned_at"] = created + timedelta(days=1)
                assignments.append(gone)
            if r.random() < 0.6:
                report_id = uuid.uuid4()
                status = r.choice(REPORT_STATUSES)
                reports.append({
                    "id": report_id, "tenant_id": tenant_id, "branch_id": branch_id, "order_id": order_id,
                    "status": status, "title": f"Reporte {order_code}", "created_by": users[0]["id"],
                    "created_at": created + timedelta(hours=4),
                    "published_at": created + timedelta(days=2) if status == "PUBLISHED" else None,
                })
                order["report_id"] = report_id
                n_versions = r.choices((1, 2, 3), weights=(60, 30, 10))[0]
                for v in range(1, n_versions + 1):
                    versions.append({
                        "id": uuid.uuid4(), "report_id": report_id, "version_no": v, "is_current": v == n_versions,
                        "authored_by": users[0]["id"], "authored_at": created + timedelta(hours=4 + v),
                        "created_at": created + timedelta(hours=4 + v),
                    })
                for reviewer in r.sample(users[1:], k=min(len(users) - 1, r.choices((0, 1, 2), weights=(50, 40, 10))[0])):
                    reviews.append({
                        "id": uuid.uuid4(), "tenant_id": tenant_id, "order_id": order_id, "report_id": report_id,
                        "reviewer_user_id": reviewer["id"], "status": r.choice(("PENDING", "APPROVED", "REJECTED")),
                        "assigned_at": created + timedelta(hours=6),
                    })

        # Identifier probes live on the first order so every tenant can be
        # searched with the same queries; skip a probe this tenant already
        # generated naturally (large tenant's CU sequence covers 276262).
        for code in SAMPLE_CODE_PROBES:
            if code in sample_codes:
                continue
            samples.append({
                "id": uuid.uuid4(), "tenant_id": tenant_id, "branch_id": branch_id, "order_id": orders[0]["id"],
                "sample_code": code, "state": "RECEIVED", "received_at": EPOCH,
                "sample_type_id": sample_type_ids["BIOPSIA"],
            })

        report_ids = {o["id"]: o.pop("report_id", None) for o in orders}
        self._insert('"order"', orders)
        self._insert("sample", samples)
        self._insert("report", reports)
        for order_id, report_id in report_ids.items():
            if report_id:
                self.conn.execute(text('UPDATE "order" SET report_id = :r WHERE id = :o'), {"r": report_id, "o": order_id})
        self._insert("report_version", versions)
        self._insert("report_review", reviews)
        self._insert("order_labels", order_labels)
        self._insert("sample_labels", sample_labels)
        self._insert("assignment", assignments)
        return {
            "profile": asdict(profile), "tenant_id": str(tenant_id), "admin_user_id": str(users[0]["id"]),
            "counts": {"orders": len(orders), "samples": len(samples), "patients": len(patients),
                       "physicians": len(physicians), "reports": len(reports), "report_versions": len(versions),
                       "report_reviews": len(reviews), "assignments": len(assignments),
                       "order_labels": len(order_labels), "sample_labels": len(sample_labels)},
        }

    def _assignment(self, tenant_id, item_type, item_id, user, at) -> dict:
        return {"id": uuid.uuid4(), "tenant_id": tenant_id, "item_type": item_type, "item_id": item_id,
                "assignee_user_id": user["id"], "assigned_at": at, "unassigned_at": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=DB_PREFIX)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--profiles", default="small,medium,large")
    parser.add_argument("--reset", action="store_true", help="drop, recreate and migrate the database first")
    parser.add_argument("--filler", type=int, default=0, help="extra filler tenants sharing the tables")
    args = parser.parse_args()

    url = make_url(os.environ["DATABASE_URL"]).set(database=args.database)
    _guard(url)
    if args.reset:
        _reset(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        if conn.execute(text("SELECT count(*) FROM tenant")).scalar():
            raise SystemExit("refusing: database already holds tenants (use --reset)")
        gen = Generator(conn, args.seed)
        tenants = [gen.build_tenant(PROFILES[name]) for name in args.profiles.split(",")]
        for i in range(args.filler):
            tenants.append(gen.build_tenant(Profile(f"filler{i + 1}", **{
                k: v for k, v in asdict(FILLER).items() if k != "name"})))
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("VACUUM ANALYZE"))
        totals = {t: conn.execute(text(f'SELECT count(*) FROM {t}')).scalar()
                  for t in ('"order"', "sample", "patient", "report", "report_version", "assignment")}
    engine.dispose()
    json.dump({"database": args.database, "seed": args.seed, "tenants": tenants, "table_totals": totals},
              sys.stdout, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
