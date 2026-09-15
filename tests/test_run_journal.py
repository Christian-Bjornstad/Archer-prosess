from concurrent.futures import ThreadPoolExecutor
import json

from archer_processor.services.run_journal import RunJournal


def test_run_journal_preserves_more_than_gui_history(tmp_path):
    journal = RunJournal.start(tmp_path, run_mode="evidence", app_version="0.1.0")

    for index in range(620):
        assert journal.record(f"event {index}", source="ClinVar", patient_index=3)
    journal.close()

    text_lines = journal.text_path.read_text(encoding="utf-8").splitlines()
    records = [
        json.loads(line)
        for line in journal.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(text_lines) == 620
    assert len(records) == 620
    assert {record["run_id"] for record in records} == {journal.run_id}
    assert records[-1]["message"] == "event 619"
    assert records[-1]["source"] == "ClinVar"


def test_run_journal_serializes_concurrent_sources_without_losing_events(tmp_path):
    journal = RunJournal.start(tmp_path, run_mode="evidence", app_version="0.1.0")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda index: journal.record(
                    f"parallel {index}", source=f"source-{index % 4}"
                ),
                range(120),
            )
        )
    journal.close()

    records = [
        json.loads(line)
        for line in journal.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(results)
    assert len(records) == 120
    assert {record["message"] for record in records} == {
        f"parallel {index}" for index in range(120)
    }


def test_run_journal_redacts_credentials_and_does_not_raise_on_write_failure(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    journal = RunJournal.start(
        blocked,
        run_mode="evidence",
        app_version="0.1.0",
    )

    assert not journal.record(
        "login failed",
        password="not-for-the-log",
        session_token="also-secret",
        stage="login",
    )
    assert journal.last_error


def test_run_journal_extracts_provider_result_fields(tmp_path):
    journal = RunJournal.start(tmp_path, run_mode="evidence", app_version="0.1.0")

    journal.record(
        "RESULT | source=OncoKB | variant=NF1:p.Y2285Tfs*5 | "
        "status=manual_review | retryable=no | stage=identity verification"
    )

    record = json.loads(journal.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["event_type"] == "RESULT"
    assert record["source"] == "OncoKB"
    assert record["variant"] == "NF1:p.Y2285Tfs*5"
    assert record["status"] == "manual_review"
    assert record["retryable"] == "no"
    assert record["stage"] == "identity verification"


def test_run_journal_extracts_provider_fields_after_patient_lane_prefix(tmp_path):
    journal = RunJournal.start(tmp_path, run_mode="evidence", app_version="0.1.0")

    journal.record(
        "Patient 9/22 (26OUM13887) · MTBP: RESULT | source=MTBP | "
        "variant=NM_002467.4:c.221C>A | status=submission_unknown | "
        "retryable=yes | stage=submission_acceptance"
    )

    record = json.loads(journal.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["event_type"] == "RESULT"
    assert record["source"] == "MTBP"
    assert record["status"] == "submission_unknown"
    assert record["stage"] == "submission_acceptance"
    assert record["patient_index"] == "9"
    assert record["patient_total"] == "22"
    assert record["patient_id"] == "26OUM13887"
    assert record["lane"] == "MTBP"


def test_run_journal_structures_operational_summary_events(tmp_path):
    journal = RunJournal.start(tmp_path, run_mode="evidence", app_version="0.1.0")

    journal.record(
        "RUN SUMMARY | found_complete=3 | found_incomplete=1 | deferred=2 | total=6"
    )
    journal.record(
        "Patient 2/4 (26OUM2) · MTBP: MTBP PREFLIGHT | reports=4 | "
        "protected_reports=1 | available_slots=1"
    )

    records = [
        json.loads(line)
        for line in journal.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["event_type"] == "RUN_SUMMARY"
    assert records[0]["total"] == "6"
    assert records[1]["event_type"] == "MTBP_PREFLIGHT"
    assert records[1]["patient_id"] == "26OUM2"
    assert records[1]["available_slots"] == "1"
