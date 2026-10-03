# LIVA context

## Glossary

**Wochen-Review**: Siehe Wochenfeedback. Es ist kein separater Sonntagsjob und keine eigene Nutzeroberfläche.

**Gesamtanalyse**: Die nutzerseitige LIVA-Analysefläche für gemeinsame Verläufe aus Training, Ernährung, Regeneration und Alltag. Sie zeigt Daten, Trends und zeitliche Zusammenhänge, behauptet aber keine unbewiesenen Ursachen und verwendet keine Evidenz- oder Confidence-Scores.

**Kontextspuren**: Optional einblendbare, visuell zurückhaltende Ereignisse in der Gesamtanalyse: manuelle Flags wie Krankheit oder Alkohol sowie Kalenderinformationen wie belegte Zeiten, Freizeit oder Klausuren. Sie sind Kontext, nicht gleichgewichtete Gesundheits- oder Leistungsdaten.

**Analyseumfang**: Training, Ernährung, Gewicht und Recovery bilden den Kern der Gesamtanalyse. Physique-Bilder gehören nicht dazu, solange daraus keine strukturierten Analysedaten vorliegen.

**Wochenfeedback**: Ein schlanker GPT-Payload für eine abgeschlossene Kalenderwoche. Er enthält wöchentliche Progressionsrate, Planadhärenz, Umfang des Ernährungsloggings, durchschnittliche Makros, Ausreißer und Gewichtsverlauf. Er ist unabhängig von der Gesamtanalyse.

**Phasenanalyse**: Eine frei wählbare, dynamische Analyse eines vergangenen Zeitraums. Sie verwendet dessen Daten und Kontext statt globale Altregeln (etwa `lean_bulk`) auf einen anderen Kontext wie einen Cut anzuwenden. Sie liefert reichhaltige Daten zur Bewertung und Bildung persönlicher Hypothesen, beispielsweise zur individuell passenden Trainingsdosis oder der zeitlichen Wirkung von Alkohol.

**Phasen-Zoom**: Der MCP erhält zuerst einen kompakten Phasenüberblick. Er kann anschließend gezielt Tageswerte, Trainings- und Übungsdetails sowie konkrete stützende und widersprechende Beispiele nachladen. Hypothesen werden klar formuliert, wenn die Daten sie wiederholt tragen; relevante Ausnahmen werden sichtbar gemacht, ohne die Darstellung unnötig unsicher zu formulieren.

**Analyse-Einstieg**: Die Gesamtanalyse hat ihr Zuhause in `/auswertung`. Training, Ernährung und Recovery können mit bereits ausgewähltem Zeitraum oder Befund dorthin verlinken.

**Analysepriorität**: Eine Phasenanalyse betrachtet Trainings-, Körper-/Ernährungs- sowie Recovery-/Alltagsdaten gemeinsam. Sie hebt nur Aussagen hervor, die im ausgewählten Zeitraum auffällig oder handlungsrelevant sind.

**Historischer Zielkontext**: Ein Ernährungsziel ist pro Datum aufzulösen, nicht aus dem aktuellen globalen Modus abzuleiten. Die Reihenfolge lautet: explizit geloggtes Ziel, historischer Wochenplan-Snapshot, datierter Modusabschnitt, sonst kein Ziel. Zielwerte sind in der Phasenanalyse nur eine einblendbare Kontextspur, nie Voraussetzung für eine Gut/Schlecht-Bewertung.

**Wochenfeedback-Auslösung**: Das Wochenfeedback entsteht beim klassischen Montags-Check-in und wertet stets die vollständig abgeschlossene Kalenderwoche Montag bis Sonntag aus. Jede vergangene abgeschlossene Kalenderwoche kann außerdem manuell angefordert werden; ein separater Sonntagsjob existiert nicht.

**Historischer Zielkonflikt**: Widersprechen sich historische Zielquellen, verwendet LIVA die beste Quelle und zeigt den Konflikt nur als zurückhaltende Kontextnotiz. Es fordert keine manuelle Datenpflege und blockiert keine Analyse.
