# LIVA UI

Produktive React-/TypeScript-Oberfläche für Tailwind CSS v4 und shadcn/ui.
Sie ergänzt die bestehende Flask-Anwendung schrittweise; Legacy-Seiten werden
nicht automatisch durch Tailwind verändert.

## Befehle

Vom Repository-Root aus:

```bash
./scripts/frontend.sh ci
./scripts/frontend.sh run check
./scripts/frontend.sh run dev
./scripts/frontend.sh run ui:add -- accordion
```

Der Produktions-Build wird nach `static/dist/liva-ui/` geschrieben. Die interne
Komponentenwerkstatt ist anschließend unter `/ui-lab` erreichbar.

## Struktur

- `src/components/ui/`: eingecheckter, editierbarer shadcn-Komponentencode
- `src/index.css`: LIVA-Farben, Typografie, Radien und semantische Tokens
- `src/App.tsx`: echte Komponenten- und Zustandsbeispiele
- `components.json`: shadcn-Registry- und Alias-Konfiguration
- `vite.config.ts`: deterministischer Flask-Asset-Build

Neue shadcn-Komponenten immer über `ui:add` hinzufügen und danach `check`
ausführen. Bestehende Flask-Seiten werden cardweise migriert, nicht über einen
globalen CSS-Austausch.
