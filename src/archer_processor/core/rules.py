from __future__ import annotations

import re

from .models import FilterRule, VariantRecord


def default_artifact_rules() -> list[dict[str, str]]:
    return [
        {
            "gene": "ASXL1",
            "hgvsc": "NM_015338.5:c.1934dup",
            "max_af": "5.5%",
            "reason": "Tier II mutation above background; artifact through 5.5% AF.",
        },
        {
            "gene": "ASXL1",
            "hgvsc": "NM_015338.5:c.1933_1934dup",
            "reason": "Artifact associated with ASXL1 c.1934dup.",
        },
        {
            "gene": "ATRX",
            "hgvsc": "NM_000489.4:c.5698-7del",
            "reason": "Homopolymer artifact.",
        },
        {
            "gene": "ATRX",
            "hgvsc": "NM_000489.4:c.5698-7dup",
            "reason": "Homopolymer artifact.",
        },
        {"gene": "CBL", "hgvsc": "NM_005188.3:c.1380_1382del", "reason": "Repetitive TGA region."},
        {"gene": "CEBPA", "hgvsc": "NM_004364.4:c.288C>G", "reason": "GC-repetitive region; fragmentation v1 artifact."},
        {"gene": "CEBPA", "hgvsc": "NM_004364.4:c.280G>C", "reason": "GC-repetitive region; fragmentation v1 artifact."},
        {"gene": "CEBPA", "hgvsc": "NM_004364.4:c.296G>C", "reason": "GC-repetitive region; fragmentation v1 artifact."},
        {"gene": "CEBPA", "hgvsc": "NM_004364.4:c.566C>A", "reason": "Artifact associated with SNP."},
        {"gene": "CEBPA", "hgvsc": "NM_004364.4:c.568T>C", "reason": "Artifact associated with SNP."},
        {"gene": "DDX41", "hgvsc": "NM_016222.3:c.1223T>A", "reason": "Observed in some runs."},
        {"gene": "EZH2", "hgvsc": "NM_004456.4:c.118-5_118-4del", "reason": "Homopolymer artifact."},
        {"gene": "EZH2", "hgvsc": "NM_004456.4:c.118-4dup", "reason": "Homopolymer artifact."},
        {"gene": "EZH2", "hgvsc": "NM_004456.4:c.118-4T>A", "reason": "Very high sample strand-bias ratio."},
        {"gene": "FLT3", "hgvsc": "NM_004119.2:c.1419-4del", "reason": "Observed in all samples."},
        {"gene": "FLT3", "hgvsc": "NM_004119.2:c.1419-4dup", "reason": "Observed in all samples."},
        {"gene": "JAK2", "hgvsc": "NM_004972.3:c.3291+16dup", "reason": "Homopolymer artifact."},
        {"gene": "JAK2", "hgvsc": "NM_004972.3:c.3291+16del", "reason": "Homopolymer artifact."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.4965_4966delinsGA", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.4966G>A", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.4958_4959delinsCA", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.4958T>C", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.6723_6724inv", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.6729_6730inv", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.6730A>G", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.6717_6718inv", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.6718A>G", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "NOTCH1", "hgvsc": "NM_017617.4:c.7244_7246del", "reason": "Observed in some runs."},
        {"gene": "PTEN", "hgvsc": "NM_000314.6:c.835C>G", "reason": "Often filtered due to strand bias."},
        {"gene": "RUNX1", "hgvsc": "NM_001754.4:c.1265A>G", "reason": "Listed in Artefakter DNA Fragmentering v2."},
        {"gene": "RUNX1", "hgvsc": "NM_001754.4:c.1261_1262inv", "reason": "Observed in some runs."},
        {"gene": "RUNX1", "hgvsc": "NM_001754.4:c.1261G>C", "reason": "Observed in some runs."},
        {"gene": "RUNX1", "hgvsc": "NM_001754.4:c.1274_1275insCCCCCCC", "reason": "Observed in some runs."},
        {"gene": "RUNX1", "hgvsc": "NM_001754.4:c.1274_1275insCCCC", "reason": "Observed in some runs."},
        {"gene": "SRSF2", "hgvsc": "NM_003016.4:c.248G>C", "reason": "Outside hotspot."},
        {"gene": "STAG2", "hgvsc": "NM_006603.4:c.1535-2A>T", "reason": "Very long homopolymer."},
        {"gene": "STAG2", "hgvsc": "NM_006603.4:c.1535-2dup", "reason": "Artifact caused by NM_006603.4:c.1535-3T>A."},
        {"gene": "STAG2", "hgvsc": "NM_006603.4:c.1535-3_1535-2insTA", "reason": "Artifact caused by NM_006603.4:c.1535-3T>A."},
        {"gene": "NPM1", "hgvsc": "NM_002520.6:c.847-3dup", "reason": "Artifact caused by NM_002520.6:c.847-5T>C."},
    ]


def production_rules(artifact_rules: list[dict[str, str]] | None = None) -> list[FilterRule]:
    return artifact_filter_rules(
        default_artifact_rules() if artifact_rules is None else artifact_rules
    )


def artifact_filter_rules(entries: list[dict[str, str]]) -> list[FilterRule]:
    rules = []
    for entry in entries:
        gene = str(entry.get("gene") or "").strip().upper()
        hgvsc = str(entry.get("hgvsc") or "").strip()
        if not hgvsc:
            continue
        reason = str(entry.get("reason") or "").strip() or "Configured artifact; excluded regardless of AF."
        max_af = _parse_af_threshold(entry.get("max_af"))
        name = f"{gene + ' ' if gene else ''}{_cdna_label(hgvsc)} artifact".strip()
        rules.append(
            FilterRule(
                rule_id=f"{_slug(gene or 'variant')}_{_slug(_cdna_label(hgvsc))}_artifact",
                name=name,
                gene=gene,
                hgvsc=hgvsc,
                reason=reason,
                max_af_inclusive=max_af,
            )
        )
    return rules


class FilterEngine:
    def __init__(self, rules: list[FilterRule] | None = None) -> None:
        self.rules = rules or production_rules()

    def apply(self, variants: list[VariantRecord]) -> list[VariantRecord]:
        for variant in variants:
            variant.decision = "included"
            variant.decision_reason = ""
            variant.matched_rules.clear()
            for rule in self.rules:
                if self._matches(rule, variant):
                    variant.decision = rule.action
                    variant.decision_reason = rule.reason
                    variant.matched_rules.append(rule.rule_id)
                    break
            self._add_review_flags(variant)
        return variants

    def _matches(self, rule: FilterRule, variant: VariantRecord) -> bool:
        if rule.gene and variant.symbol.upper() != rule.gene.upper():
            return False
        if variant.hgvsc != rule.hgvsc:
            return False
        if rule.max_af_exclusive is None:
            if rule.max_af_inclusive is None:
                return True
            return variant.af is not None and variant.af <= rule.max_af_inclusive
        return variant.af is not None and variant.af < rule.max_af_exclusive

    def _add_review_flags(self, variant: VariantRecord) -> None:
        symbol = variant.symbol.upper()
        raw_text = " ".join(str(value).lower() for value in variant.raw.values())
        if symbol == "ASXL1" and "missense" in variant.consequence.lower():
            variant.warnings.append("ASXL1 missense variant requires special review.")
        if symbol == "CEBPA" and "b-zip" in raw_text:
            variant.warnings.append("CEBPA b-ZIP region variant requires comment.")
        if symbol == "TP53" and variant.af is not None and (variant.af > 0.50 or 0.40 <= variant.af <= 0.60):
            variant.warnings.append("TP53 AF may require multihit/germline assessment.")
        if "yes" in variant.has_seq_dir_bias.lower() or "yes" in variant.has_sample_strand_bias.lower():
            variant.warnings.append("Strand/direction bias present; review read support.")


def _cdna_label(hgvsc: str) -> str:
    return hgvsc.split(":", 1)[-1]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _parse_af_threshold(value: object) -> float | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        if text.endswith("%"):
            parsed = float(text[:-1].strip()) / 100
        else:
            parsed = float(text)
            if parsed > 1:
                parsed /= 100
    except ValueError:
        raise ValueError(f"Invalid artifact AF threshold: {value!r}") from None
    if not 0 <= parsed <= 1:
        raise ValueError(f"Artifact AF threshold must be between 0% and 100%: {value!r}")
    return parsed
