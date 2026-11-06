# Task: make the stock tool correct

`src/` reads a CSV of stock movements, applies them to a stock table and reports low stock. The visible test suite in
`tests/` fails. Find and fix the bugs in `src/` (they are spread over more than one file) until `run_tests` passes.
Do not change anything under `tests/`. The visible tests do not check every rule below; follow all of them.

## Rules
1. CSV (`parseCsv` in `src/csv.mjs`): the first line is a header; fields are separated by `,`; a field may be quoted
   with `"`, and a quoted field may contain commas and doubled quotes (`""` means one `"`). Blank lines are skipped,
   and `\r\n` line endings are accepted. Returns an array of objects keyed by the header names, values as strings,
   with surrounding spaces trimmed from unquoted values only.
2. Movements (`applyMovements` in `src/stock.mjs`): each row has `sku`, `kind` (`in` or `out`) and `qty` (a positive
   integer written in digits). `in` adds, `out` subtracts. An `out` that would make the quantity negative throws an
   `Error` whose message contains the sku. An unknown `kind`, or a `qty` that is not a positive integer, throws an
   `Error` whose message contains the row number (1-based, header excluded). A sku not yet in the table starts at 0.
   The input table is not modified: return a new `Map`.
3. Report (`lowStock` in `src/report.mjs`): returns the skus whose quantity is at or below `threshold`, ordered by
   quantity ascending, then sku ascending.
