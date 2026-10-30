# Synthetic test fixtures

`sample_variants.tsv` is a five-row regression fixture with synthetic sample
identifiers `26OUM00001` through `26OUM00005`. These sequential identifiers do
not identify patients. They retain the `YYOUM#####` shape so the PDF report
tests also exercise the supported DIT format. Reference variant annotations,
counts, and allele fractions are fixed regression inputs for filtering and
report layout; they must not be treated as clinical results.

`archer_v7_layout.json` contains only the expected export column names and
layout settings; it contains no sample rows.

Keep local clinical exports outside this directory. Only these reviewed
fixtures belong in the source distribution.
