---
name: statistik-bs
description: >
  Query the statistics portal of the canton of Basel-Stadt (statistik.bs.ch).
  Use to find statistical indicators (Indikatoren) about Basel-Stadt — population,
  labour, housing, environment, politics, health — read their explanations and
  sources, load their time series, and read the web tables (Webtabellen) of the
  statistical yearbook via plain HTTP. No MCP server needed.
---

# Statistik Basel-Stadt (statistik.bs.ch)

Public JSON endpoints of the Statistisches Amt Basel-Stadt. No auth, no SDK.

## Base URL

```
https://statistik.bs.ch
```

## 1. Find indicators

`GET /indikatoren/metadata/portal/indikatoren.json`

A JSON array (~2 MB, may start with a UTF-8 BOM) with the full metadata of every
indicator shown in the Indikatorenportal. Download it once and search locally in
`title`, `subtitle`, `thema`, `unterthema`, `kennzahlenset`, `lesehilfe`.

```bash
curl -s https://statistik.bs.ch/indikatoren/metadata/portal/indikatoren.json \
  | sed '1s/^\xEF\xBB\xBF//' \
  | jq '[.[] | select(.title | test("Arbeitslos"; "i")) | {id, title, subtitle}]'
```

Useful fields: `id`, `title`, `subtitle`, `thema` (e.g. `"03 Arbeit, Erwerb"`),
`unterthema`, `kennzahlenset`, `raeumlicheGliederung` (Kanton, Gemeinde,
Wohnviertel, ...), `darstellungsart` (Diagramm/Karte), `aktualisierungsdatum`,
`data-id`.

## 2. Indicator metadata

`GET /api/indikator-meta/{id}`

```bash
curl https://statistik.bs.ch/api/indikator-meta/10027
```

Read `lesehilfe` (plain-language reading aid), `erlaeuterungen` (method notes,
may contain HTML) and `quellenangabe` (sources, always cite them).

## 3. Indicator data

`GET /api/indikator-data/{data-id}` → `{ "headers": [...], "rows": [[...], ...] }`

**Use `data-id` from the metadata, not `id`.** Several indicators share one data
file; for those the ids differ. All cells are strings; empty string means no value.

```bash
curl https://statistik.bs.ch/api/indikator-data/10027
```

If the metadata has a non-empty `filter`, the portal chart only shows a subset of
that shared table.

## 4. Link the user to the portal

`https://statistik.bs.ch/indikatorenportal/{id}` — interactive chart, map, table.

## 5. Web tables (Webtabellen)

~220 Excel tables of the statistical yearbook, updated continuously. File names
follow the table code: `t06.1.05` → `t06-1-05.xlsx`.

`GET /api/webtabelle-data/{file}.xlsx?sheet={n}` → flat data, use this one

```bash
curl https://statistik.bs.ch/api/webtabelle-data/t06-1-05.xlsx
curl "https://statistik.bs.ch/api/webtabelle-data/t10-1-04.xlsx?sheet=0"
```

The response has `sheets` (`index`, `name`, `kind`: `steckbrief` or `table`), the
name of the returned `sheet`, `code`, `title`, `headers`, `rows` and `notes`.
Without `sheet` the first data sheet is returned; the Steckbrief (source,
reference period, last and next update, contact) is usually `sheet=0` and comes
as `["Merkmal", "Wert"]` rows.

In table sheets, multi-row headers are already joined into column names
(`"Arbeitslose – Anzahl"`), group rows ("Total", "3-Sterne") are the first column
`Gruppe`, and numbers are raw values (percent formats as fractions:
`"12.3%"` → `0.123`). Placeholders like `…` stay text. Time series usually start
with the oldest year, so the latest values are in the last rows.

Other formats of the same sheet:

- Formatted cells (`runs`, `colspan`/`rowspan`, as rendered on the portal):
  `/api/webtabelle/{file}.xlsx?sheet={n}`
- CSV (semicolon separated, for Excel): `/api/webtabelle/{file}.xlsx?sheet={n}&format=csv`
- Excel download: `https://statistik.bs.ch/files/webtabellen/{file}.xlsx`

## Suggested workflow

1. Search the index for the topic (German terms).
2. Read metadata of the top hit(s): `lesehilfe`, `erlaeuterungen`, sources.
3. Load data via `data-id`, answer with numbers, cite `quellenangabe`, link the portal page.
4. For yearbook tables, load the web table, pick the sheet (year, month, commune),
   and cite the Steckbrief (`Datenquelle`, `Letzte Aktualisierung`).
5. For raw datasets beyond indicators, use the OGD portal https://data.bs.ch.
