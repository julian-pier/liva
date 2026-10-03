# Frontend-Stabilitätsstrategie

Stand: 2026-07-27

## Zielbild

LIVA bekommt eine kleine, verbindliche Layout- und Komponentenbasis. Seiten
sollen aus denselben Abständen, Containern, Karten und responsiven Regeln gebaut
werden. Einzelne Screens dürfen fachlich verschieden sein, aber nicht jeweils ihr
eigenes Spacing- und Breakpoint-System erfinden.

Die Umstellung erfolgt inkrementell. Bestehende Nutzerflüsse bleiben während der
Migration funktionsfähig; neue globale Overrides werden nicht mehr ergänzt.

## Gemessener Ausgangspunkt

- `static/css/style.css`: 223 KB, 9.807 Zeilen, 1.937 Pixelwerte,
  782 `!important`-Deklarationen und 91 Media Queries.
- `static/css/dashboard_new.css`: 220 KB, 8.731 Zeilen, 1.766 Pixelwerte,
  213 `!important`-Deklarationen und 53 Media Queries.
- Beide Dateien enthalten zusammen 64 unterschiedliche Media-Query-Ausdrücke.
  Viele Bereiche überlappen sich bei 640/641, 700/701, 767/768, 899/900/901,
  1023/1024/1025, 1199/1200 und 1366/1367 Pixeln.
- `dashboard_vnext.js`: 10.899 Zeilen und 367 benannte Funktionen in einer Datei;
  darin 84 direkte `innerHTML`-Schreibvorgänge, 97 DOM-Queries und 86 einzeln
  registrierte Event Listener.
- Das Dashboard lädt gleichzeitig die globale `style.css` und die große
  `dashboard_new.css`. Mindestens 24 Selektoren sind in beiden Dateien definiert.
- Es existiert keine visuelle Regression-Suite für definierte Viewports.

Diese Zahlen sind keine Qualitätsziele an sich. Sie erklären aber die beobachteten
Symptome: hohe Spezifität, lokale Gegen-Overrides, zufällige Abstände und
Breakpoint-Lücken.

## Verbindliche Architektur

### 1. Eine Token-Quelle

Eine neue `static/css/foundation/tokens.css` wird allein für Designwerte zuständig:

- Spacing-Skala: `--space-1` bis `--space-8`, abgeleitet aus einer kleinen
  rem-basierten Skala.
- Typografie: vier Textgrößen plus zwei Displaygrößen, jeweils mit definierter
  Zeilenhöhe.
- Container: `--container-reading`, `--container-page`, `--container-wide`.
- Karten: ein Radius, ein Innenabstandsbereich, ein Border- und Shadow-Vertrag.
- Z-Index-Stufen und Touch-Zielgröße werden benannt statt als Zufallszahlen
  verteilt.

Pixel bleiben für echte 1px-Borders und bei technisch begründeten Grafiken erlaubt.
Layoutabstände verwenden Tokens, `rem`, `%`, `fr`, `minmax()` und `clamp()`.

### 2. Drei responsive Modi

Es gibt nur drei Layoutmodi:

1. Compact: Standard, mobile-first, bis 40rem.
2. Regular: ab 40rem.
3. Wide: ab 64rem; optional ein reiner Container-Cap ab 80rem.

Komponenten reagieren vorrangig über Grid/Flex und Container Queries. Globale
Viewport-Breakpoints bestimmen nur Seitengerüst und Navigation. Pointer-/Hover-
Queries dürfen Interaktion verbessern, aber niemals ein anderes Inhaltslayout
erzeugen.

### 3. Kleine Layout-Primitiven

Die wiederverwendbare Ebene umfasst nur:

- `.l-page`: zentrierter Seitencontainer mit sicherem horizontalem Gutter.
- `.l-stack`: vertikaler Rhythmus über `gap`.
- `.l-cluster`: umbrechende horizontale Gruppe.
- `.l-grid`: auto-fit Grid mit einer deklarativen Mindestbreite.
- `.c-card`, `.c-section`, `.c-toolbar`, `.c-sheet`: gemeinsame Komponenten.

Margins innerhalb von Feature-Komponenten werden vermieden. Eltern besitzen den
Abstand über `gap`. Dadurch kann ein Kind auf Mobile umbrechen, ohne dass sich
mehrere Außenabstände addieren.

### 4. Kontrollierte CSS-Kaskade

Neue Styles nutzen Cascade Layers in fester Reihenfolge:

```css
@layer reset, tokens, base, layout, components, features, utilities;
```

Feature-Dateien dürfen keine fremden Seiten-IDs selektieren. Neue Selektoren bleiben
bei maximal einer Komponentenklasse plus Zustand. `!important`, verschachtelte
ID-Ketten und neue globale Element-Overrides werden in CI abgewiesen. Eine kleine
temporäre Legacy-Layer darf während der Migration bewusst höher liegen und wird
pro migriertem Bereich verkleinert.

### 5. Dashboard in Feature-Module teilen

`dashboard_vnext.js` wird ohne Framework-Zwang entlang fachlicher Grenzen zerlegt:

- `dashboard/api.js`: Requests, AbortController, Fehler- und Timeout-Vertrag.
- `dashboard/store.js`: genau ein normalisiertes View-State-Objekt.
- `dashboard/bootstrap.js`: Initialdaten und Refresh-Orchestrierung.
- `dashboard/features/today.js`, `training.js`, `nutrition.js`, `calendar.js`,
  `signals.js`, `core_explain.js`: je Feature Rendering und lokale Events.
- `dashboard/dom.js`: sichere DOM-Helfer; Text standardmäßig über `textContent`.

Ein Feature bekommt einen Root-Knoten und darf nur darunter rendern. Globale Events
werden delegiert. API-Payloads werden am Modulrand normalisiert, sodass fehlende
Felder nicht zu individuell gehardcodeten Fallbacks in jeder Render-Funktion führen.

## Stabilitäts-Gates

Vor jeder größeren visuellen Migration werden folgende Tests eingerichtet:

- Screenshot-Baselines für 360×800, 390×844, 768×1024, 1024×768 und 1440×900.
- Zustände: normale Daten, leere Daten, lange Texte, Fehler/Timeout, Modal offen.
- Automatische Prüfungen auf horizontalen Overflow, überlappende Controls,
  abgeschnittene Fokusrahmen und Touch-Ziele unter 44 CSS-Pixeln.
- Ein CSS-Audit mit Budgets für neue `!important`-Deklarationen, neue Breakpoints
  und Selektorspezifität. Anfangs gilt ein „nicht schlechter werden“-Budget; danach
  werden die Bestände schrittweise reduziert.
- Bestehende API-/Payload-Tests bleiben unabhängig von visuellen Tests.

## Reihenfolge der Migration

### Phase 0 – Absichern

Viewport-Screenshots und Overflow-Prüfungen auf dem aktuellen Stand erzeugen. Die
oben genannten CSS-Metriken als reproduzierbaren Audit-Report in CI aufnehmen.

### Phase 1 – Foundation

Tokens, Cascade Layers und Layout-Primitiven ergänzen. `base.html` erhält genau
einen Foundation-Einstieg. Noch kein flächiger visueller Umbau.

### Phase 2 – Dashboard-Shell

Nur Seitencontainer, Top-Grid, Context-Row und Plan-Tools auf `.l-page`, `.l-grid`
und `gap` migrieren. Die Karteninhalte bleiben zunächst unverändert. Damit werden
Symmetrie, Außenränder und Mobile-Reihenfolge stabil, ohne Fachlogik anzufassen.

### Phase 3 – Karten einzeln migrieren

Je Pull Request genau eine Karte inklusive ihrer Zustände. Zuerst Today, Training
und Mealplan, danach Kalender/Signals und zuletzt das große CORE-Erklärungspanel.
Nach jeder Karte werden ihre alten Selektoren aus beiden Legacy-CSS-Dateien entfernt.

Die bestehende Athletica-/Endurance-Karte wird nicht vorschnell nur umgestaltet.
Vorher wird fachlich entschieden, ob sie als eigenständige Karte weiter einen klaren
Job besitzt. Falls nicht, wird sie durch eine kompakte „Ausdauer heute“-Sektion in
Today oder Training ersetzt; Backend-Daten bleiben dabei als Quelle erhalten.

### Phase 4 – JavaScript modularisieren

Featureweise extrahieren, beginnend mit read-only Signals und Today. Jeder Schritt
muss dieselben DOM-Zustände und API-Tests bestehen. Erst nach vollständiger
Extraktion wird `dashboard_vnext.js` entfernt.

### Phase 5 – Legacy abbauen

Unbenutzte Selektoren, doppelte Dashboard-Regeln in `style.css`, alte Breakpoint-
Inseln und die temporäre Legacy-Layer löschen. Ziel ist nicht eine willkürliche
Dateigröße, sondern eine einzige Zuständigkeit pro Regel.

## Entscheidungsregeln für neue UI-Arbeit

- Kein neuer Breakpoint, wenn Grid/Flex/Container Query das Problem lösen kann.
- Kein negativer Margin-Fix für ein Problem des Elternlayouts.
- Kein `!important` außerhalb der ausdrücklich befristeten Legacy-Layer.
- Kein Feature-HTML als riesiger ungetesteter String, wenn wiederholbare DOM-
  Bausteine oder ein Template genügen.
- Keine Karte nur deshalb behalten, weil ein Endpoint existiert; zuerst den
  Nutzerjob definieren, dann Darstellung und Informationsdichte wählen.
- Mobile ist der Ausgangszustand, nicht ein nachträglicher Override des Desktops.
