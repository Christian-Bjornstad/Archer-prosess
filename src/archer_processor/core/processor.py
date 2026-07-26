from __future__ import annotations

from datetime import datetime
from pathlib import Path

from archer_processor.io.tsv_reader import ArcherTsvReader
from archer_processor.knowledge.history import VariantHistoryRepository

from .models import ProcessingResult
from .rules import FilterEngine


class VariantProcessor:
    def __init__(
        self,
        reader: ArcherTsvReader | None = None,
        filter_engine: FilterEngine | None = None,
        history: VariantHistoryRepository | None = None,
    ) -> None:
        self.reader = reader or ArcherTsvReader()
        self.filter_engine = filter_engine or FilterEngine()
        self.history = history

    def process(self, input_path: Path, run_date: str, output_path: Path | None = None) -> ProcessingResult:
        started = datetime.now()
        variants = self.reader.read(input_path)
        self.filter_engine.apply(variants)
        if self.history:
            self.history.annotate(variants)
        return ProcessingResult(
            input_path=input_path,
            output_path=output_path,
            run_date=run_date,
            variants=variants,
            rules_applied=[rule.rule_id for rule in self.filter_engine.rules],
            started_at=started,
            finished_at=datetime.now(),
        )
