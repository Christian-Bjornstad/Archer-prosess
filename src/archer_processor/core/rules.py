from __future__ import annotations

from .models import FilterRule, VariantRecord


def production_rules() -> list[FilterRule]:
    return [
        FilterRule(
            rule_id="flt3_1419_4dup_artifact",
            name="FLT3 c.1419-4dup artifact",
            gene="FLT3",
            hgvsc="NM_004119.2:c.1419-4dup",
            reason="Known recurrent FLT3 artifact; excluded regardless of AF.",
        ),
        FilterRule(
            rule_id="flt3_1419_4del_artifact",
            name="FLT3 c.1419-4del artifact",
            gene="FLT3",
            hgvsc="NM_004119.2:c.1419-4del",
            reason="Known recurrent FLT3 artifact; excluded regardless of AF.",
        ),
        FilterRule(
            rule_id="jak2_3291_16dup_artifact",
            name="JAK2 c.3291+16dup artifact",
            gene="JAK2",
            hgvsc="NM_004972.3:c.3291+16dup",
            reason="Known recurrent JAK2 artifact; excluded regardless of AF.",
        ),
        FilterRule(
            rule_id="jak2_3291_16del_artifact",
            name="JAK2 c.3291+16del artifact",
            gene="JAK2",
            hgvsc="NM_004972.3:c.3291+16del",
            reason="Known recurrent JAK2 artifact; excluded regardless of AF.",
        ),
        FilterRule(
            rule_id="asxl1_1934dup_low_af",
            name="ASXL1 c.1934dup low AF",
            gene="ASXL1",
            hgvsc="NM_015338.5:c.1934dup",
            max_af_exclusive=0.045,
            reason="ASXL1 c.1934dup below 4.5% AF threshold.",
        ),
    ]


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
        if variant.hgvsc != rule.hgvsc:
            return False
        if rule.max_af_exclusive is None:
            return True
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
