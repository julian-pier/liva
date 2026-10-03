# MCP-Live-Smoke-Tests sicher ausführen

Öffentliche Smoke-Tests verwenden ausschließlich den signierten, kurzlebigen OAuth-Testclient, der serverseitig über `LIVA_MCP_SMOKE_TEST_CLIENT_ID` konfiguriert wird. Ein Token dieses Clients trägt den signierten Claim `liva_smoke_test=true`. Der Server akzeptiert in diesem Modus nur `dry_run=true`; ein Live-Write wird vor Validierung und Service-Aufruf abgewiesen. Normale Clients sind davon nicht betroffen. Scope- und Schema-Prüfung bleiben aktiv.

Der sichere Testaufruf erzeugt ein echtes Nullbyte im JSON, ohne Shell-Escaping zu verwenden:

```bash
PAYLOAD=$(python -c 'import json; print(json.dumps({"category":"general","text":"escape-regression-" + "\x00"}))')
```

Für eine JSON-Unicode-Escape-Sequenz, die beim JSON-Parsing als Nullbyte ankommt:

```bash
python -c 'import json; print(json.dumps({"category":"general","text":"escape-regression-\u0000"}))'
```

Die sichtbaren Zeichen `\\u0000` oder `\\0` sind dagegen kein Steuerzeichen und werden als gewöhnlicher Text behandelt. Ein Smoke-Test darf trotzdem nie live schreiben: Für alle öffentlichen Write-Tools ist `dry_run=true` verpflichtend.
