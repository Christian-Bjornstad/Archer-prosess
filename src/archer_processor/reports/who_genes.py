from __future__ import annotations

import csv
import re
from pathlib import Path

from openpyxl import load_workbook

WHO_DRIVER_GENES = frozenset(
    {
        "ASXL1", "BCOR", "BCORL1", "BRAF", "BRCC3", "CALR", "CBL", "CEBPA",
        "CREBBP", "CSF1R", "CSF3R", "CTCF", "CUX1", "DNMT3A", "ETV6", "EZH2",
        "GATA2", "GNAS", "GNB1", "IDH1", "IDH2", "JAK2", "JAK3", "KDM6A",
        "KIT", "KMT2A", "KRAS", "MPL", "MYD88", "NOTCH1", "NRAS", "PHF6",
        "PIGA", "PPM1D", "PRPF40B", "PTEN", "PTPN11", "RAD21", "RUNX1",
        "SETBP1", "SF1", "SF3A1", "SF3B1", "SMC1A", "SMC3", "SRSF2", "STAG2",
        "STAT3", "TET2", "TP53", "U2AF1", "U2AF2", "WT1", "ZRSR2",
    }
)


def load_who_driver_genes(path: str | Path | None = None) -> frozenset[str]:
    """Load a local WHO gene list, or use the bundled list when unset."""
    if not path or not str(path).strip():
        return WHO_DRIVER_GENES
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"WHO-drivergenfilen finnes ikke: {source}")
    suffix = source.suffix.casefold()
    if suffix == ".xlsx":
        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            sheet = workbook["WHO"] if "WHO" in workbook.sheetnames else workbook.active
            rows = sheet.iter_rows(values_only=True)
            first = next(rows, ())
            header = [str(value or "").strip().casefold() for value in first]
            if not header:
                raise ValueError(f"WHO-drivergenfilen er tom: {source}")
            column = next(
                (index for index, value in enumerate(header) if value in {"gene", "gen", "symbol", "driver gen", "drivergen"}),
                0,
            )
            values = [] if header[column] in {"gene", "gen", "symbol", "driver gen", "drivergen"} else [first[column]]
            values.extend(row[column] for row in rows if len(row) > column)
        finally:
            workbook.close()
    elif suffix in {".csv", ".txt"}:
        with source.open(encoding="utf-8-sig", newline="") as stream:
            sample = stream.read(2048)
            stream.seek(0)
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
            values = [row[0] for row in csv.reader(stream, delimiter=delimiter) if row]
    else:
        raise ValueError("WHO-drivergenfilen må være .xlsx, .csv eller .txt.")
    genes = frozenset(
        symbol for value in values
        if (symbol := str(value or "").strip().upper())
        and symbol not in {"GENE", "GEN", "SYMBOL", "DRIVER GEN", "DRIVERGEN"}
        and re.fullmatch(r"[A-Z][A-Z0-9-]*", symbol)
    )
    if not genes:
        raise ValueError(f"WHO-drivergenfilen inneholder ingen gyldige gensymboler: {source}")
    return genes

