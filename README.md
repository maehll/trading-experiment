# Trading Agent – Autonomous Trading Experiment

Plattform für ein Experiment mit autonomen KI-Trading-Agenten auf **ausschließlich simuliertem Kapital** (Paper Trading).

## Architekturprinzipien

1. **Autonomie** – Agenten treffen Handels- und Strategieentscheidungen eigenständig.
2. **Lernen (ab Iteration 5)** – Rewards können später einen dokumentierten Lernzyklus speisen.
3. **Sicherheit** – Keine Agentenentscheidung kann Risikogrenzen überschreiben (unabhängige Risk Engine).
4. **Nachvollziehbarkeit** – Entscheidungen, Strategien, Rewards und Ausführungen sind versioniert und append-only.
5. **Messbarkeit** – Adaptive Agenten werden mit eingefrorenen Kontrollvarianten und passivem Benchmark verglichen.

## Lokale Entwicklung

Voraussetzungen: Python 3.12+ und PostgreSQL 16 – wahlweise als Docker-Container
(`docker compose up -d`) **oder** als portable Instanz ohne Admin-Rechte (siehe unten).

### Variante A: Portable PostgreSQL (ohne Docker/Admin)

Binaries (offizielle EDB-ZIP) liegen unter `C:\dev\tools\pgsql`, das Datenverzeichnis
unter `.pgdata/` (nicht versioniert), Port **5433**.

```powershell
.\scripts\db.ps1 start    # starten
.\scripts\db.ps1 status   # prüfen
.\scripts\db.ps1 stop     # stoppen
```

### Variante B: Docker

```powershell
docker compose up -d
```

### Einrichtung

```powershell
# 1. Virtuelle Umgebung + Abhängigkeiten
python -m venv .venv
.venv\Scripts\pip install -e .[dev]

# 2. Konfiguration
copy .env.example .env

# 3. Migrationen anwenden
.venv\Scripts\alembic upgrade head

# 4. Tests (benötigt laufende DB)
.venv\Scripts\pytest
```

## Iteration 2: Paper-Order-Gateway

Das lokale Gateway läuft ausschließlich auf `127.0.0.1`; es gibt keinen Live-Broker-
Adapter und keinen externen Order-Transport. Eine Order wird im Gateway protokolliert,
gegen die administratorseitige `config/risk/risk.yaml` geprüft und entweder von der
lokalen Simulator-Ledger gebucht oder mit Begründung abgelehnt.

Der Gateway akzeptiert nur EUR-Aktien/ETFs mit frischen, als `ok` markierten Preisen
in `market_data` und `meta.instrument_type` gleich `stock` oder `etf`. Damit ist
`market_data` die einzige verbindliche Preisquelle für Prüfung und simulierte
Ausführung; Research-Daten werden nicht zur Preisbildung verwendet. FX, Short-Selling
und Hebelprodukte sind in dieser Iteration gesperrt. Marktfähige Orders füllen sofort;
nicht-marktfähige Limitorders bleiben offen, reservieren ihr Risikobudget und können
storniert werden.

Vor dem Start:

```powershell
# Migration der zusätzlichen Simulator-Kontotabelle und Positionssperre
.\.venv\Scripts\alembic upgrade head

# Gateway-Schlüssel erzeugen und nur lokal im Prozess setzen (min. 32 Zeichen)
$env:TRADING_GATEWAY_API_KEY = [guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()
$env:TRADING_AGENT_GATEWAY_API_KEY = [guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()
.\.venv\Scripts\python.exe -m services.order_gateway.main
```

Order-Einreichung erfolgt über `POST /orders` mit `X-API-Key`. `client_order_id` ist
idempotent: identische Wiederholungen liefern denselben Orderstatus, abweichende
Requests mit derselben ID werden abgelehnt. `POST /agents/{agent_id}/kill-switch`
aktiviert einen persistenten Stopp; es gibt absichtlich keinen API-Endpunkt zum
Entsperren.

Jede Order, auch eine administrative, benötigt `agent_run_id` und `decision_id` einer
gespeicherten BUY-/SELL-Entscheidung eines aktiven Agenten und laufenden Runs. Pro
Entscheidung ist höchstens eine Order zulässig. Der Agent-Schlüssel gilt nur für
Agent A; Kill-Switch, Stornierung und Matching benötigen den Admin-Schlüssel.
Damit entfallen frühere manuelle Order-Requests ohne Entscheidung.

Nach Eingang neuer Kurse muss der Betreiber das deterministische Matching aufrufen
(kein Feed oder Scheduler ist eingebaut):

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/admin/agents/agent-a/orders/match" `
  -Method Post `
  -Headers @{ "X-API-Key" = $env:TRADING_GATEWAY_API_KEY }
```

Es verarbeitet offene Limits in zeitlicher Reihenfolge unter Kontosperre, prüft frische,
neuere Marktdaten und Risikolimits erneut und lässt nicht ausführbare Orders offen.
Abgelaufene DAY-Orders werden beim Matching nach UTC-Kalendertag storniert.
Der Simulator erzeugt aktuell nur vollständige Fills; vorhandene Teilfüllungen werden
bei Matching und Stornierung berücksichtigt, ihre ursprüngliche Cash-Reservierung bleibt
bis zum Abschluss konservativ bestehen. Aufrufe sind wiederholbar; kein Matching und
keine automatische Stornierung findet statt, solange der Betreiber es nicht ausführt.

Vor neuer Order oder Matching gleicht das Gateway Cash, Positionshistorie einschließlich
Einstand und realisierter P&L, Order-/Fillstatus, offene Reservierungen und den letzten
seit der letzten Ausführung erstellten Snapshot einschließlich unrealisierter P&L ab.
Ein Widerspruch aktiviert den Kill-Switch und blockiert die Ausführung. Historische
Marktdaten ohne eindeutige ISIN oder Börse sind nicht automatisch eindeutig: Bei
widersprüchlichen bekannten Identitäten desselben Symbols wird blockiert, fehlende
Identifikatoren bleiben ein offenes Risiko.

Vor dem ersten Agentenlauf richtet ein Administrator Agent A und dessen Simulator-Konto
einmalig ein:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/admin/agents/agent-a/initialize" `
  -Method Post `
  -Headers @{ "X-API-Key" = $env:TRADING_GATEWAY_API_KEY }
```

## Phase 3: Portfolio-Snapshots und Rewards

Gefüllte Paper-Orders erzeugen atomar mit der Ausführung einen EUR-Portfolio-Snapshot;
der erste Snapshot hält das Startkapital vor dem ersten Orderversuch fest. Zusätzliche
Bewertungen (zum Beispiel einmal täglich) lassen sich manuell ausführen:

```powershell
.\.venv\Scripts\python.exe -m services.portfolio.cli snapshot-all
```

Der passive Vergleich wird einmalig aus einem EUR-ETF-Kurs in `market_data` initialisiert.
Der Kurs muss als `ok` markiert sein und `meta.instrument_type: etf` enthalten:

```powershell
.\.venv\Scripts\python.exe -m services.portfolio.cli init-benchmark --symbol <ETF-SYMBOL>
```

Snapshots und Benchmark verwenden ausschließlich zeitgerechte, valide Einträge aus
`market_data`; fehlende, veraltete oder nicht verfügbare Kurse brechen die Bewertung
explizit ab. Ein Datenfeed und eine Zeitplanung sind nicht Teil dieser Phase und müssen
vom Betreiber bereitgestellt werden.

Rewards können nach Vorliegen der Agenten- und Benchmark-Snapshots sowie abgeschlossener
Agent-Runs für einen Zeitraum berechnet werden:

```powershell
.\.venv\Scripts\python.exe -m services.reward_engine.cli `
  --agent-id <AGENT-ID> `
  --period-start 2026-01-01T00:00:00Z `
  --period-end 2026-01-02T00:00:00Z
```

Die versionierte Formel und Gewichte stehen in `config/reward.yaml`. Modellkosten werden
nur bei vollständigen Runs berücksichtigt und über einen zeitgerechten `EURUSD`-Eintrag
mit der Konvention `USD_per_EUR` in EUR umgerechnet. Pro Agent, Zeitraum und Reward-Version
ist das Ergebnis idempotent und unveränderlich.

Standard ist v2: `100 * net_return - 0.5 * max_period_drawdown_pct - 0.05 * turnover_pct`.
Die Nettorendite ist ein Dezimalwert; Drawdown und Umsatz sind Prozentpunkte.
Die Benchmarkrendite wird nur zum Vergleich gespeichert. Der Perioden-Drawdown ist
der größte Rückgang gegenüber einem ab dem Start-Snapshot beobachteten Höchstwert;
er ist nur so vollständig wie die vorhandenen Snapshots. Die dafür verwendeten
Snapshots werden im Audit gespeichert. Das bestehende Feld `drawdown_increase_pct`
enthält bei v2 diesen Perioden-Drawdown. Gebühren, Spread und Slippage sind bereits
im Portfoliowert enthalten; lediglich die nicht dort verbuchten Modellkosten werden
einmal abgezogen. Für historische Neuberechnungen bleibt `version: "v1"` mit der
ursprünglichen Formel und den damaligen Gewichten verfügbar.

Der Paper-Simulator verwendet vorhandene Bid-/Ask-Kurse ohne zusätzlichen Spread;
fehlt die jeweilige Kursseite, wird der konfigurierte Gesamtspread symmetrisch um den
Mittelpreis simuliert. Slippage und Gebühren sind separat konfiguriert. Limitorders
werden nur ausgeführt, wenn der simulierte Preis einschließlich Slippage das Limit einhält.

Jeder Reward enthält die Formelversion, Snapshot-IDs und -Zeitpunkte, Portfolio- und
Benchmarkwerte sowie die in den Snapshots dokumentierten Marktdaten-IDs und -Zeitpunkte.
Die Datenbank erzwingt diese Audit-Felder. Agent A hat keine Datenbankzugangsdaten.
Nur die separate Data API erhält eine eingeschränkte Leserolle:

```powershell
psql -h 127.0.0.1 -p 5433 -U trading -d trading_experiment `
  -v ON_ERROR_STOP=1 -f scripts\provision_agent_readonly.sql
```

Die Rolle `trading_data_reader` ist eine `NOLOGIN`-Gruppenrolle. Ein Administrator muss
eine separate LOGIN-Rolle für den Data-API-Prozess anlegen, ihr ausschließlich
`trading_data_reader` zuweisen und das Passwort separat in dessen Prozessumgebung
injizieren. Beispiel (Passwort nur interaktiv bzw. außerhalb des Repositories setzen):

```sql
CREATE ROLE trading_data_api LOGIN PASSWORD '<separates Passwort>';
GRANT trading_data_reader TO trading_data_api;
```

Der Agent-Prozess darf dieses Passwort und Datenbankzugriff nicht erhalten; diese
Trennung muss beim Deployment erzwungen werden.
Die API liefert gefilterte Kurse, Research- und Portfolioinformationen sowie nur
lesbare Reward-Ergebnisse; sie besitzt keine Schreib- oder Order-Routen.

## Iteration 4/5: Agenten- und Lern-Grenzen

Iteration 4 protokolliert autonome Entscheidungen, führt BUY/SELL-Vorschläge nur über
das Order Gateway aus und lässt HOLD/NO_TRADE ohne Order. Es gibt keine automatischen
Strategieänderungen, Reward-Manipulationen oder Änderungen der Risikokonfiguration.
Der Lernzyklus beginnt frühestens in Iteration 5 nach gesonderter Freigabe. Das Gateway
verwendet einen eigenen Admin-Schlüssel und einen davon verschiedenen Agent-Schlüssel;
der Agent-Schlüssel kann weder Kill-Switch noch Orderstornierungen aufrufen.

Für den lokalen Start sind drei getrennte Prozesse erforderlich: Order Gateway mit
Gateway-DB-Zugang und Risikokonfiguration, Data API mit ausschließlich
`trading_data_reader` und Agent A ohne DB-Zugang. Agent- und Data-API-Geheimnisse
werden nicht in die gemeinsame `.env` eingetragen. Beide Dienste binden ausschließlich
an Loopback. Das allein isoliert Prozesse desselben Benutzers nicht. Für Ubuntu sind
getrennte Dienstidentitäten, Dateirechte und Netzwerkregeln erforderlich; der Agent darf
weder Risikodateien noch Gateway-/DB-Administratorgeheimnisse lesen oder verändern.

Nach Ausführung des Rollenskripts erstellt ein Administrator die LOGIN-Rolle des
Data-API-Prozesses, z. B. `trading_data_api`, und gewährt ihr ausschließlich
Mitgliedschaft in `trading_data_reader`. Das Passwort kommt nur in die Prozessumgebung
dieses Dienstes. Data API und Agent starten in getrennten PowerShell-Prozessen:

```powershell
# PowerShell-Fenster 1: Data API
$env:TRADING_DATA_DB_USER = "trading_data_api"
$env:TRADING_DATA_DB_PASSWORD = "<Data-API-Datenbankpasswort>"
$env:TRADING_DATA_API_KEY = "<separater Data-API-Schlüssel mit mindestens 32 Zeichen>"
.\.venv\Scripts\python.exe -m services.data_api.main
```

```powershell
# PowerShell-Fenster 2: Agent A, nur für lokale Entwicklung
$env:TRADING_AGENT_OPENROUTER_API_KEY = "<OpenRouter-Key>"
$env:TRADING_AGENT_DATA_API_KEY = "<Data-API-Schlüssel>"
$env:TRADING_AGENT_GATEWAY_API_KEY = "<Agent-Gateway-Schlüssel>"
.\.venv\Scripts\python.exe -m services.agent_runtime.main
```

Der OpenRouter-Key muss ein monatliches Budget mit Reset `monthly` und einem USD-Limit
innerhalb des aktuellen Gegenwerts von 20 EUR besitzen. Der Adapter verlangt zusätzlich
10 % Währungspuffer und bricht vor dem Modellaufruf ab, wenn Budgetdaten fehlen oder
das Limit überschritten wird. Die laufende Data API liefert nur aktuelle EUR-Aktien-
und ETF-Kurse, aktuelle Research-Daten, das Paper-Portfolio und lesbare Reward-Ergebnisse;
die freie Instrumentenauswahl erfolgt aus dem jeweils verfügbaren Kursuniversum.

## Security- und Betriebsgrenzen (Iteration 4.1)

- Im Code gibt es ausschließlich den Paper-Simulator; Agent A nutzt HTTP statt DB-
  oder Simulatorimports. Schlüsselrollen trennen Agent-Routen von Admin-Routen;
  DB-Transaktionen, Konto-/Run-Sperren, Entscheidungsbindung und Idempotenz verhindern
  konkurrierende Grenzüberschreitungen bzw. doppelte Orders im Gateway. Fehler rollen
  die Transaktion zurück; nach einem Netzwerk-Timeout muss der Orderstatus mit derselben
  `client_order_id` geprüft werden, bevor ein neuer Versuch erfolgt.
- PostgreSQL-Trigger schützen Rewards, Entscheidungen, Fills, Snapshots und Risk-Events
  vor UPDATE/DELETE durch gewöhnliche Rollen. Eigentümer/Superuser können Trigger,
  Rollen und Daten ändern; unveränderbare externe Backups, Monitoring und Rechteprüfung
  bleiben Betreiberaufgaben. `a41e0c2d784f` ergänzt nullable Kursreferenzen und
  Kostenannahmen für neue Fills; alte Daten bleiben erhalten und haben dort NULL.
  Ein Downgrade dieser Migration ist wegen Auditdatenverlust absichtlich gesperrt.
- Die Codebasis kann nicht beweisen, dass Agent A keine direkten Netzwerk-, Broker-,
  Dateisystem- oder administrativen DB-Rechte besitzt. Ubuntu-Deployment muss eigene
  Unix-Benutzer, minimale DB-LOGIN-Rollen ohne Eigentümer-/Adminrechte, Dateirechte,
  getrennte Secrets, Loopback-/Firewall- und ausgehende Netzwerkregeln durchsetzen und
  testen. Loopback und unterschiedliche API-Schlüssel allein reichen nicht.
- Quellen werden bei Entscheidungen auf Agentenzuordnung, Kontext und Zeit geprüft;
  der vom Agenten eingereichte Kontext ist aber nicht kryptografisch an einen Data-API-
  Abruf gebunden. Research und Marktdaten selbst sind nicht append-only und können
  Prompt-Injection oder unbekannte Geheimnisse enthalten. Bekannte Prozessschlüssel
  werden vor einem Modellaufruf bzw. vor Weitergabe der Modellantwort erkannt, aber
  eine allgemeine Secret-Erkennung in beliebigen Quellen, Logs und API-Antworten ist
  nicht garantiert. Produktive Datenquellen, Logfilter und Egress erfordern Prüfung.
- Nicht getestet sind Ubuntu-Isolation, echte Provider-/Netzwerkausfälle, Live-Broker,
  hohe Last, komplexe historische Instrumentenwechsel und tatsächliche Teilfill-Erzeugung.
  Der Perioden-Drawdown ist auf vorhandene Snapshots beschränkt; ohne zuverlässige
  Datenerfassung und regelmäßiges Matching sind Handels- und Rewardwerte unvollständig.
  Iteration 5 sollte erst nach Klärung dieser Risiken und separater Freigabe beginnen.

## Struktur

| Pfad | Inhalt |
|---|---|
| `src/trading_core/` | Gemeinsame Bibliothek: Datenmodelle, DB, Settings |
| `migrations/` | Alembic-Migrationen (inkl. Append-only-Trigger) |
| `services/order_gateway/` | Lokales FastAPI-Gateway, unabhängige Risikoprüfung, Paper-Simulator |
| `services/portfolio/` | Portfolio-Bewertung, Snapshots und passiver Benchmark |
| `services/reward_engine/` | Versionierte Reward-Berechnung |
| `scripts/provision_agent_readonly.sql` | Eingeschränkte PostgreSQL-Leserolle für die Data API |
| `config/agents/` | Beschreibende Agentenkonfiguration (keine Sicherheitslimits) |
| `config/risk/` | Verbindliche Risikolimits – nur administrativ änderbar |
| `tests/` | Testsuite (benötigt laufende PostgreSQL-Instanz) |

## Sicherheitsmodell (Datenbank)

Append-only-Tabellen (UPDATE/DELETE per Trigger blockiert): `decisions`, `executions`,
`rewards`, `portfolio_snapshots`, `risk_events`, `learning_events`, `experiment_configs`
und `benchmark_portfolios`. `strategies` erlaubt nur Lifecycle-Übergänge (`status`,
`activated_at`, `retired_at`); alle inhaltlichen Felder sind nach dem Anlegen unveränderlich.
