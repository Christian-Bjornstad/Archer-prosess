from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class GenomicChange:
    chromosome: str
    kind: Literal["substitution", "deletion", "insertion", "delins"]
    start: int
    end: int
    ref: str
    alt: str


def normalize_vcf_change(
    location: str, ref: str, alt: str
) -> GenomicChange | None:
    compact_location = re.sub(r"\s+", "", location or "")
    match = re.fullmatch(
        r"(?:chr)?(?P<chromosome>[0-9]{1,2}|X|Y|M|MT):"
        r"(?P<position>\d+)(?:-\d+)?",
        compact_location,
        flags=re.IGNORECASE,
    )
    normalized_ref = re.sub(r"\s+", "", ref or "").upper()
    normalized_alt = re.sub(r"\s+", "", alt or "").upper()
    if (
        match is None
        or not normalized_ref
        or not normalized_alt
        or normalized_ref == normalized_alt
        or re.fullmatch(r"[ACGT]+", normalized_ref) is None
        or re.fullmatch(r"[ACGT]+", normalized_alt) is None
    ):
        return None

    chromosome = match.group("chromosome").upper()
    if chromosome == "MT":
        chromosome = "M"
    position = int(match.group("position"))

    while (
        normalized_ref
        and normalized_alt
        and normalized_ref[-1] == normalized_alt[-1]
    ):
        normalized_ref = normalized_ref[:-1]
        normalized_alt = normalized_alt[:-1]
    while (
        normalized_ref
        and normalized_alt
        and normalized_ref[0] == normalized_alt[0]
    ):
        normalized_ref = normalized_ref[1:]
        normalized_alt = normalized_alt[1:]
        position += 1

    if not normalized_ref:
        if position <= 1:
            return None
        return GenomicChange(
            chromosome, "insertion", position - 1, position, "", normalized_alt
        )
    end = position + len(normalized_ref) - 1
    if not normalized_alt:
        return GenomicChange(
            chromosome, "deletion", position, end, normalized_ref, ""
        )
    if len(normalized_ref) == len(normalized_alt) == 1:
        return GenomicChange(
            chromosome,
            "substitution",
            position,
            position,
            normalized_ref,
            normalized_alt,
        )
    return GenomicChange(
        chromosome, "delins", position, end, normalized_ref, normalized_alt
    )


def format_mtbp_grch37(location: str, ref: str, alt: str) -> str:
    change = normalize_vcf_change(location, ref, alt)
    if change is None:
        return ""
    prefix = f"chr{change.chromosome}:g."
    coordinate = (
        str(change.start)
        if change.start == change.end
        else f"{change.start}_{change.end}"
    )
    if change.kind == "substitution":
        return f"{prefix}{change.start}{change.ref}>{change.alt}"
    if change.kind == "deletion":
        return f"{prefix}{coordinate}del"
    if change.kind == "insertion":
        return f"{prefix}{change.start}_{change.end}ins{change.alt}"
    return f"{prefix}{coordinate}delins{change.alt}"
