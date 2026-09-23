# API-Analyse statistik.bs.ch für den MCP-Server

Stand: 2026-09-23. Grundlage: `statistikportal/server/api/*`, `public/llms.txt`,
Live-Abfragen gegen statistik.bs.ch.

## Fazit

**Für eine erste Version muss die API nicht erweitert werden.** Suche, Metadaten und
Daten funktionieren mit den bestehenden Endpunkten. Der MCP-Server lädt den
Indikatoren-Index einmal (≈2 MB, 1 h Cache) und sucht lokal.

Mittelfristig lohnen sich einige Erweiterungen (siehe unten). Dringend ist nur ein
Punkt, der Meilisearch-Key (Sicherheit).

## Vorhandene Endpunkte

| Endpunkt | Inhalt | Nutzung im MCP |
|---|---|---|
| `/indikatoren/metadata/portal/indikatoren.json` | Volle Metadaten aller im Portal sichtbaren Indikatoren (1'077) | `search_indicators`, `get_facets` |
| `/api/indikator-meta/{id}` | Metadaten eines Indikators (2'526 Dateien) | `get_indicator` |
| `/api/indikator-data/{id}` | TSV → `{headers, rows}` (2'504 Dateien) | `get_indicator_data` |
| Meilisearch `product`-Index | Alle Produkte (Artikel, Tabellen, OGD, Karten, …) | `search_portal` (optional) |

## Beobachtungen

1. **`id` ≠ `data-id`.** Bei 96 Indikatoren teilen sich mehrere Indikatoren eine
   Datendatei. `/api/indikator-data/{id}` liefert für 6 davon 404, für die übrigen
   möglicherweise eine andere Tabelle als im Chart. Der MCP löst `data-id` über die
   Metadaten auf. 66 Indikatoren haben zusätzlich einen `filter` (Chart zeigt nur
   einen Teil der Tabelle), der ausserhalb von Highcharts nicht ausgewertet wird.
2. **Index umfasst nur Portal-Indikatoren.** `llms.txt` spricht von „über 2'500
   Indikatoren“, der Index enthält 1'077 (`visibleInPortal`). Die übrigen ~1'450
   (z. B. nur in Indikatorensets sichtbar, etwa 10027) sind nur per ID erreichbar,
   also nicht durchsuchbar.
3. **Alle Werte sind Strings.** Leere Zellen = `""`. Typen/Einheiten fehlen; die
   Einheit steht nur im `subtitle`.
4. **Metadaten enthalten HTML** (`<br>`, `<a href>` in `erlaeuterungen`,
   `externalLinks`). Der MCP bereinigt das.
5. **Keine Validierung der ID** in `server/api/indikator-*/[id].ts` (`join(..., `${id}.tsv`)`).
   Sollte auf `^\d+$` geprüft werden (Pfad-Traversal-Schutz, saubere 400-Fehler).
6. **Meilisearch-Key ist ein Admin-Key.** Der Key in `runtimeConfig.public`
   (`MEILISEARCH_KEY`) ist im Browser-Bundle sichtbar und hat Zugriff auf `/keys` und
   `/indexes/*/settings`. Er sollte ersetzt werden durch einen Key, der nur die
   Aktion `search` erlaubt; der alte sollte widerrufen werden. Der MCP verwendet
   Meilisearch nur, wenn ein Key explizit konfiguriert ist.

## Empfohlene API-Erweiterungen (Priorität)

1. **(Sicherheit, sofort)** Search-only Meilisearch-Key für das Frontend; alten Key
   rotieren.
2. **ID-Validierung** in beiden `server/api`-Routen.
3. **`/api/indikator-data/{id}` löst `data-id` selbst auf** (und optional `filter`),
   damit jeder Client dieselben Daten wie der Chart erhält.
4. **`/api/indikatoren`**: schlanker Index (id, title, subtitle, thema, unterthema,
   kennzahlenset, raeumlicheGliederung, aktualisierungsdatum, visibleInPortal) über
   **alle** Indikatoren, optional mit `?q=`, `?thema=`. Spart die 2 MB und macht
   Set-Indikatoren auffindbar.
5. **`/api/indikatorenset/{slug}`**: Liste der Indikatoren eines Sets inkl.
   Gliederung (`stufe1..5`), da Sets heute nur über das Frontend auflösbar sind.
6. **`/api/search?q=`**: serverseitiger Proxy auf Meilisearch. Dann braucht weder
   das Frontend noch der MCP einen Key.
7. Optional: Einheit/Datentyp pro Spalte in `indikator-data`.

Mit 4–6 würde der MCP deutlich einfacher: kein lokaler Index-Cache, kein Key.
