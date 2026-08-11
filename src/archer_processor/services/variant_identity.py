from __future__ import annotations

import re
from dataclasses import dataclass

from archer_processor.core.models import VariantRecord


@dataclass(frozen=True, slots=True)
class GenomicIdentity:
    assembly: str
    chromosome: str
    position: int
    reference: str
    alternate: str


def genomic_identity(
    variant: VariantRecord,
    assembly: str = "GRCh37",
) -> GenomicIdentity | None:
    location = re.sub(r"\s+", "", variant.genomic_location or "")
    match = re.fullmatch(
        r"(?:chr)?(?P<chromosome>[0-9]{1,2}|X|Y|M|MT):"
        r"(?P<position>\d+)(?:-\d+)?",
        location,
        flags=re.IGNORECASE,
    )
    reference = re.sub(r"\s+", "", variant.ref_allele or "").upper()
    alternate = re.sub(r"\s+", "", variant.alt_allele or "").upper()
    if not match or not reference or not alternate:
        return None
    chromosome = match.group("chromosome").upper()
    if chromosome == "MT":
        chromosome = "M"
    return GenomicIdentity(
        assembly,
        chromosome,
        int(match.group("position")),
        reference,
        alternate,
    )
