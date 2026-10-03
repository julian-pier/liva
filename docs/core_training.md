# CORE Training – Calibration Deck

## Nutzung
- Öffne `/core-training`.
- Wische eine Karte nach links für `Nein` oder nach rechts für `Ja`.
- Alternativ funktionieren die Buttons unten und die Pfeiltasten links/rechts.
- CORE zeigt oben, was es gerade selbst tun würde und wie sicher es dabei ist.

## Reset
- `Reset` löscht nur Deck-Historie und gelernte Gewichte des Calibration Decks.
- Trainings-, HRV-, Lauf-, Ernährungs- und Plan-Daten bleiben unberührt.

## Keys
- Die Seite folgt dem bestehenden Key-/CORE-Zugriffsmodell der App.
- `GET /api/core_training/next` nutzt denselben Lesezugang wie CORE.
- Antworten und Reset laufen über denselben eingeloggten Zugriff und senden den vorhandenen CSRF-Header mit.
