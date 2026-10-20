from __future__ import annotations

import re
from collections.abc import Iterable

from .models import VariantRecord


_AMINO_ACIDS = dict(zip(
    (
        "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
        "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
        "TER", "SEC", "PYL",
    ),
    "ARNDCQEGHILKMFPSTWYV*UO",
))
_AMINO_PATTERN = re.compile("|".join(_AMINO_ACIDS), re.IGNORECASE)
_MISSING = frozenset({"", ".", "?", "N/A", "NA", "UNKNOWN"})


def _compact(value: str) -> str:
    value = re.sub(r"\s+", "", value).upper()
    return "" if value in _MISSING else value


def _protein(value: str) -> str:
    value = _compact(value).rsplit(":", 1)[-1].removeprefix("P.")
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if value in _MISSING:
        return ""
    return _AMINO_PATTERN.sub(lambda match: _AMINO_ACIDS[match.group().upper()], value)


def _locus(value: str) -> str:
    value = _compact(value)
    match = re.fullmatch(r"(?:CHR)?([\w]+):(\d+)(?:-(\d+))?", value)
    if not match:
        return value
    chromosome, start, end = match.groups()
    chromosome = "M" if chromosome == "MT" else chromosome
    start = str(int(start))
    end = str(int(end)) if end else start
    return f"{chromosome}:{start}" + (f"-{end}" if start != end else "")


def validate_variant_keys(variants: Iterable[VariantRecord]) -> None:
    """Reject contradictory identities that would overwrite one evidence key.

    Evidence and reports share the existing sample|HGVSc key. Repeated rows are
    safe when their known identity fields agree; absent optional fields do not
    contradict a known value. Remember each known field across the entire group
    so an incomplete duplicate cannot hide a later conflicting allele or locus.
    """
    known: dict[str, dict[str, tuple[str, VariantRecord]]] = {}
    for variant in variants:
        key = f"{variant.sample}|{variant.hgvsc}"
        fields = {
            "gene": _compact(variant.symbol),
            "protein": _protein(variant.hgvsp),
            "genomic location": _locus(variant.genomic_location),
            "reference allele": _compact(variant.ref_allele),
            "alternate allele": _compact(variant.alt_allele),
        }
        previous = known.setdefault(key, {})
        for field, value in fields.items():
            if not value:
                continue
            if field in previous and previous[field][0] != value:
                first = previous[field][1]
                raise ValueError(
                    f"Distinct variants share the same evidence key for sample "
                    f"'{variant.sample}' and HGVSc '{variant.hgvsc or '<missing>'}' "
                    f"(rows {first.source_row} and {variant.source_row}: conflicting {field}). "
                    "Correct the variant annotation before importing or searching."
                )
            previous.setdefault(field, (value, variant))
