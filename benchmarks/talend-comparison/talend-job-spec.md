# Talend Open Studio — equivalent job specification

The TOS job to run alongside the F-Pulse benchmark for a head-to-head comparison. Build this in TOS Open Studio, run it against the same input data the F-Pulse side uses, record wall-clock + peak RSS, compare.

## Inputs

Three `tFileInputDelimited` components reading the gzipped CSVs from `data/`:

| Component | File | Rows | Columns |
|---|---|---|---|
| `tFileInputDelimited_1` | `data/sales.csv.gz` | 1,000,000 | sale_id, customer_id, product_id, sale_date (string → date), amount (float) |
| `tFileInputDelimited_2` | `data/customers.csv.gz` | 100,000 | customer_id, region, segment, signup_year (int) |
| `tFileInputDelimited_3` | `data/products.csv.gz` | 10,000 | product_id, category, margin_pct (float), list_price (float) |

TOS doesn't natively read `.csv.gz` — either decompress the files first (`gunzip -k data/*.csv.gz`), or insert a `tFileUnarchive` component upstream of each input.

## Pipeline shape

```
tFileInputDelimited_1 (sales)
  ├──> tMap_1 (left join on customer_id) <──── tFileInputDelimited_2 (customers)
                  │
                  └──> tMap_2 (left join on product_id) <──── tFileInputDelimited_3 (products)
                              │
                              └──> tAggregateRow_1 (group by region, category, month)
                                          │
                                          └──> tPivotToColumnsDelimited (pivot category into columns)
                                                      │
                                                      └──> tSortRow (by region asc)
                                                                  │
                                                                  └──> tFileOutputDelimited (out/result.csv)
```

## Component settings

### tMap_1 (sales × customers join)

- **Join model:** Inner Join
- **Lookup:** customers (load once)
- **Join key:** `sales.customer_id == customers.customer_id`
- **Output columns:** sales.*, customers.region, customers.segment

### tMap_2 (× products join)

- **Join model:** Inner Join
- **Lookup:** products (load once)
- **Join key:** `intermediate.product_id == products.product_id`
- **Output columns:** intermediate.*, products.category, products.margin_pct

### tAggregateRow_1

- **Group by columns:** region, category, sale_year_month
- **Operations:**
  - sum of `amount` → `total_amount`
  - avg of `margin_pct` → `avg_margin`
  - count(*) → `tx_count`

For the `sale_year_month` field, derive it from `sale_date` in a `tMap` above the aggregate (`TalendDate.formatDate("yyyy-MM", sale_date)`).

### tPivotToColumnsDelimited

- **Pivot column:** category
- **Value column:** total_amount

### tSortRow

- **Sort by:** region (ascending)

## Output

`out/result.csv` — ~1,000 rows with shape `region, year_month, Books, Electronics, Apparel, ...` (one column per pivoted category, plus the two grouping columns).

## Running it

In TOS Studio: right-click the job → Run Job → tick "Statistics" and "Trace". The execution log gives you total wall-clock. For peak RSS, watch the Java process in Task Manager (Windows) or `top -p $(pgrep -f Talend)` (Linux/Mac) while the job runs.

> **JVM heap tuning:** TOS defaults to a small heap that will OOM on the 100K customer lookup if your machine has > 8 GB RAM. Edit the run profile → JVM options → `-Xmx4g` to give it room. (F-Pulse doesn't need this tuning — DuckDB streams.)

## Expected pain points

When running this on TOS, you'll likely hit:

- **JVM startup:** 8–15 seconds before any rows move. F-Pulse is sub-second.
- **tMap heap pressure:** the 100K customer lookup loads to RAM up front. At 1M sales rows the JVM heap will spike to 2–4 GB just for the join state.
- **Pivot materialisation:** `tPivotToColumnsDelimited` doesn't stream — it buffers the entire aggregated result before writing. With 10K aggregated groups this is fine; with 1M+ it becomes the bottleneck.
- **Sort spill:** if you have a small heap (`-Xmx512m`), the sort step will spill to disk. F-Pulse's DuckDB sort spills natively.

These pain points are exactly what the F-Pulse side avoids by being column-vectorised and using DuckDB's out-of-core execution.

## Comparing fairly

For an honest benchmark:

- Same hardware (don't compare a F-Pulse on a fresh SSD with a TOS on a network drive).
- Same input files (decompressed for TOS, gzipped for F-Pulse — F-Pulse reads `.csv.gz` natively).
- Run each side **three times**, drop the slowest (warmup variance), report the median.
- Verify the outputs are equivalent — same row count, same totals per group, same sort order. If the answers don't match, the comparison is meaningless.

## Sharing your numbers

Got it running on your machine? We'd love to see your results — open a [benchmark-contribution issue](https://github.com/hybridyn/fpulse/issues/new/choose) with your hardware + the two wall-clock numbers. The more data points across machines and shapes, the more honest the comparison page on the docs site becomes.
