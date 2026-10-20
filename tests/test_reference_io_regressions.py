import pytest
from openpyxl import Workbook

from archer_processor.reports.who_genes import load_who_driver_genes


def test_corrupt_excel_reference_returns_validation_error(tmp_path):
    source = tmp_path / "corrupt-reference.xlsx"
    source.write_bytes(b"not an Excel zip archive")

    with pytest.raises(ValueError, match="WHO.*Excel|WHO.*les"):
        load_who_driver_genes(source)


def test_malformed_excel_reference_releases_input_file_even_when_error_is_retained(tmp_path):
    from zipfile import ZipFile

    source = tmp_path / "malformed-reference.xlsx"
    with ZipFile(source, "w") as archive:
        archive.writestr("dummy.txt", "synthetic archive without workbook metadata")

    with pytest.raises(ValueError) as error:
        load_who_driver_genes(source)

    source.rename(tmp_path / "renamed-reference.xlsx")
    assert error.value.__cause__ is not None


def test_csv_reference_uses_named_gene_column(tmp_path):
    source = tmp_path / "reference.csv"
    source.write_text("Index,Gen\n1,TP53\n2,RUNX1\n", encoding="utf-8")

    assert load_who_driver_genes(source) == frozenset({"TP53", "RUNX1"})


def test_semicolon_reference_keeps_quoted_commas_in_metadata(tmp_path):
    source = tmp_path / "reference.csv"
    source.write_text('Gen;Merknad\nTP53;"alpha,beta,gamma,delta"\nRUNX1;"one,two,three,four"\n', encoding="utf-8")

    assert load_who_driver_genes(source) == frozenset({"TP53", "RUNX1"})


@pytest.mark.parametrize("suffix", [".txt", ".csv", ".xlsx"])
def test_valid_headerless_reference_preserves_first_gene_and_blank_rows(tmp_path, suffix):
    source = tmp_path / f"reference{suffix}"
    if suffix == ".xlsx":
        workbook = Workbook()
        for row in [[], ["TP53"], [], ["RUNX1"]]:
            workbook.active.append(row)
        workbook.save(source)
        workbook.close()
    else:
        source.write_text("\nTP53\n\nRUNX1\n", encoding="utf-8")

    assert load_who_driver_genes(source) == frozenset({"TP53", "RUNX1"})


@pytest.mark.parametrize("invalid_value", ["=RUNX1", "RUNX1 / uncertain", 123])
def test_excel_reference_rejects_invalid_gene_with_row_error(tmp_path, invalid_value):
    source = tmp_path / "reference.xlsx"
    workbook = Workbook()
    workbook.active.append(["Gen"])
    workbook.active.append(["TP53"])
    workbook.active.append([invalid_value])
    workbook.save(source)
    workbook.close()

    with pytest.raises(ValueError, match="[Rr]ad 3|[Rr]ow 3"):
        load_who_driver_genes(source)


@pytest.mark.parametrize("invalid_row", ["=RUNX1,literal", "RUNX1 / uncertain,literal", ",missing gene"])
def test_csv_reference_rejects_invalid_gene_with_row_error(tmp_path, invalid_row):
    source = tmp_path / "reference.csv"
    source.write_text(f"Gen,Note\nTP53,literal\n{invalid_row}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="[Rr]ad 3|[Rr]ow 3"):
        load_who_driver_genes(source)
