"""Deterministic synthetic Céluma 1.2 dataset generator (Phase 5 Block B).

TEST TOOLING ONLY. Never imported from `app/`. Never contacts AWS. Never
creates an S3 object. Contains no PHI — every name, code and address below is
generated from a seeded PRNG over fixed syllable tables.

What this builds and why
------------------------
A database at exactly `v1_2_0`, populated with the shapes the frozen `v1_3_0`
migration's section 15 actually reads:

  * the four historical attribution categories the backfill covers
    (sample images, renditions, report JSON, legacy/manual PDFs, live
    signatures);
  * tenant logos, which the backfill deliberately does NOT cover -- included
    to quantify the known pre-1.3 gap;
  * negative controls the backfill must leave alone (user avatars, which
    belong to no billable category).

Fidelity rules this generator obeys, because getting them wrong would make
every downstream number meaningless:

  * `storage_object` has no `tenant_id` column at `v1_2_0`. Nothing here
    pretends otherwise -- attribution is what the migration is being tested
    to perform.
  * `sha256_hex` is NULL on every row. Verified against the Céluma 1.2 source
    (commit 7d765aa): only `app/services/report_pdf_generation.py`, a 1.3
    service, ever writes it. A synthetic dataset that populated it would
    invent an "official PDF" category that cannot exist on a real 1.2
    database, and would silently change the usage baseline.
  * Object keys use the Céluma 1.2 layouts, not the 1.3 ones. This matters
    most for tenant logos: 1.2 wrote `tenants/{id}/logo.{ext}` while the
    backfill matches `tenants/%/logo/%`. See `TENANT_LOGO_KEY_12`.

Determinism
-----------
Every UUID is `uuid5` of a seed-derived namespace, and every random choice
comes from a single seeded `random.Random`. The same seed and profile produce
byte-identical row contents, including primary keys -- which is what makes the
repeatability comparison in §35 of the brief meaningful.
"""
from __future__ import annotations

import io
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Iterator

import psycopg2.extras

from profiles import Profile

# `COPY` receives everything as text, but the `UPDATE ... WHERE id = %s`
# statements that close the two FK cycles bind UUID objects directly.
psycopg2.extras.register_uuid()

# --------------------------------------------------------------------------
# Céluma 1.2 object-key layouts, copied from the 1.2 source (commit 7d765aa).
#
# TENANT_LOGO_KEY_12 is the load-bearing one. Céluma 1.2's
# `app/api/v1/tenants.py` wrote `tenants/{tenant_id}/logo.{ext}` -- a file
# named `logo`, not a directory. Céluma 1.3 writes
# `tenants/{tenant_id}/logo/{hex}.{ext}` via ManagedTenantImageService, and
# the v1_3_0 backfill matches `object_key LIKE 'tenants/%/logo/%'`, which the
# 1.2 layout cannot satisfy. Generating the 1.3 layout here would manufacture
# a resolution success that no real upgrade can produce.
# --------------------------------------------------------------------------
TENANT_LOGO_KEY_12 = "tenants/{tenant_id}/logo.{ext}"
SAMPLE_PROCESSED_KEY = "samples/{tenant_id}/{branch_id}/{sample_id}/processed/{name}_{uid}.jpg"
SAMPLE_THUMBNAIL_KEY = "samples/{tenant_id}/{branch_id}/{sample_id}/thumbnails/{name}_{uid}.jpg"
SAMPLE_RAW_KEY = "samples/{tenant_id}/{branch_id}/{sample_id}/raw/{name}_{uid}.tif"
REPORT_JSON_KEY = "reports/{tenant_id}/{branch_id}/{report_id}/versions/{n}/report.json"
REPORT_PDF_KEY = "reports/{tenant_id}/{branch_id}/{report_id}/versions/{n}/report.pdf"
SIGNATURE_KEY = "users/{tenant_id}/{user_id}/signature/sign_{ts}.png"
AVATAR_KEY = "avatars/{user_id}/avatar.jpg"

#: The public base URL a 1.2 deployment stored in `tenant.logo_url`.
MEDIA_BASE_URL = "https://media.example.invalid"

ORDER_STATUSES = ("RECEIVED", "PROCESSING", "DIAGNOSIS", "REVIEW", "RELEASED", "CLOSED")
SAMPLE_TYPES = ("SANGRE", "BIOPSIA", "LAMINILLA", "TEJIDO", "OTRO")
SAMPLE_STATES = ("RECEIVED", "PROCESSING", "READY")
REPORT_STATUSES = ("DRAFT", "IN_REVIEW", "APPROVED", "PUBLISHED")

#: Synthetic name fragments. Deliberately nonsense syllables, not a name list.
_SYL_A = ("va", "lo", "mi", "ren", "tas", "cor", "bel", "nur", "pel", "sid")
_SYL_B = ("dra", "ton", "quir", "mel", "vas", "ler", "nis", "cad", "rop", "tem")

EPOCH = datetime(2024, 1, 1, 0, 0, 0)


class _RowSource(io.RawIOBase):
    """Adapts an iterator of TSV lines into the file object `COPY` wants.

    Materialising 650k rows as one string costs hundreds of MB; this streams
    them instead, which is what keeps LARGE generation inside a normal
    container memory budget.
    """

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = iter(lines)
        self._buf = ""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        while size < 0 or len(self._buf) < size:
            try:
                self._buf += next(self._lines)
            except StopIteration:
                break
        if size < 0:
            chunk, self._buf = self._buf, ""
        else:
            chunk, self._buf = self._buf[:size], self._buf[size:]
        return chunk.encode("utf-8")


def _esc(value) -> str:
    r"""TSV-escape one value for `COPY ... FROM STDIN`.

    `\N` is COPY's NULL marker; tabs, newlines and backslashes inside text
    must be escaped or the row count silently shifts.
    """
    if value is None:
        return r"\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _copy(conn, table: str, columns: list[str], rows: Iterator[tuple]) -> int:
    """Bulk-load `rows` into `table`, returning the number of rows written."""
    count = 0

    def lines() -> Iterator[str]:
        nonlocal count
        for row in rows:
            count += 1
            yield "\t".join(_esc(v) for v in row) + "\n"

    with conn.cursor() as cur:
        cur.copy_expert(
            f'COPY {table} ({", ".join(columns)}) FROM STDIN WITH (FORMAT text)',
            _RowSource(lines()),
        )
    return count


@dataclass
class GenerationResult:
    """What was written, for the dataset contract and the pre-migration snapshot."""

    profile: str
    seed: int
    rows: dict[str, int]
    storage_categories: dict[str, int]
    tenant_notes: dict[str, list[str]]


class Generator:
    """Builds one synthetic Céluma 1.2 database.

    Instantiate, then call `generate(conn)`. The connection must point at a
    database already at `v1_2_0`; `generate` refuses to run otherwise.
    """

    #: How many users per tenant may carry a signature.
    #:
    #: "faithful" is what a real Céluma 1.2 lab looks like: every pathologist
    #: or reviewer who signs reports has an uploaded signature, so a tenant
    #: routinely has several. It is also what triggers Block B finding B-001 --
    #: the frozen migration's `signature` CTE is the only one of eight without
    #: `GROUP BY tenant_id`, so two signature-bearing users in one tenant fan
    #: the tenant row out and the `tenant_usage` INSERT dies on its primary
    #: key. The upgrade aborts entirely.
    #:
    #: "one_per_tenant" caps it at one, which is the shape every pre-existing
    #: migration test happens to use and the only shape under which the frozen
    #: migration completes. It exists so that the remaining ~25 Block B
    #: validation dimensions can still be measured while B-001 stands
    #: unrepaired. It is NOT a realistic dataset and must never be presented
    #: as one.
    SIGNATURE_MODES = ("faithful", "one_per_tenant")

    def __init__(self, profile: Profile, seed: int,
                 signature_mode: str = "faithful") -> None:
        if signature_mode not in self.SIGNATURE_MODES:
            raise ValueError(f"unknown signature_mode {signature_mode!r}")
        self.profile = profile
        self.seed = seed
        self.signature_mode = signature_mode
        self.rng = random.Random(seed)
        self.ns = uuid.uuid5(uuid.NAMESPACE_DNS, f"celuma-release-validation-{seed}")

    # -- deterministic primitives ------------------------------------------

    def uid(self, kind: str, index: int) -> uuid.UUID:
        return uuid.uuid5(self.ns, f"{kind}:{index}")

    def word(self) -> str:
        return self.rng.choice(_SYL_A) + self.rng.choice(_SYL_B)

    def ts(self, offset_days: int) -> datetime:
        return EPOCH + timedelta(days=offset_days, seconds=self.rng.randrange(86_400))

    def size(self, low: int, high: int) -> int:
        """A size in bytes. `storage_object.size_bytes` is int4, so every
        bound here stays well inside 2^31."""
        return self.rng.randrange(low, high)

    # -- generation --------------------------------------------------------

    def generate(self, conn) -> GenerationResult:
        p = self.profile
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
            if not row or row[0] != "v1_2_0":
                raise SystemExit(
                    f"refusing to generate: database is at {row[0] if row else 'base'}, "
                    "expected v1_2_0"
                )
            cur.execute("SELECT code, id FROM role")
            roles = dict(cur.fetchall())

        rows: dict[str, int] = {}
        cats: dict[str, int] = {}
        notes: dict[str, list[str]] = {}

        # ---- tenants and branches ----------------------------------------
        # Tenant 0 is deliberately a zero-billable-storage tenant: it gets
        # users and patients but no samples, reports or logo. §23 of the brief
        # makes "a real zero row, never a missing row" a load-bearing
        # invariant, and it can only be tested if such a tenant exists.
        tenants = [self.uid("tenant", i) for i in range(p.tenants)]
        # "Multiple" is what §23 asks for, so two whenever the profile has
        # room. These tenants get users, branches and avatars -- none of which
        # is billable storage -- but no sample, report, signature or logo.
        zero_usage_tenants = {tenants[0]}
        if p.tenants >= 5:
            zero_usage_tenants.add(tenants[1])

        # Which tenants carry a pre-1.3 logo_url.
        logo_tenants = set(tenants[: int(p.tenants * p.logo_fraction)]) - zero_usage_tenants
        # One tenant gets two objects under its logo prefix, so the
        # `HAVING COUNT(*) = 1` ambiguity branch is exercised rather than
        # assumed. (On a 1.2 dataset it cannot resolve anyway -- see
        # TENANT_LOGO_KEY_12 -- but the row shape is present.)
        ambiguous_logo_tenants = set(list(logo_tenants)[:1]) if logo_tenants else set()

        tenant_rows = []
        for i, tid in enumerate(tenants):
            has_logo = tid in logo_tenants
            logo_url = (
                f"{MEDIA_BASE_URL}/" + TENANT_LOGO_KEY_12.format(tenant_id=tid, ext="png")
                if has_logo
                else None
            )
            tenant_rows.append(
                (self.ts(i), tid, f"Lab {self.word().title()} {i}",
                 f"Lab {self.word().title()} {i} SA de CV", f"TAX{i:06d}", logo_url, True)
            )
        rows["tenant"] = _copy(
            conn, "tenant",
            ["created_at", "id", "name", "legal_name", "tax_id", "logo_url", "is_active"],
            iter(tenant_rows),
        )
        notes["zero_usage_tenants"] = [str(t) for t in sorted(zero_usage_tenants, key=str)]
        notes["logo_tenants"] = [str(t) for t in sorted(logo_tenants, key=str)]
        notes["ambiguous_logo_tenants"] = [str(t) for t in sorted(ambiguous_logo_tenants, key=str)]

        branches_per_tenant = 2
        branches: dict[uuid.UUID, list[uuid.UUID]] = {}
        branch_rows = []
        n = 0
        for tid in tenants:
            branches[tid] = []
            for b in range(branches_per_tenant):
                bid = self.uid("branch", n)
                branches[tid].append(bid)
                branch_rows.append(
                    (self.ts(n % 300), bid, tid, f"BR{b:02d}", f"Sucursal {self.word().title()}",
                     "America/Mexico_City", f"Calle {self.word().title()} {n}", None,
                     "Ciudad", "Estado", f"{10000 + n % 80000}", "MX", True)
                )
                n += 1
        rows["branch"] = _copy(
            conn, "branch",
            ["created_at", "id", "tenant_id", "code", "name", "timezone", "address_line1",
             "address_line2", "city", "state", "postal_code", "country", "is_active"],
            iter(branch_rows),
        )

        # ---- users, with the role semantics §24 asks to be able to check ---
        # Five deliberate categories, assigned round-robin so every tenant has
        # a mix: active internal, inactive internal, physician-only,
        # multi-role (physician + pathologist), and roleless-but-active.
        user_rows = []
        user_role_rows = []
        users: list[tuple[uuid.UUID, uuid.UUID]] = []   # (user_id, tenant_id)
        users_by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {t: [] for t in tenants}
        category_counts = {k: 0 for k in
                           ("active_internal", "inactive_internal", "physician_only",
                            "multi_role", "roleless_active")}
        internal_roles = ("admin", "pathologist", "lab_tech", "assistant", "viewer")

        categories = ("active_internal", "inactive_internal", "physician_only",
                      "multi_role", "roleless_active")
        for i in range(p.users):
            uid_ = self.uid("user", i)
            tid = tenants[i % p.tenants]
            # Category advances once per full pass over the tenants, not once
            # per user. Using `i % 5` for both would tie the category to the
            # tenant index whenever `tenants` is a multiple of 5 -- which it is
            # for every profile here -- and each tenant would end up a pure
            # single-category cohort. Real labs have mixed rosters, and §24
            # asks for a tenant where internal and portal users coexist.
            category = categories[(i // p.tenants) % len(categories)]
            category_counts[category] += 1
            is_active = category != "inactive_internal"
            users.append((uid_, tid))
            users_by_tenant[tid].append(uid_)
            user_rows.append(
                (self.ts(i % 400), uid_, tid, f"user{i}@lab{i % p.tenants}.invalid",
                 f"{self.word().title()} {self.word().title()}",
                 "$2b$12$syntheticsyntheticsyntheticsyntheticsyntheticsyntheticsy",
                 is_active, f"user{i}", None, self.word().title(), self.word().title(), None)
            )
            assigned: list[str] = {
                "active_internal": [internal_roles[i % len(internal_roles)]],
                "inactive_internal": [internal_roles[(i + 2) % len(internal_roles)]],
                "physician_only": ["physician"],
                "multi_role": ["physician", "pathologist"],
                "roleless_active": [],
            }[category]
            for j, code in enumerate(assigned):
                user_role_rows.append(
                    (self.uid("user_role", i * 10 + j), self.ts(i % 400), uid_, roles[code])
                )

        rows["app_user"] = _copy(
            conn, "app_user",
            ["created_at", "id", "tenant_id", "email", "full_name", "hashed_password",
             "is_active", "username", "avatar_url", "first_name", "last_name",
             "signature_storage_id"],
            iter(user_rows),
        )
        rows["user_role"] = _copy(
            conn, "user_role", ["id", "created_at", "user_id", "role_id"], iter(user_role_rows)
        )
        notes["user_categories"] = [f"{k}={v}" for k, v in sorted(category_counts.items())]

        # `created_by` on every generated storage object. Any tenant's own
        # user would do; using one keeps the FK satisfied without implying
        # ownership, which at v1_2_0 storage_object cannot express anyway.
        author = users[0][0]

        # ---- patients -----------------------------------------------------
        # Tenants are weighted rather than uniform: one tenant holds a
        # disproportionate share so that "tenant with many storage objects"
        # (§11) is a real case and per-tenant timing is not uniform.
        billable_tenants = [t for t in tenants if t not in zero_usage_tenants] or tenants
        weights = [4.0 if i == 0 else 1.0 for i in range(len(billable_tenants))]

        def pick_tenant() -> uuid.UUID:
            return self.rng.choices(billable_tenants, weights=weights, k=1)[0]

        patient_rows = []
        patients: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for i in range(p.patients):
            tid = pick_tenant()
            bid = self.rng.choice(branches[tid])
            pid = self.uid("patient", i)
            patients.append((pid, tid, bid))
            fn, ln = self.word().title(), self.word().title()
            patient_rows.append(
                (tid, bid, self.ts(i % 700), pid, f"P{i:08d}", fn, ln,
                 (EPOCH - timedelta(days=self.rng.randrange(7000, 30000))).date(),
                 self.rng.choice(("M", "F")), f"55{i % 100000000:08d}",
                 f"patient{i}@example.invalid", f"{fn} {ln}", f"Calle {self.word().title()} {i}")
            )
        rows["patient"] = _copy(
            conn, "patient",
            ["tenant_id", "branch_id", "created_at", "id", "patient_code", "first_name",
             "last_name", "dob", "sex", "phone", "email", "full_name", "address"],
            iter(patient_rows),
        )

        # ---- orders -------------------------------------------------------
        order_rows = []
        orders: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for i in range(p.orders):
            pid, tid, bid = patients[i % len(patients)] if patients else (None, tenants[0], branches[tenants[0]][0])
            oid = self.uid("order", i)
            orders.append((oid, tid, bid, pid))
            order_rows.append(
                (self.ts(i % 700), oid, tid, bid, pid, f"O{i:08d}",
                 self.rng.choice(ORDER_STATUSES), f"Dr. {self.word().title()}", None,
                 False, author, None, None, None, None)
            )
        rows["order"] = _copy(
            conn, '"order"',
            ["created_at", "id", "tenant_id", "branch_id", "patient_id", "order_code",
             "status", "requested_by", "notes", "billed_lock", "created_by", "report_id",
             "study_type_id", "invoice_id", "requesting_physician_id"],
            iter(order_rows),
        )

        # ---- reports and report versions -----------------------------------
        report_rows = []
        reports: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for i in range(p.reports):
            oid, tid, bid, _pid = orders[i % len(orders)]
            rid = self.uid("report", i)
            reports.append((rid, tid, bid, oid))
            report_rows.append(
                (self.ts(i % 700), rid, tid, bid, oid, self.rng.choice(REPORT_STATUSES),
                 f"Informe {self.word().title()}", None, author, None)
            )
        rows["report"] = _copy(
            conn, "report",
            ["created_at", "id", "tenant_id", "branch_id", "order_id", "status", "title",
             "published_at", "created_by", "template"],
            iter(report_rows),
        )
        # `order.report_id` is UNIQUE and each report points back at a distinct
        # order, so this closes the 1:1 loop the FK pair describes.
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE "order" o SET report_id = r.id FROM report r WHERE r.order_id = o.id'
            )

        # ---- storage objects ------------------------------------------------
        # Built in one pass per category so the totals are auditable, and
        # streamed into COPY so LARGE never materialises 650k tuples at once.
        storage_cols = ["created_at", "id", "provider", "region", "bucket", "object_key",
                        "version_id", "etag", "sha256_hex", "content_type", "size_bytes",
                        "created_by"]

        def storage_row(sid, key, content_type, size, i):
            # sha256_hex is None on every row: see the module docstring.
            return (self.ts(i % 700), sid, "aws", "mx-central-1", "celuma-media-synthetic",
                    key, None, f"{i:032x}", None, content_type, size, author)

        # samples first -- their images drive the largest category.
        sample_rows = []
        samples: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for i in range(p.samples):
            oid, tid, bid, _pid = orders[i % len(orders)]
            sid = self.uid("sample", i)
            samples.append((sid, tid, bid, oid))
            sample_rows.append(
                (sid, tid, bid, oid, f"S{i:08d}", self.rng.choice(SAMPLE_TYPES),
                 self.rng.choice(SAMPLE_STATES), self.ts(i % 700), self.ts(i % 700), None)
            )
        rows["sample"] = _copy(
            conn, "sample",
            ["id", "tenant_id", "branch_id", "order_id", "sample_code", "type", "state",
             "collected_at", "received_at", "notes"],
            iter(sample_rows),
        )

        image_storage: list[tuple] = []
        image_rows: list[tuple] = []
        rendition_rows: list[tuple] = []
        rendition_storage: list[tuple] = []
        img_i = 0
        ren_i = 0
        for s_index, (sample_id, tid, bid, _oid) in enumerate(samples):
            for k in range(p.images_per_sample):
                so_id = self.uid("so_image", img_i)
                key = SAMPLE_PROCESSED_KEY.format(
                    tenant_id=tid, branch_id=bid, sample_id=sample_id,
                    name=f"img{k}", uid=f"{img_i:08x}")
                image_storage.append(storage_row(so_id, key, "image/jpeg",
                                                 self.size(180_000, 2_400_000), img_i))
                si_id = self.uid("sample_image", img_i)
                image_rows.append(
                    (self.ts(s_index % 700), si_id, tid, bid, sample_id, so_id,
                     f"Campo {k + 1}", k == 0, author)
                )
                for r in range(p.renditions_per_image):
                    r_so = self.uid("so_rendition", ren_i)
                    kind, tmpl, ctype, lo, hi = (
                        ("thumbnail", SAMPLE_THUMBNAIL_KEY, "image/jpeg", 8_000, 60_000)
                        if r == 0 else
                        ("raw", SAMPLE_RAW_KEY, "image/tiff", 2_000_000, 12_000_000)
                    )
                    rendition_storage.append(storage_row(
                        r_so,
                        tmpl.format(tenant_id=tid, branch_id=bid, sample_id=sample_id,
                                    name=f"img{k}", uid=f"{ren_i:08x}"),
                        ctype, self.size(lo, hi), ren_i))
                    rendition_rows.append(
                        (self.uid("rendition", ren_i), si_id, kind, r_so))
                    ren_i += 1
                img_i += 1

        # report JSON bodies and legacy/manual PDFs
        version_rows: list[tuple] = []
        json_storage: list[tuple] = []
        pdf_storage: list[tuple] = []
        v_i = 0
        pdf_i = 0
        for r_index, (rid, tid, bid, _oid) in enumerate(reports):
            versions = 2 if (r_index % 2 == 0 and p.versions_per_report >= 1.5) else 1
            for vn in range(1, versions + 1):
                j_so = self.uid("so_json", v_i)
                json_storage.append(storage_row(
                    j_so,
                    REPORT_JSON_KEY.format(tenant_id=tid, branch_id=bid, report_id=rid, n=vn),
                    "application/json", self.size(4_000, 120_000), v_i))
                has_pdf = self.rng.random() < p.legacy_pdf_fraction
                p_so = None
                if has_pdf:
                    p_so = self.uid("so_pdf", pdf_i)
                    pdf_storage.append(storage_row(
                        p_so,
                        REPORT_PDF_KEY.format(tenant_id=tid, branch_id=bid, report_id=rid, n=vn),
                        "application/pdf", self.size(90_000, 3_000_000), pdf_i))
                    pdf_i += 1
                version_rows.append(
                    (self.ts(r_index % 700), self.uid("report_version", v_i), rid, vn,
                     p_so, None, None, author, self.ts(r_index % 700), vn == versions,
                     j_so, None, None)
                )
                v_i += 1

        # signatures, avatars (control) and tenant logos
        signature_storage: list[tuple] = []
        signature_updates: list[tuple] = []
        avatar_storage: list[tuple] = []
        signed_tenants: set = set()
        for i, (uid_, tid) in enumerate(users):
            wants_signature = self.rng.random() < p.signature_fraction
            # A zero-billable-storage tenant must own no billable object at
            # all, and a signature is billable.
            if tid in zero_usage_tenants:
                wants_signature = False
            if self.signature_mode == "one_per_tenant":
                # Draw from the same PRNG either way, so the two modes stay
                # comparable downstream; only the acceptance differs.
                wants_signature = wants_signature and tid not in signed_tenants
            if wants_signature:
                signed_tenants.add(tid)
                s_so = self.uid("so_signature", i)
                signature_storage.append(storage_row(
                    s_so,
                    SIGNATURE_KEY.format(tenant_id=tid, user_id=uid_, ts=1_700_000_000 + i),
                    "image/png", self.size(12_000, 240_000), i))
                signature_updates.append((s_so, uid_))
            if self.rng.random() < p.avatar_fraction:
                a_so = self.uid("so_avatar", i)
                avatar_storage.append(storage_row(
                    a_so, AVATAR_KEY.format(user_id=uid_), "image/jpeg",
                    self.size(20_000, 400_000), i))

        logo_storage: list[tuple] = []
        for i, tid in enumerate(sorted(logo_tenants, key=str)):
            logo_storage.append(storage_row(
                self.uid("so_logo", i),
                TENANT_LOGO_KEY_12.format(tenant_id=tid, ext="png"),
                "image/png", self.size(15_000, 500_000), i))
            if tid in ambiguous_logo_tenants:
                logo_storage.append(storage_row(
                    self.uid("so_logo_alt", i),
                    TENANT_LOGO_KEY_12.format(tenant_id=tid, ext="svg"),
                    "image/svg+xml", self.size(15_000, 500_000), i))

        all_storage = (image_storage + rendition_storage + json_storage + pdf_storage
                       + signature_storage + avatar_storage + logo_storage)
        rows["storage_object"] = _copy(conn, "storage_object", storage_cols, iter(all_storage))
        cats = {
            "sample_image_processed": len(image_storage),
            "sample_image_rendition": len(rendition_storage),
            "report_json": len(json_storage),
            "report_legacy_pdf": len(pdf_storage),
            "live_signature": len(signature_storage),
            "tenant_logo": len(logo_storage),
            "user_avatar_control": len(avatar_storage),
        }

        rows["sample_image"] = _copy(
            conn, "sample_image",
            ["created_at", "id", "tenant_id", "branch_id", "sample_id", "storage_id",
             "label", "is_primary", "created_by"],
            iter(image_rows),
        )
        rows["sample_image_rendition"] = _copy(
            conn, "sample_image_rendition",
            ["id", "sample_image_id", "kind", "storage_id"], iter(rendition_rows)
        )
        rows["report_version"] = _copy(
            conn, "report_version",
            ["created_at", "id", "report_id", "version_no", "pdf_storage_id",
             "html_storage_id", "changelog", "authored_by", "authored_at", "is_current",
             "json_storage_id", "signed_by", "signed_at"],
            iter(version_rows),
        )

        # signature_storage_id closes the app_user <-> storage_object cycle the
        # FK pair creates; it cannot be set during the initial COPY.
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE app_user SET signature_storage_id = %s WHERE id = %s",
                signature_updates,
            )

        conn.commit()
        notes["signature_mode"] = [self.signature_mode]
        notes["tenants_with_signature_users"] = [str(len(signed_tenants))]
        return GenerationResult(
            profile=p.name, seed=self.seed, rows=rows,
            storage_categories=cats, tenant_notes=notes,
        )
