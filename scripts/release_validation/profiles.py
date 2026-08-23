"""Synthetic Céluma 1.2 dataset profiles for Phase 5 Block B.

TEST TOOLING ONLY. Nothing under `scripts/release_validation/` may be
imported from `app/`, and nothing here runs at application startup.

Each profile describes a *shape*, not a fixed row count: the domain counts
below are inputs, and the storage-object total is whatever that shape
produces. `storage_object` is the scale dimension that matters, because the
frozen `v1_3_0` migration's section 15 does bulk attribution and usage
baselining over exactly that table.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """One synthetic dataset shape.

    The `*_per_*` ratios are what turn domain counts into storage objects.
    They are held constant across profiles so that the three runs differ in
    scale alone, which is what makes their timings comparable.
    """

    name: str

    tenants: int
    users: int
    patients: int
    orders: int
    samples: int
    reports: int

    #: Processed sample images per sample.
    images_per_sample: int = 2
    #: Renditions per processed image (thumbnail + raw, as Céluma 1.2 wrote).
    renditions_per_image: int = 2
    #: report_version rows per report, as a rate (1.5 => half get a v2).
    versions_per_report: float = 1.5
    #: Fraction of report_versions carrying a legacy/manual pdf_storage_id.
    legacy_pdf_fraction: float = 0.20
    #: Fraction of users with an uploaded signature.
    signature_fraction: float = 0.40
    #: Fraction of users with an avatar (a NON-billable negative control).
    avatar_fraction: float = 1.00
    #: Fraction of tenants carrying a pre-1.3 logo_url.
    logo_fraction: float = 0.80

    def estimated_storage_objects(self) -> int:
        """What `generate()` will produce, before it produces it.

        Used only to print an expectation next to the measured result; the
        generator never reads this back.
        """
        images = self.samples * self.images_per_sample
        renditions = images * self.renditions_per_image
        versions = int(self.reports * self.versions_per_report)
        return (
            images
            + renditions
            + versions                                      # report JSON bodies
            + int(versions * self.legacy_pdf_fraction)      # legacy/manual PDFs
            + int(self.users * self.signature_fraction)     # live signatures
            + int(self.users * self.avatar_fraction)        # avatars (control)
            + int(self.tenants * self.logo_fraction)        # tenant logos
        )


# Order-of-magnitude targets come from the Block B brief §9. The domain counts
# are tuned so the resulting storage_object totals land on ~10k / ~100k / ~650k
# while keeping the per-sample and per-report distributions identical.
SMALL = Profile(
    name="SMALL",
    tenants=5,
    users=50,
    patients=500,
    orders=1_000,
    samples=1_500,
    reports=1_000,
)

MEDIUM = Profile(
    name="MEDIUM",
    tenants=20,
    users=300,
    patients=5_000,
    orders=10_000,
    samples=15_000,
    reports=10_000,
)

# LARGE's brief is "500,000-1,000,000 storage objects, with related domain rows
# scaled enough to keep distributions realistic". Holding every ratio at the
# MEDIUM value and scaling the domain by 6 lands at ~649,000.
LARGE = Profile(
    name="LARGE",
    tenants=40,
    users=900,
    patients=30_000,
    orders=60_000,
    samples=90_000,
    reports=60_000,
)

PROFILES = {p.name: p for p in (SMALL, MEDIUM, LARGE)}
