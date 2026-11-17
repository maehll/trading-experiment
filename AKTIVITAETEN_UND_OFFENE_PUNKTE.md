# Trading Agent – Aktivitäten und offene Punkte

**Stand: 16.11.2026**  
**Arbeitsstand:** Iteration 4 implementiert und lokal getestet; der isolierte
Agentenbetrieb ist noch nicht eingerichtet.

## Kurzüberblick

Die bisherigen Iterationen bilden eine Paper-Trading-Plattform mit
Risikoprüfung, Portfolio-Bewertung und versionierten Rewards. Iteration 4 ergänzt
Agent A mit OpenRouter-Entscheidungen und einer getrennten, eingeschränkten
Data API. Der Agent hat keine direkten Datenbankzugangsdaten und kann Orders
ausschließlich über das Order Gateway einreichen.

Der Code wurde lokal getestet. Ein echter OpenRouter-Aufruf und ein Betrieb
unter einer separaten Windows-Prozessidentität wurden noch nicht durchgeführt.
Es gibt keinen Live-Broker-Adapter.

## Bisherige Aktivitäten

### Iterationen 1–3: Plattform, Gateway und Bewertung

- Python-/PostgreSQL-Grundlage mit versioniertem Schema und Agenten-, Run-,
  Entscheidungs-, Markt- und Ausführungsdaten.
- Paper-Order-Gateway mit unabhängiger Risk Engine, idempotenten Orders,
  Simulator-Ledger und persistentem Kill-Switch. Preise für Prüfung und
  Ausführung kommen ausschließlich aus `market_data`.
- Portfolio-Snapshots bei Ausführungen und separat auslösbare Bewertungen;
  passiver Benchmark sowie versionierte Reward-Berechnung.
- Rewards enthalten Auditdaten, unter anderem Formelversion, Snapshot-IDs und
  Zeitpunkte, Portfolio-/Benchmarkwerte und zugrunde liegende Marktdaten.
  Datenbankregeln schützen die Bewertungsdaten vor nachträglicher Änderung.

### Iteration 4: autonomer Agent A

- OpenRouter-Adapter mit strikt strukturiertem JSON, austauschbarem Modell,
  Request-ID-, Token- und Kostenprotokollierung. Vor einem Modellaufruf wird ein
  monatliches Providerbudget geprüft; die Obergrenze ist auf 20 EUR begrenzt
  und berücksichtigt einen 10-%-Währungspuffer.
- Separate HTTP-only Data API für aktuelle EUR-Aktien-/ETF-Kurse, Research,
  Portfolioinformationen und lesbare Reward-Ergebnisse. Sie wählt dynamisch
  aus den jüngsten verfügbaren Instrumenten; es gibt keine feste Symbol-Whitelist.
- Decision Engine für BUY, SELL, HOLD und NO_TRADE. Entscheidung, Begründung,
  Horizont, Konfidenz, Quellen-IDs und verwendeter Datenkontext werden beim
  Gateway dokumentiert.
- BUY-/SELL-Aufträge müssen auf eine gespeicherte Entscheidung und einen
  laufenden Agent-Run verweisen. Die Gateway-Risikoprüfung bleibt unabhängig.
  HOLD/NO_TRADE lösen keine Order aus.
- Ungültige Modellantworten, nicht verfügbare Daten, Budgetüberschreitungen
  und Servicefehler führen zu keiner neuen Order. Kosten unbekannter,
  fehlgeschlagener Provideraufrufe werden nicht fälschlich als null verbucht.
- Admin- und Agent-Gateway-Schlüssel sind getrennt. Der Agent-Schlüssel kann
  keine Admin-Routen wie Kill-Switch oder Orderstornierung aufrufen.
- Migration `c414a6b27e01` ergänzt die OpenRouter-Request-ID beim Agent-Run.
- `scripts/provision_agent_readonly.sql` wurde auf der lokalen Datenbank
  ausgeführt. Die alte direkte Agent-Leserolle wurde entprivilegiert;
  `trading_data_reader` ist eine eingeschränkte `NOLOGIN`-Rolle für den
  Data-Service. Die getesteten Reads funktionieren; Schreib-, Snapshot- und
  Benchmark-Lesezugriffe sind für diese Rolle nicht freigegeben.

### Validierung

- **41 Tests bestanden**, darunter ein Gateway-/Data-API-End-to-End-Test mit
  simuliertem LLM für BUY, SELL, HOLD und NO_TRADE sowie Tests für Fehler- und
  Budgetgrenzen.
- Alembic meldet keinen Schema-Drift; Datenbankstand: `c414a6b27e01`.
- Python-Kompilierung und `git diff --check` waren erfolgreich.
- Die bestehende Starlette-/httpx-Deprecation-Warnung bleibt bestehen und ist
  kein Iteration-4-Blocker.

## Noch ausstehende Punkte

### Vor dem ersten Agentenstart

- [ ] Eine eigene PostgreSQL-LOGIN-Rolle für den Data-API-Prozess anlegen
  (z. B. `trading_data_api`) und ausschließlich `trading_data_reader`
  zuweisen. Ein Passwort wurde noch nicht eingerichtet.
- [ ] Eine separate Windows-Prozessidentität für Agent A einrichten.
  Sie darf weder `config/risk/` noch Gateway-Admin-Geheimnisse oder die
  Zugangsdaten des Data-Service lesen. Nur getrennte Umgebungsvariablen bei
  gleicher Windows-Identität sind keine ausreichende Isolation.
- [ ] Separate, zufällige Schlüssel erzeugen und nur den jeweiligen Prozessen
  injizieren: Gateway-Admin, Gateway-Agent und Data API. Keine Schlüssel in
  die gemeinsame `.env` oder ins Repository eintragen.
- [ ] Einen OpenRouter-API-Key mit monatlichem Budget-Reset bereitstellen.
  Das Providerlimit muss zum aktuellen USD/EUR-Kurs innerhalb des 20-EUR-Limits
  inklusive Sicherheitsabschlag liegen.
- [ ] Einen Datenfeed für aktuelle EUR-Aktien-/ETF-Kurse und einen zeitgerechten
  EURUSD-Kurs mit Konvention `USD_per_EUR` bereitstellen. Research-Daten müssen
  ebenfalls eingespeist werden. Ohne diese Daten kann der Agent nicht sinnvoll
  entscheiden oder Orders ausführen.
- [ ] Agent A und sein Simulator-Konto über die Admin-Route initialisieren;
  anschließend Data API, Gateway und Agent unter den vorgesehenen Identitäten
  starten und Healthchecks prüfen. Start- und Konfigurationshinweise stehen in
  [README.md](README.md).

### Noch nicht mit echten Diensten verifiziert

- [ ] Einen echten OpenRouter-Aufruf mit dem vorgesehenen API-Key und Modell
  durchführen und Providerbudget, Structured Output sowie tatsächliche
  Nutzungsdaten verifizieren. Bisher wurden hierfür Testantworten verwendet.
- [ ] Den vollständigen Ablauf mit der separat angemeldeten Data API und realen
  Marktdaten als Paper-Order durchspielen. Die automatisierten E2E-Tests
  verwenden PostgreSQL-Testdaten und ein Fake-LLM.
- [ ] Prüfen, dass ein fehlender oder veralteter Markt-/FX-Kurs im lokalen
  Datenfeed sicher zu NO_TRADE bzw. keiner Order führt.
- [ ] Falls wiederkehrender autonomer Betrieb gewünscht ist: Auslöser und
  Zeitplan festlegen. Der aktuelle Agent-Entrypoint führt einen einzelnen
  manuellen Run aus; ein Scheduler und automatischer Datenfeed sind nicht
  eingerichtet.
- [ ] Vor einem Commit die vollständigen Änderungen im Worktree prüfen.
  Der aktuelle Stand enthält uncommittete Änderungen aus mehreren Iterationen.

## Bewusst zurückgestellt

- **Iteration 5 (Lernzyklus):** keine automatische Strategieänderung,
  Reward-basierte Anpassung oder Änderung der Risikokonfiguration in Iteration 4.
  Dafür ist eine gesonderte Freigabe und Implementierung erforderlich.
- **Technische Warnung:** Starlette-/httpx-Deprecation separat bereinigen; sie
  blockiert die aktuelle Testsuite nicht.

## Relevante Dateien

- [README.md](README.md) – lokale Einrichtung und Sicherheitsgrenzen
- [services/agent_runtime/](services/agent_runtime/) – OpenRouter-Adapter und
  Agenten-Orchestrierung
- [services/data_api/](services/data_api/) – gefilterter, read-only Datenzugriff
- [services/order_gateway/](services/order_gateway/) – Order-, Run- und
  Entscheidungsgrenze
- [scripts/provision_agent_readonly.sql](scripts/provision_agent_readonly.sql) –
  Data-Service-Datenbankrolle
- [tests/test_agent_runtime.py](tests/test_agent_runtime.py) – Iteration-4-
  Integrations- und Fehlerfalltests
