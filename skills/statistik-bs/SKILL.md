---
name: statistik-bs
description: >
  Query the statistics portal of the canton of Basel-Stadt (statistik.bs.ch).
  Use to find statistical indicators (Indikatoren) about Basel-Stadt — population,
  labour, housing, environment, politics, health — read their explanations and
  sources, and load their time series via plain HTTP. No MCP server needed.
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

## Suggested workflow

1. Search the index for the topic (German terms).
2. Read metadata of the top hit(s): `lesehilfe`, `erlaeuterungen`, sources.
3. Load data via `data-id`, answer with numbers, cite `quellenangabe`, link the portal page.
4. For raw datasets beyond indicators, use the OGD portal https://data.bs.ch.
