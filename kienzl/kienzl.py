from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database.connections import get_hrv_db, get_nutrition_db, get_plans_db, get_runs_db, get_training_db

POOL_SIZE = 20
KIENZL_VERSION = 1


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _day_iso(d: date | str | None) -> str:
    if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d.strip()):
        return d.strip()
    if isinstance(d, date):
        return d.isoformat()
    return date.today().isoformat()


def _safe_float(x) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def _safe_int(x) -> int | None:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _mean(vals: Iterable[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _std(vals: Iterable[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((v - m) ** 2 for v in xs) / (len(xs) - 1)
    return var ** 0.5


def _lin_trend_delta(vals: List[float]) -> float | None:
    if len(vals) < 3:
        return None
    n = len(vals)
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(vals) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, vals))
    den = sum((x - x_mean) ** 2 for x in xs) or 0.0
    if den == 0.0:
        return None
    slope = num / den
    return slope * (n - 1)


def ensure_kienzl_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if conn is None:
        conn = get_training_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kienzl_actions (
          id TEXT PRIMARY KEY,
          domain TEXT NOT NULL,
          title TEXT NOT NULL,
          prompt_seed TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kienzl_phrases (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          action_id TEXT NOT NULL,
          text TEXT NOT NULL,
          created_at TEXT NOT NULL,
          used_count INTEGER NOT NULL DEFAULT 0,
          last_used_at TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(action_id) REFERENCES kienzl_actions(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kienzl_daily (
          day TEXT PRIMARY KEY,
          summary_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kienzl_usage (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          day TEXT NOT NULL,
          phrase_id INTEGER NOT NULL,
          action_id TEXT NOT NULL,
          context TEXT,
          FOREIGN KEY(phrase_id) REFERENCES kienzl_phrases(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kienzl_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kienzl_analyse_weekly (
          week_end TEXT PRIMARY KEY,
          range_start TEXT NOT NULL,
          range_end TEXT NOT NULL,
          text TEXT NOT NULL,
          input_json TEXT NOT NULL,
          model TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_kienzl_phrases_action_active ON kienzl_phrases(action_id, is_active)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_kienzl_usage_day ON kienzl_usage(day)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_kienzl_usage_ts ON kienzl_usage(ts)")
    conn.commit()
    if own:
        conn.close()


@dataclass(frozen=True)
class KienzlAction:
    id: str
    domain: str
    title: str
    prompt_seed: str


def _seed_prompt(action_id: str, signal: str, intent: str) -> str:
    return (
        "Kienzl-Stimme (hart): direkt, erwachsen, leicht provokant, null Drama. Kein 'vielleicht', kein 'könnte', kein Coaching-Kitsch.\n"
        "Struktur (fast immer 2 Sätze):\n"
        "Satz 1: Signal -> Interpretation (knallhart).\n"
        "Satz 2: Handlung -> Rahmen/Trade-off. Oft Schluss-Klammer: 'Punkt.' / 'fertig.' / 'genau so.'.\n"
        "Wording-Muster: 'Das ist nicht X, das ist Y.' / 'Reagiere auf Trend, nicht auf Ego.' / 'Mach’s planbarer, sonst Rauschen.' / 'Ändere eine Variable.'\n"
        "Tabus: keine Emojis, kein Markdown, keine Listen, keine langen Erklärungen. Max 1–2 Sätze.\n"
        f"Signal: {signal}\n"
        f"Intent: {intent}"
    )


CORE_ACTIONS: List[KienzlAction] = [
    KienzlAction(
        id="KIENZL_DECISION_STOP",
        domain="global",
        title="Decision: STOP",
        prompt_seed=_seed_prompt(
            "KIENZL_DECISION_STOP",
            "Heute ist ein Stop-Tag.",
            "Sag nur die Entscheidung und den Rahmen: stoppen oder maximal locker bewegen. Kein Warum.",
        ),
    ),
    KienzlAction(
        id="KIENZL_DECISION_EASY_DOWN",
        domain="global",
        title="Decision: EASY_DOWN",
        prompt_seed=_seed_prompt(
            "KIENZL_DECISION_EASY_DOWN",
            "Heute ist easy down.",
            "Sag nur die Entscheidung und den Rahmen: Intensität runter, Volumen im Rahmen, sauber bleiben. Kein Warum.",
        ),
    ),
    KienzlAction(
        id="KIENZL_DECISION_NORMAL",
        domain="global",
        title="Decision: NORMAL",
        prompt_seed=_seed_prompt(
            "KIENZL_DECISION_NORMAL",
            "Heute ist normal.",
            "Sag nur die Entscheidung und den Rahmen: Plan treffen, nicht eskalieren. Kein Warum.",
        ),
    ),
    KienzlAction(
        id="KIENZL_DECISION_PUSH",
        domain="global",
        title="Decision: PUSH",
        prompt_seed=_seed_prompt(
            "KIENZL_DECISION_PUSH",
            "Heute ist push, aber kontrolliert.",
            "Sag nur die Entscheidung und den Rahmen: gezielt pushen, Rest normal, kein Overkill. Kein Warum.",
        ),
    ),
    KienzlAction(
        id="CAL_SURPLUS_TREND_UP",
        domain="nutrition",
        title="Kalorien drift nach oben",
        prompt_seed=_seed_prompt(
            "CAL_SURPLUS_TREND_UP",
            "Kalorien-Schnitt steigt im Trend, du driftest Richtung Überschuss.",
            "Sag klar, dass es kein Zufall ist und dass du wieder Planbarkeit reinbringen sollst (Track, feste Mahlzeiten, gleiche Basis).",
        ),
    ),
    KienzlAction(
        id="CAL_DEFICIT_TREND_DOWN",
        domain="nutrition",
        title="Kalorien drift nach unten",
        prompt_seed=_seed_prompt(
            "CAL_DEFICIT_TREND_DOWN",
            "Kalorien-Schnitt fällt im Trend, du rutschst Richtung Defizit.",
            "Sag: das ist nicht 'Disziplin', das ist Unterversorgung. Handlungsanweisung: Essen stabilisieren, nicht Training retten wollen.",
        ),
    ),
    KienzlAction(
        id="CAL_VARIANCE_HIGH",
        domain="nutrition",
        title="Kalorien schwanken stark",
        prompt_seed=_seed_prompt(
            "CAL_VARIANCE_HIGH",
            "Kalorien schwanken stark, viel Rauschen im Alltag.",
            "Sag: Rauschen erzeugt Chaos. Handlungsanweisung: gleiche Basis, gleiche Zeiten, gleiche Defaults.",
        ),
    ),
    KienzlAction(
        id="PROTEIN_LOW",
        domain="nutrition",
        title="Protein zu niedrig",
        prompt_seed=_seed_prompt(
            "PROTEIN_LOW",
            "Protein ist im Schnitt zu niedrig.",
            "Sag: ohne Protein ist dein Plan nur Papier. Handlungsanweisung: pro Mahlzeit Protein fix setzen, fertig.",
        ),
    ),
    KienzlAction(
        id="SUGAR_HIGH_CHRONIC",
        domain="nutrition",
        title="Zucker chronisch hoch",
        prompt_seed=_seed_prompt(
            "SUGAR_HIGH_CHRONIC",
            "Zucker ist über Tage hoch, das ist kein Ausrutscher.",
            "Sag: du machst dir Hunger und Cravings selbst. Handlungsanweisung: Süßes begrenzen, Protein+Ballaststoff zuerst.",
        ),
    ),
    KienzlAction(
        id="BW_TREND_UP_WRONG_GOAL",
        domain="bodyweight",
        title="Gewicht steigt trotz Cut",
        prompt_seed=_seed_prompt(
            "BW_TREND_UP_WRONG_GOAL",
            "Gewichtstrend geht hoch, obwohl der Modus nicht 'Bulk' ist.",
            "Sag: Ziel und Realität passen nicht. Handlungsanweisung: Kalorien wieder in Band bringen, nicht herumraten.",
        ),
    ),
    KienzlAction(
        id="BW_TREND_DOWN_WRONG_GOAL",
        domain="bodyweight",
        title="Gewicht fällt trotz Bulk",
        prompt_seed=_seed_prompt(
            "BW_TREND_DOWN_WRONG_GOAL",
            "Gewichtstrend geht runter, obwohl du eigentlich aufbauen willst.",
            "Sag: du isst am Ziel vorbei. Handlungsanweisung: Kalorien hoch und konstant, fertig.",
        ),
    ),
    KienzlAction(
        id="BW_NOISE_HIGH_TREND_STABLE",
        domain="bodyweight",
        title="Gewicht stabil, aber viel Rauschen",
        prompt_seed=_seed_prompt(
            "BW_NOISE_HIGH_TREND_STABLE",
            "Gewichtstrend ist grob stabil, aber die Schwankung ist hoch.",
            "Sag: das ist Rauschen, nicht Progress. Handlungsanweisung: gleiche Messroutine, gleiche Salz-/Carb-Pattern.",
        ),
    ),
    KienzlAction(
        id="MISSED_SESSIONS_UP",
        domain="planning",
        title="Mehr verpasste Sessions",
        prompt_seed=_seed_prompt(
            "MISSED_SESSIONS_UP",
            "Du verpasst aktuell zu viele geplante Sessions.",
            "Sag: Plan ist nur echt, wenn du auftauchst. Handlungsanweisung: nächstes Training fix blocken und ausführen, kein Drama.",
        ),
    ),
    KienzlAction(
        id="STREAK_GOOD",
        domain="planning",
        title="Streak läuft",
        prompt_seed=_seed_prompt(
            "STREAK_GOOD",
            "Du triffst den Plan zuverlässig.",
            "Sag: Kontrolle schlägt Talent. Handlungsanweisung: genau so weitermachen, keine Extras erfinden.",
        ),
    ),
    KienzlAction(
        id="OVERRIDES_HIGH",
        domain="planning",
        title="Zu viele Overrides",
        prompt_seed=_seed_prompt(
            "OVERRIDES_HIGH",
            "Zu viele Plan-Overrides/Manuell-Eingriffe.",
            "Sag: du fummelst am System. Handlungsanweisung: eine Woche ohne Eingriffe durchziehen, dann erst bewerten.",
        ),
    ),
    KienzlAction(
        id="MULTIPLE_ACTIVE_PLANS",
        domain="planning",
        title="Mehrere aktive Pläne",
        prompt_seed=_seed_prompt(
            "MULTIPLE_ACTIVE_PLANS",
            "Mehrere Pläne sind gleichzeitig aktiv oder wirken gleichzeitig.",
            "Sag: zwei Pläne = keiner. Handlungsanweisung: einen aktiv lassen, den Rest killen.",
        ),
    ),
    KienzlAction(
        id="SICKNESS_MARKED",
        domain="global",
        title="Krankheit markiert",
        prompt_seed=_seed_prompt(
            "SICKNESS_MARKED",
            "Krankheit ist markiert oder Symptome stehen im Raum.",
            "Sag: heute ist Management, nicht Performance. Handlungsanweisung: stoppen oder nur locker bewegen, fertig.",
        ),
    ),
    KienzlAction(
        id="HRV_RMSSD_BELOW_BASELINE",
        domain="recovery",
        title="RMSSD unter Baseline",
        prompt_seed=_seed_prompt(
            "HRV_RMSSD_BELOW_BASELINE",
            "RMSSD liegt unter Baseline.",
            "Sag: Qualität ja, Eskalation nein. Handlungsanweisung: Intensität runter, Volumen sauber.",
        ),
    ),
    KienzlAction(
        id="HRV_RMSSD_ABOVE_BASELINE",
        domain="recovery",
        title="RMSSD über Baseline",
        prompt_seed=_seed_prompt(
            "HRV_RMSSD_ABOVE_BASELINE",
            "RMSSD liegt über Baseline.",
            "Sag: das ist ein gutes Signal, aber kein Freifahrtschein. Handlungsanweisung: kontrolliert pushen, nicht eskalieren.",
        ),
    ),
    KienzlAction(
        id="RHR_ABOVE_NORMAL",
        domain="recovery",
        title="RHR über normal",
        prompt_seed=_seed_prompt(
            "RHR_ABOVE_NORMAL",
            "Ruhepuls liegt über normal.",
            "Sag: System ist gereizt. Handlungsanweisung: heute leicht, Technik und Tempo deckeln.",
        ),
    ),
    KienzlAction(
        id="RMSSD_DOWN_MULTI_DAY",
        domain="recovery",
        title="RMSSD mehrere Tage down",
        prompt_seed=_seed_prompt(
            "RMSSD_DOWN_MULTI_DAY",
            "RMSSD ist mehrere Tage im Drift nach unten.",
            "Sag: das ist Trend, nicht Zufall. Handlungsanweisung: eine Stufe runter, dann wieder aufbauen.",
        ),
    ),
    KienzlAction(
        id="HRV_CONFLICT_RMSSD_RHR",
        domain="recovery",
        title="RMSSD/RHR Konflikt",
        prompt_seed=_seed_prompt(
            "HRV_CONFLICT_RMSSD_RHR",
            "RMSSD und Ruhepuls ziehen in verschiedene Richtungen.",
            "Sag: gemischtes Signal. Handlungsanweisung: konservativ bleiben, Plan treffen, keine Heldentaten.",
        ),
    ),
    KienzlAction(
        id="SLEEP_QUALITY_LOW",
        domain="recovery",
        title="Schlafqualität niedrig",
        prompt_seed=_seed_prompt(
            "SLEEP_QUALITY_LOW",
            "Schlafqualität ist niedrig.",
            "Sag: du baust heute keinen Peak. Handlungsanweisung: easy, sauber, früh raus aus dem Gym.",
        ),
    ),
    KienzlAction(
        id="SLEEP_QUALITY_HIGH",
        domain="recovery",
        title="Schlafqualität hoch",
        prompt_seed=_seed_prompt(
            "SLEEP_QUALITY_HIGH",
            "Schlafqualität ist auffällig gut.",
            "Sag: gutes Setup. Handlungsanweisung: nutz es für einen sauberen Top-Satz, Rest normal.",
        ),
    ),
    KienzlAction(
        id="WAKE_TIME_VARIANCE_HIGH",
        domain="recovery",
        title="Aufstehzeit schwankt stark",
        prompt_seed=_seed_prompt(
            "WAKE_TIME_VARIANCE_HIGH",
            "Aufstehzeit schwankt stark über Tage.",
            "Sag: das ist Rauschen im System. Handlungsanweisung: feste Aufstehzeit/Window, sonst bezahlst du den Preis.",
        ),
    ),
    KienzlAction(
        id="TONNAGE_UP_STRONG",
        domain="strength",
        title="Kraft-Volumen steigt",
        prompt_seed=_seed_prompt(
            "TONNAGE_UP_STRONG",
            "Kraft-Umfang/Load ist im Trend hoch.",
            "Sag: du ziehst es durch. Handlungsanweisung: Qualität halten, keine Extra-Sätze sammeln.",
        ),
    ),
    KienzlAction(
        id="TONNAGE_DOWN_STRONG",
        domain="strength",
        title="Kraft-Volumen fällt",
        prompt_seed=_seed_prompt(
            "TONNAGE_DOWN_STRONG",
            "Kraft-Umfang/Load ist im Trend runter.",
            "Sag: du lässt liegen. Handlungsanweisung: Plan wieder treffen, erst dann optimieren.",
        ),
    ),
    KienzlAction(
        id="RUN_EFFICIENCY_UP",
        domain="running",
        title="Run-Effizienz besser",
        prompt_seed=_seed_prompt(
            "RUN_EFFICIENCY_UP",
            "Laufen sieht effizienter aus als zuletzt.",
            "Sag: das ist sauberer Output. Handlungsanweisung: keep it controlled, keine Pace-Eitelkeit.",
        ),
    ),
    KienzlAction(
        id="RUN_EFFICIENCY_DOWN",
        domain="running",
        title="Run-Effizienz schlechter",
        prompt_seed=_seed_prompt(
            "RUN_EFFICIENCY_DOWN",
            "Laufen sieht teurer aus als zuletzt.",
            "Sag: du zahlst mehr für weniger. Handlungsanweisung: easy Pace, Technik, und erst dann wieder drücken.",
        ),
    ),
]


SEED_PHRASES: Dict[str, List[str]] = {
    "KIENZL_DECISION_STOP": [
        "Heute ist Stop. Kein Ego: raus aus dem Gas und nur Hygiene-Bewegung, fertig.",
        "Stop-Tag. Du musst heute nichts beweisen: runterfahren, fertig.",
        "Heute stoppst du. Management statt Performance. Punkt.",
        "Stop. Wenn du trainierst, dann nur locker und kurz. fertig.",
        "Heute ist Pause. Reagiere auf das Signal, nicht auf dein Ego. Punkt.",
        "Stop-Entscheidung. Schlaf, Essen, Ruhe. Training ist heute optional: nein. fertig.",
    ],
    "KIENZL_DECISION_EASY_DOWN": [
        "Heute easy down. Bremse rein, Plan treffen, keine Eskalation. Punkt.",
        "Easy down. Du bleibst sauber und kontrolliert, der Rest ist Ego. fertig.",
        "Heute nimmst du Druck raus. Qualität ja, Eskalation nein. Punkt.",
        "Easy down. Ändere eine Variable: weniger Intensität. fertig.",
        "Heute konservativ. Reagiere auf Trend, nicht auf Stimmung. Punkt.",
        "Easy down. Wenn Performance leidet, ist das der Preis. genau so.",
    ],
    "KIENZL_DECISION_NORMAL": [
        "Heute normal. Plan treffen, nicht spielen. Punkt.",
        "Normal-Tag. Mach’s solide und hör auf zu optimieren. fertig.",
        "Heute ganz normal. Konstanz schlägt Kreativität. Punkt.",
        "Normal. Reagiere auf Trend, nicht auf Ego. genau so.",
        "Heute lieferst du ab. Keine Extras, keine Ausreden. Punkt.",
        "Normal. Wenn du mehr willst: mach’s erst planbar. fertig.",
    ],
    "KIENZL_DECISION_PUSH": [
        "Heute push. Gezielt, nicht gierig. Punkt.",
        "Push-Tag. Eine Sache hart, der Rest sauber. fertig.",
        "Heute darfst du drücken. Reagiere auf Trend, nicht auf Ego. Punkt.",
        "Push, aber kontrolliert. Wenn du eskalierst, zahlst du später. fertig.",
        "Heute pushst du. Mach’s planbar und sauber, nicht spektakulär. Punkt.",
        "Push. Ändere eine Variable: etwas mehr Intensität, sonst nichts. genau so.",
    ],
    "CAL_SURPLUS_TREND_UP": [
        "Kalorien schieben nach oben. Das ist Drift, nicht Magie: mach’s wieder planbar.",
        "Du rutschst in den Überschuss. Stell die Defaults fest ein und hör auf zu improvisieren.",
        "Essen wird gerade zu locker. Kontrolle rein, dann erst Freiheit.",
        "Kalorien steigen. Wenn du das nicht willst: gleiche Basis, gleiche Zeiten, fertig.",
        "Du sammelst Extras. Streichen, tracken, und gut ist.",
        "Überschuss-Trend. Nicht diskutieren: Mahlzeiten standardisieren.",
        "Kalorien gehen hoch. Das ist kein 'normal': Band setzen und treffen.",
        "Drift nach oben. Plan schlagen, nicht Stimmung.",
        "Mehr Kalorien als nötig. Runter mit dem Rauschen, dann reden wir weiter.",
        "Du isst Richtung Bulk ohne Plan. Entweder bewusst oder lass es.",
    ],
    "CAL_DEFICIT_TREND_DOWN": [
        "Kalorien fallen. Das ist kein Hero-Move: stabilisieren und nicht Training kompensieren.",
        "Du rutschst ins Defizit. Essen hoch und konstant, fertig.",
        "Kalorien gehen runter. Wenn du Leistung willst: Fuel zuerst, dann Ballern.",
        "Defizit-Trend. Stell das Essen wieder auf Schienen.",
        "Du sparst am falschen Ende. Plan rein, nicht Hunger spielen.",
        "Kalorien sinken. Das ist Drift: mach es wieder bewusst.",
        "Du isst zu wenig. Heute: Basis hochziehen, nicht jammern.",
        "Unterversorgung im Trend. Stabil essen, dann erst Intensität.",
        "Kalorien runter. Wenn du dich wundert: hör auf zu raten und tracke sauber.",
        "Defizit ohne Ansage. Korrigieren, nicht romantisieren.",
    ],
    "CAL_VARIANCE_HIGH": [
        "Kalorien sind ein Zickzack. Das ist Rauschen: bau dir eine Basis und halte sie.",
        "Du würfelst beim Essen. Standardisieren, dann wird’s ruhig.",
        "Große Schwankungen. Planbarkeit rein, sonst ist alles nur Zufall.",
        "Essen ist zu chaotisch. Gleiche Defaults, gleiche Reihenfolge, fertig.",
        "Rauschen im Kalorien-Output. Weniger Varianten, mehr Routine.",
        "Kalorien schwanken hart. Das ist kein Flex: mach’s boring.",
        "Zuviel Spread. Fixe Mahlzeiten und gleiche Snacks.",
        "Du fährst Achterbahn. Runter mit dem Chaos, dann kommt Progress.",
        "Kalorien-Rauschen. Kontrolle ist eine Entscheidung, nicht ein Gefühl.",
        "Schwankungen killen die Aussage. Routine rein, fertig.",
    ],
    "PROTEIN_LOW": [
        "Protein ist zu niedrig. Ohne das ist dein Plan nur Deko: pro Mahlzeit fix setzen.",
        "Du lässt Protein liegen. Stell’s als Standard ein, nicht als Option.",
        "Protein fehlt im Schnitt. Erst Protein, dann der Rest.",
        "Zu wenig Protein. Mach’s langweilig und konsequent.",
        "Protein ist nicht verhandelbar. Fixe Quelle pro Mahlzeit, fertig.",
        "Du baust ohne Baustoff. Protein hoch, dann reden wir.",
        "Protein zu niedrig. Track es wie Training, nicht wie Stimmung.",
        "Du snackst am Ziel vorbei. Protein zuerst, dann Süßkram.",
        "Zu wenig Protein. Das ist kein Detail, das ist die Basis.",
        "Protein fällt ab. Heb’s an und halt’s stabil.",
    ],
    "SUGAR_HIGH_CHRONIC": [
        "Zucker ist über Tage hoch. Das ist Muster: begrenzen und ersetzen, fertig.",
        "Du fütterst Cravings. Zucker runter, Protein und Ballaststoff zuerst.",
        "Zucker bleibt hoch. Das macht dein Essen instabil: cut the noise.",
        "Süßes ist gerade zu präsent. Stell klare Grenzen und halte sie.",
        "Chronisch viel Zucker. Das ist kein Reward: Struktur rein.",
        "Zucker-Drift. Wenn du Kontrolle willst, fang hier an.",
        "Zucker ist dauerhaft hoch. Weniger Trigger-Food, mehr Basics.",
        "Du snackst dich raus. Zucker runter, echte Mahlzeiten hoch.",
        "Zucker läuft aus dem Ruder. Stoppe das Muster, nicht den Tag.",
        "Zu viel Zucker. Wenn du es nicht einkaufst, kannst du es nicht essen.",
    ],
    "BW_TREND_UP_WRONG_GOAL": [
        "Gewichtstrend geht hoch. Ziel passt nicht: Kalorien wieder ins Band bringen.",
        "Du nimmst zu in der falschen Richtung. Track sauber und hör auf zu schätzen.",
        "Gewicht steigt. Das ist kein 'Wasser' als Standard: planbarer essen.",
        "Trend nach oben. Korrigieren, nicht diskutieren.",
        "Gewicht hoch. Kontrolle rein, dann wird’s wieder klar.",
        "Du driftest hoch. Band treffen, fertig.",
        "Gewichtstrend hoch. Wenn du cuttest, ist das ein Signal: nicht ignorieren.",
        "Skala geht hoch. Plan schlagen, nicht hoffen.",
        "Trend hoch. Essen wieder standardisieren, dann schauen wir weiter.",
        "Du sammelst zu viele Extras. Weg damit und stabil bleiben.",
    ],
    "BW_TREND_DOWN_WRONG_GOAL": [
        "Gewichtstrend geht runter. Wenn du aufbauen willst: Kalorien hoch und konstant.",
        "Du verlierst Gewicht im falschen Modus. Mehr essen, nicht mehr raten.",
        "Trend nach unten. Korrigier den Input, nicht das Training.",
        "Gewicht fällt. Fuel hochziehen und sauber treffen.",
        "Du bist zu knapp unterwegs. Kalorien hoch, fertig.",
        "Trend runter. Erst Essen stabil, dann Performance.",
        "Skala geht runter. Für Aufbau: Überschuss bewusst setzen.",
        "Gewicht sinkt. Das ist kein Feature: planbar mehr essen.",
        "Trend down. Wenn du wachsen willst, brauchst du Input.",
        "Du bist zu sparsam. Heb’s an und halt’s konstant.",
    ],
    "BW_NOISE_HIGH_TREND_STABLE": [
        "Gewicht ist grob stabil, aber es rauscht. Routine beim Wiegen, dann erst interpretieren.",
        "Trend stabil, Rauschen hoch. Mach die Messung langweilig, dann wird’s klar.",
        "Du siehst nur Schwankung. Gleiche Bedingungen, gleiche Uhrzeit, fertig.",
        "Stabiler Trend, chaotische Tage. Standardisieren, dann hat’s Aussage.",
        "Gewicht schwankt stark. Das ist Rauschen: nicht emotional werden.",
        "Rauschen im Gewicht. Erst Routine, dann Entscheidungen.",
        "Trend stabil, aber du misst wie ein Glücksspiel. Fixe Routine.",
        "Zu viel Noise. Halt die Basics konstant, dann sinkt das Drama.",
        "Gewicht ist nicht das Problem, deine Messung ist es. Mach’s sauber.",
        "Stabil, aber unruhig. Routine rein und Ruhe im Kopf.",
    ],
    "MISSED_SESSIONS_UP": [
        "Du verpasst Sessions. Plan ist nur echt, wenn du auftauchst: nächste Einheit fix durchziehen.",
        "Mehr Ausfälle. Weniger Diskussion, mehr Termin im Kalender.",
        "Du lässt Training liegen. Plan treffen, dann optimieren.",
        "Sessions fehlen. Das ist keine Strategie: auftauchen und arbeiten.",
        "Du driftest aus dem Plan. Nächste Einheit machen, egal wie du dich fühlst.",
        "Zu viele Lücken. System bauen: feste Slots, keine Ausreden.",
        "Du bist inkonsistent. Heute: klein anfangen, aber auftauchen.",
        "Plan wird gerade optional. Mach ihn wieder Pflicht.",
        "Sessions fehlen. Erst Attendance, dann Intensität.",
        "Du brauchst weniger Motivation, mehr Routine. Punkt.",
    ],
    "STREAK_GOOD": [
        "Du triffst den Plan. Kontrolle schlägt Talent: bleib genau so boring.",
        "Streak läuft. Kein Extra-Gebastel, einfach weiter.",
        "Plan-Compliance sitzt. Das ist Progress: nicht kaputt optimieren.",
        "Du bist zuverlässig. Halte den Output konstant, fertig.",
        "Du tauchst auf. Genau das ist der Unterschied.",
        "Streak gut. Jetzt nicht nervös werden und irgendwas ändern.",
        "Plan sitzt. Weiter arbeiten, nicht feiern.",
        "Konstanz ist da. Lass die Eitelkeit weg und bleib im System.",
        "Du machst’s richtig. Das fühlt sich langweilig an, und genau deshalb funktioniert’s.",
        "Gute Serie. Halte die Basics, dann kommt der Rest.",
    ],
    "OVERRIDES_HIGH": [
        "Zu viele Overrides. Du fummelst am System: eine Woche ohne Eingriffe durchziehen.",
        "Du übersteuerst den Plan. Weniger Kontrolle spielen, mehr ausführen.",
        "Override-Mode an. Stop das und mach einfach den Plan.",
        "Zu viel Manuell. Eine Variable ändern, nicht zehn.",
        "Du bastelst. Plan treffen, dann bewerten.",
        "Overrides häufen sich. Das ist Unsicherheit: bleib konservativ und konstant.",
        "Du änderst zu oft. Eine Woche Stabilität, dann reden wir.",
        "Plan ist kein Menü. Hör auf zu picken.",
        "Du suchst die perfekte Einheit. Du brauchst die nächste Einheit.",
        "Zu viele Eingriffe. System respektieren, fertig.",
    ],
    "MULTIPLE_ACTIVE_PLANS": [
        "Mehrere Pläne aktiv. Zwei Pläne = keiner: einen wählen, Rest killen.",
        "Du fährst Doppelspur. Entscheide dich für einen Plan.",
        "Zwei Systeme gleichzeitig. Das ist Chaos: eins aktiv, fertig.",
        "Mehrere Pläne wirken parallel. Das ist Selbstsabotage: aufräumen.",
        "Du willst alles. Ergebnis: nichts. Einen Plan wählen.",
        "Zwei Pläne konkurrieren. Kill den Lärm und mach’s klar.",
        "Mehrere aktive Pläne. Das ist nicht clever, das ist unklar.",
        "Du hast Plan-Spaghetti. Ein Plan, ein Fokus.",
        "Zwei Pläne sind ein Ausreden-Buffet. Cut it.",
        "Plan-Chaos. Auf einen committen, dann liefern.",
    ],
    "SICKNESS_MARKED": [
        "Krankheit ist markiert. Heute ist Management, nicht Performance: stop oder nur locker bewegen.",
        "Sickness-Flag. Kein Heldentum: runterfahren und gut.",
        "Du bist krank. Training ist heute maximal Hygiene, fertig.",
        "Symptome im Raum. Stop die Eskalation und mach es leicht.",
        "Krankheit markiert. Erholung priorisieren, nicht PRs jagen.",
        "Sick-Mode. Weniger tun ist heute die richtige Entscheidung.",
        "Krankheit. Plan pausieren, Körper managen.",
        "Du bist nicht fit. Easy bewegen oder komplett raus, fertig.",
        "Sickness-Flag. Heute nicht beweisen, heute schützen.",
        "Krankheit markiert. Stop, essen, schlafen, fertig.",
    ],
    "HRV_RMSSD_BELOW_BASELINE": [
        "RMSSD unter Baseline. Qualität ja, Eskalation nein: Intensität runter.",
        "RMSSD niedrig. Heute kontrolliert bleiben und nicht pushen.",
        "RMSSD drunter. Mach’s sauber und konservativ.",
        "RMSSD unter Normal. Kein Ego-Training: easy und strukturiert.",
        "RMSSD down. Heute ist Plan treffen genug.",
        "RMSSD unter Baseline. Deckel drauf und Technik priorisieren.",
        "RMSSD niedrig. Du brauchst keine Heldentaten, du brauchst Kontrolle.",
        "RMSSD drunter. Intensität runter, Volumen sauber.",
        "RMSSD down. Heute nicht eskalieren, fertig.",
        "RMSSD unter Baseline. Das ist Signal: easy und raus.",
    ],
    "HRV_RMSSD_ABOVE_BASELINE": [
        "RMSSD über Baseline. Gutes Signal, aber kein Freifahrtschein: kontrolliert pushen.",
        "RMSSD hoch. Nimm den Tag mit, aber bleib sauber.",
        "RMSSD über normal. Ein gezielter Push, Rest normal.",
        "RMSSD hoch. Qualität hoch halten, nicht mehr Volumen sammeln.",
        "RMSSD über Baseline. Nutze es, ohne zu eskalieren.",
        "RMSSD gut. Heute darfst du drücken, aber gezielt.",
        "RMSSD hoch. Ein Top-Satz, dann zurück in den Plan.",
        "RMSSD über normal. Keine Eitelkeit, nur Output.",
        "RMSSD hoch. Push kontrolliert, fertig.",
        "RMSSD über Baseline. Heute ist Spielraum da, nutz ihn sauber.",
    ],
    "RHR_ABOVE_NORMAL": [
        "Ruhepuls höher als normal. System ist gereizt: heute leicht und ruhig.",
        "RHR hoch. Deckel drauf, Tempo und Intensität runter.",
        "Ruhepuls oben. Kein Push-Tag: easy und sauber.",
        "RHR erhöht. Heute nicht beweisen, heute verwalten.",
        "Ruhepuls hoch. Konservativ trainieren, fertig.",
        "RHR über normal. Weniger Härte, mehr Qualität.",
        "RHR hoch. Technik und Kontrolle, nicht Ballern.",
        "Ruhepuls oben. Kappt die Eskalation, fertig.",
        "RHR erhöht. Heute easy und früher Schluss.",
        "Ruhepuls hoch. Du brauchst Bremse, nicht Gas.",
    ],
    "RMSSD_DOWN_MULTI_DAY": [
        "RMSSD mehrere Tage down. Das ist Trend: eine Stufe runter und stabilisieren.",
        "RMSSD driftet seit Tagen. Konservativ fahren und Routine halten.",
        "Mehrtagiger RMSSD-Downtrend. Heute easy und sauber.",
        "RMSSD sinkt über mehrere Tage. Intensität reduzieren, Plan treffen.",
        "Trend down. Du brauchst eine kleine Entlastung, nicht Drama.",
        "RMSSD seit Tagen runter. Nimm das ernst und geh einen Schritt zurück.",
        "Mehrtagiger Drift. Heute keine Eskalation, fertig.",
        "RMSSD down über Tage. Stabilisieren, dann wieder aufbauen.",
        "Trend down. Easy down, sauber bleiben.",
        "RMSSD driftet. Bremse rein und System beruhigen.",
    ],
    "HRV_CONFLICT_RMSSD_RHR": [
        "RMSSD und Ruhepuls sind nicht auf einer Linie. Gemischtes Signal: konservativ bleiben.",
        "Konflikt im Signal. Kein Push: Plan treffen und gut.",
        "RMSSD vs RHR widersprüchlich. Heute konservativ und sauber.",
        "Gemischtes Signal. Keine Heldentaten, nur Basics.",
        "Konflikt. Tempo raus und Qualität rein.",
        "Widerspruch im System. Konservativ trainieren, fertig.",
        "Signal nicht klar. Du bleibst im Plan und übertreibst nicht.",
        "RMSSD hoch, RHR auch. Das ist kein grünes Licht: normal fahren.",
        "Gemischt. Konservativ, aber nicht panisch.",
        "Signal-Konflikt. Heute boring und stabil.",
    ],
    "SLEEP_QUALITY_LOW": [
        "Schlafqualität niedrig. Das ist nicht 'hardcore', das ist dumm: heute easy bleiben.",
        "Schlaf schlecht. Heute nicht ballern, heute sauber durchkommen.",
        "Low sleep. Intensität runter und früher raus.",
        "Schlafqualität drunter. Technik und Kontrolle, kein Push.",
        "Schlaf schlecht. Plan treffen reicht, fertig.",
        "Low sleep. Heute easy down, sauber bleiben.",
        "Schlafqualität niedrig. Du brauchst Bremse, nicht Ego.",
        "Schlaf schlecht. Kein PR-Tag, Punkt.",
        "Low sleep. Mach’s leicht und lass das Heldentum weg.",
        "Schlafqualität down. Ruhig trainieren und gut ist.",
    ],
    "SLEEP_QUALITY_HIGH": [
        "Schlafqualität stark. Das ist nicht Magie, das ist Setup: nutz es sauber.",
        "Schlaf gut. Nutze den Tag kontrolliert, nicht chaotisch.",
        "High sleep. Heute darfst du pushen, aber gezielt.",
        "Schlafqualität hoch. Qualität hoch, Volumen normal.",
        "Schlaf gut. Mach eine Sache hart, den Rest sauber.",
        "High sleep. Das ist grünes Licht für kontrollierten Output.",
        "Schlafqualität hoch. Kein Overkill, nur ein klarer Fokus.",
        "Schlaf gut. Ein Push-Element, fertig.",
        "High sleep. Nutze es, ohne dich zu verlieren.",
        "Schlafqualität stark. Du kannst liefern, wenn du kontrolliert bleibst.",
    ],
    "WAKE_TIME_VARIANCE_HIGH": [
        "Aufstehzeit ist ein Zickzack. Das ist nicht Flex, das ist Rauschen: mach’s planbarer. Punkt.",
        "Deine Aufstehzeit schwankt hart. System kaputt: Window setzen und halten. fertig.",
        "Wake-up Chaos. Wenn Performance leidet, ist das der Preis: Routine rein. Punkt.",
        "Aufstehzeit schwankt. Ändere eine Variable: feste Zeit, sonst bleibt’s Rauschen. genau so.",
        "Unruhiger Rhythmus. Das ist nicht Stress, das ist schlecht gemanagt: stabilisieren. Punkt.",
        "Aufstehzeit driftet. Mach’s boring und konstant, dann wird’s wieder sauber. fertig.",
    ],
    "TONNAGE_UP_STRONG": [
        "Kraft-Load läuft hoch. Das ist Arbeit: Qualität halten, keine Extra-Sätze sammeln.",
        "Tonnage steigt. Keep it clean und bleib im Plan.",
        "Volumen hoch. Sauber ausführen, nicht gierig werden.",
        "Du schiebst mehr Last. Technik halten, fertig.",
        "Trend up im Kraft-Output. Kein Overkill, nur Kontrolle.",
        "Mehr Load. Genau so, aber ohne Ego-Zusätze.",
        "Volumen steigt. Wenn du dich gut fühlst: bleib trotzdem sauber.",
        "Kraft-Umfang hoch. Stabil bleiben und nicht eskalieren.",
        "Trend up. Plan treffen, nicht übertreffen.",
        "Mehr Last im System. Qualität priorisieren, Punkt.",
    ],
    "TONNAGE_DOWN_STRONG": [
        "Kraft-Load fällt. Du lässt liegen: Plan wieder treffen, dann optimieren.",
        "Tonnage runter. Erst Attendance, dann Feinheiten.",
        "Volumen sinkt. Plan treffen, fertig.",
        "Du schiebst weniger Last. Kein Drama: wieder sauber abliefern.",
        "Trend down. Du brauchst Routine, nicht neue Tricks.",
        "Weniger Kraft-Output. Plan wieder ernst nehmen.",
        "Volumen fällt. Fix die Basics und hör auf zu basteln.",
        "Trend runter. Heute: Plan treffen und gut.",
        "Load down. Nicht schönreden: wieder liefern.",
        "Weniger Last. Attendance und Ausführung zuerst.",
    ],
    "RUN_EFFICIENCY_UP": [
        "Laufen sieht effizienter aus. Gutes Signal: kontrolliert bleiben, keine Pace-Eitelkeit.",
        "Run-Effizienz hoch. Sauberer Output: keep it steady.",
        "Du läufst günstiger. Nutze es, ohne zu überziehen.",
        "Effizienz up. Heute darfst du sauber drücken, aber kontrolliert.",
        "Laufen läuft. Bleib im Plan und mach’s nicht zum Wettkampf.",
        "Effizienz besser. Konstanz halten, fertig.",
        "Run-Output wirkt sauber. Kein Overpace, nur Kontrolle.",
        "Effizienz up. Ein klarer Reiz, Rest easy.",
        "Du läufst ökonomischer. Nicht eskalieren, sondern stabilisieren.",
        "Run-Effizienz hoch. Genau so, ohne Drama.",
    ],
    "RUN_EFFICIENCY_DOWN": [
        "Laufen ist teurer als sonst. Easy Pace und Technik, dann wird’s wieder sauber.",
        "Run-Effizienz down. Heute konservativ bleiben.",
        "Du zahlst mehr pro Tempo. Runter mit der Pace, fertig.",
        "Effizienz schlechter. Easy laufen und den Kopf rausnehmen.",
        "Run wirkt teuer. Heute nicht drücken, heute sauber bewegen.",
        "Effizienz down. Technik und ruhiger Output, fertig.",
        "Laufen kostet mehr. Easy und kontrolliert.",
        "Run-Effizienz schlechter. Kein Pace-Game, nur Basis.",
        "Du bist gerade weniger effizient. Konservativ bleiben und weiter arbeiten.",
        "Effizienz down. Easy Pace, dann erst wieder pushen.",
    ],
}


def ensure_kienzl_schema_and_seed() -> None:
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        cur = conn.cursor()
        # upsert actions (keeps prompts current across deploys)
        for a in CORE_ACTIONS:
            cur.execute(
                """
                INSERT INTO kienzl_actions (id, domain, title, prompt_seed, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                  domain = excluded.domain,
                  title = excluded.title,
                  prompt_seed = excluded.prompt_seed,
                  active = 1
                """,
                (a.id, a.domain, a.title, a.prompt_seed),
            )
        # phrases: ensure at least 10 per action
        now = _utc_iso()
        for a in CORE_ACTIONS:
            phrases = SEED_PHRASES.get(a.id) or []
            if not phrases:
                continue
            cnt = cur.execute(
                "SELECT COUNT(*) FROM kienzl_phrases WHERE action_id=? AND is_active=1",
                (a.id,),
            ).fetchone()[0]
            if cnt >= min(10, len(phrases)):
                continue
            to_add = phrases[: max(0, min(len(phrases), 10) - cnt)]
            for text in to_add:
                cur.execute(
                    "INSERT INTO kienzl_phrases (action_id, text, created_at, used_count, last_used_at, is_active) VALUES (?, ?, ?, 0, NULL, 1)",
                    (a.id, text.strip(), now),
                )
        conn.commit()
    finally:
        conn.close()


def _get_action(conn: sqlite3.Connection, action_id: str) -> sqlite3.Row | None:
    try:
        row = conn.execute("SELECT * FROM kienzl_actions WHERE id=? AND active=1", (action_id,)).fetchone()
        return row
    except Exception:
        return None


def _pick_phrase(conn: sqlite3.Connection, action_id: str) -> Tuple[int | None, str | None]:
    row = conn.execute(
        """
        SELECT id, text
        FROM kienzl_phrases
        WHERE action_id = ? AND is_active = 1
        ORDER BY used_count ASC, created_at ASC, id ASC
        LIMIT 1
        """,
        (action_id,),
    ).fetchone()
    if not row:
        act = _get_action(conn, action_id)
        if not act:
            return None, None
        seed = (act["prompt_seed"] or "").strip()
        # fallback: last line after "Signal:" if present, else seed trimmed
        m = re.search(r"^Signal:\\s*(.+)$", seed, flags=re.MULTILINE)
        fallback = (m.group(1).strip() if m else seed.splitlines()[0].strip() if seed else "").strip()
        if fallback:
            fallback = re.sub(r"\s+", " ", fallback).strip()
        return None, fallback or None
    return int(row["id"]), (row["text"] or "").strip()


def _pick_phrase_for_day(conn: sqlite3.Connection, action_id: str, day_iso: str) -> Tuple[int | None, str | None]:
    """
    Stable-per-day selection among least-used phrases.
    Keeps the "least-used first" spirit, but avoids getting stuck on the oldest phrase forever.
    """
    day_iso = _day_iso(day_iso)
    row = conn.execute(
        """
        SELECT MIN(used_count) AS m
        FROM kienzl_phrases
        WHERE action_id = ? AND is_active = 1
        """,
        (action_id,),
    ).fetchone()
    if not row or row["m"] is None:
        return _pick_phrase(conn, action_id)
    m = int(row["m"])
    rows = conn.execute(
        """
        SELECT id, text, created_at
        FROM kienzl_phrases
        WHERE action_id = ? AND is_active = 1 AND used_count = ?
        ORDER BY created_at ASC, id ASC
        LIMIT 12
        """,
        (action_id, m),
    ).fetchall()
    if not rows:
        return _pick_phrase(conn, action_id)

    # deterministic index for the day/action, stable across reloads
    key = f"{day_iso}:{action_id}".encode("utf-8")
    idx = (int(hashlib.sha1(key).hexdigest(), 16) % len(rows)) if rows else 0
    r = rows[idx]
    return int(r["id"]), (r["text"] or "").strip()


def record_usage(day: str, phrase_id: int, action_id: str, context: str | None = None) -> bool:
    day = _day_iso(day)
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        now = _utc_iso()
        conn.execute(
            "INSERT INTO kienzl_usage (ts, day, phrase_id, action_id, context) VALUES (?, ?, ?, ?, ?)",
            (now, day, int(phrase_id), action_id, (context or "").strip() or None),
        )
        conn.execute(
            "UPDATE kienzl_phrases SET used_count = used_count + 1, last_used_at = ? WHERE id = ?",
            (now, int(phrase_id)),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def load_daily_cached(day: str) -> dict | None:
    day = _day_iso(day)
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        row = conn.execute("SELECT summary_json FROM kienzl_daily WHERE day = ?", (day,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["summary_json"])
        except Exception:
            return None
    finally:
        conn.close()


def save_daily(day: str, payload: dict) -> None:
    day = _day_iso(day)
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        conn.execute(
            """
            INSERT INTO kienzl_daily (day, summary_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
              summary_json = excluded.summary_json,
              created_at = excluded.created_at
            """,
            (day, json.dumps(payload, ensure_ascii=False), _utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _date_window(day_iso: str, lookback: int) -> Tuple[str, str]:
    d = datetime.strptime(day_iso, "%Y-%m-%d").date()
    start = (d - timedelta(days=lookback - 1)).isoformat()
    return start, day_iso


def _fetch_hrv_rows(day_iso: str, lookback: int) -> List[dict]:
    start_iso, end_iso = _date_window(day_iso, lookback)
    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) AS date_iso,
              hr,
              rmssd,
              sickness,
              sleep_quality,
              training_motivation
            FROM hrv_measurements
            WHERE (date_utc IS NOT NULL OR ts_measurement IS NOT NULL)
              AND COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) BETWEEN ? AND ?
            ORDER BY date_iso ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "date_iso": r["date_iso"],
                    "hr": _safe_float(r["hr"]),
                    "rmssd": _safe_float(r["rmssd"]),
                    "sickness": _safe_int(r["sickness"]),
                    "sleep_quality": _safe_float(r["sleep_quality"]),
                    "training_motivation": _safe_float(r["training_motivation"]),
                }
            )
        return out
    finally:
        conn.close()


def get_hrv_signals(day: str, lookback: int = 14) -> dict:
    day_iso = _day_iso(day)
    # baseline: 28 days before day-1
    d = datetime.strptime(day_iso, "%Y-%m-%d").date()
    baseline_start = (d - timedelta(days=28)).isoformat()
    baseline_end = (d - timedelta(days=1)).isoformat()

    conn = get_hrv_db()
    conn.row_factory = sqlite3.Row
    try:
        today_row = conn.execute(
            """
            SELECT
              COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) AS date_iso,
              hr, rmssd, sickness, sleep_quality, training_motivation
            FROM hrv_measurements
            WHERE COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) = ?
            ORDER BY COALESCE(ts_measurement, date_utc) DESC
            LIMIT 1
            """,
            (day_iso,),
        ).fetchone()

        base_rows = conn.execute(
            """
            SELECT
              COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) AS date_iso,
              hr, rmssd, sleep_quality, training_motivation
            FROM hrv_measurements
            WHERE (date_utc IS NOT NULL OR ts_measurement IS NOT NULL)
              AND COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) BETWEEN ? AND ?
            ORDER BY date_iso ASC
            """,
            (baseline_start, baseline_end),
        ).fetchall()

        baseline_rmssd = _mean([_safe_float(r["rmssd"]) for r in base_rows])
        baseline_hr = _mean([_safe_float(r["hr"]) for r in base_rows])
        baseline_sleep = _mean([_safe_float(r["sleep_quality"]) for r in base_rows])
        baseline_ready = _mean([_safe_float(r["training_motivation"]) for r in base_rows])

        recent_rows = _fetch_hrv_rows(day_iso, lookback)
        last3 = [r for r in recent_rows if r["rmssd"] is not None][-3:]
        rmssd_3d = _mean([r["rmssd"] for r in last3]) if last3 else None

        if not today_row:
            return {
                "available": False,
                "day": day_iso,
                "baseline": {"rmssd": baseline_rmssd, "hr": baseline_hr, "sleep_quality": baseline_sleep, "ready": baseline_ready},
                "today": None,
                "actions": [],
            }

        today_rmssd = _safe_float(today_row["rmssd"])
        today_hr = _safe_float(today_row["hr"])
        today_sleep = _safe_float(today_row["sleep_quality"])
        today_ready = _safe_float(today_row["training_motivation"])
        sickness = _safe_int(today_row["sickness"]) or 0

        actions = []
        evidence_base = {
            "rmssd_today": today_rmssd,
            "rmssd_baseline": baseline_rmssd,
            "rmssd_3d": rmssd_3d,
            "hr_today": today_hr,
            "hr_baseline": baseline_hr,
            "sleep_today": today_sleep,
            "sleep_baseline": baseline_sleep,
            "ready_today": today_ready,
            "ready_baseline": baseline_ready,
            "sickness": sickness,
        }

        if sickness:
            actions.append(("SICKNESS_MARKED", "stop", "bad", evidence_base))

        ratio = (today_rmssd / baseline_rmssd) if (today_rmssd is not None and baseline_rmssd not in (None, 0)) else None
        hr_delta = (today_hr - baseline_hr) if (today_hr is not None and baseline_hr is not None) else None
        sleep_delta = (today_sleep - baseline_sleep) if (today_sleep is not None and baseline_sleep is not None) else None

        if ratio is not None:
            if ratio <= 0.92:
                actions.append(("HRV_RMSSD_BELOW_BASELINE", "warn", "bad", {**evidence_base, "rmssd_ratio": ratio}))
            elif ratio >= 1.08:
                actions.append(("HRV_RMSSD_ABOVE_BASELINE", "info", "good", {**evidence_base, "rmssd_ratio": ratio}))

        if hr_delta is not None and hr_delta >= 4.0:
            actions.append(("RHR_ABOVE_NORMAL", "warn", "bad", {**evidence_base, "hr_delta": hr_delta}))

        if rmssd_3d is not None and baseline_rmssd not in (None, 0) and (rmssd_3d / baseline_rmssd) <= 0.92:
            actions.append(("RMSSD_DOWN_MULTI_DAY", "alert", "bad", {**evidence_base, "rmssd_3d_ratio": rmssd_3d / baseline_rmssd}))

        if ratio is not None and hr_delta is not None:
            if ratio >= 1.05 and hr_delta >= 3.0:
                actions.append(("HRV_CONFLICT_RMSSD_RHR", "warn", "bad", {**evidence_base, "rmssd_ratio": ratio, "hr_delta": hr_delta}))

        if today_sleep is not None:
            if (baseline_sleep is not None and sleep_delta is not None and sleep_delta <= -0.8) or today_sleep <= 6.5:
                actions.append(("SLEEP_QUALITY_LOW", "alert" if (ratio is not None and ratio <= 0.92) else "warn", "bad", {**evidence_base, "sleep_delta": sleep_delta}))
            elif (baseline_sleep is not None and sleep_delta is not None and sleep_delta >= 0.8) or today_sleep >= 8.3:
                actions.append(("SLEEP_QUALITY_HIGH", "info", "good", {**evidence_base, "sleep_delta": sleep_delta}))

        # wakeup variance (from ts_measurement HH:MM), similar to terminal_api
        try:
            start_iso, end_iso = _date_window(day_iso, lookback)
            rows_ts = conn.execute(
                """
                SELECT
                  COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) AS day,
                  substr(ts_measurement, 12, 5) AS hhmm,
                  COALESCE(source_file, '') AS source_file,
                  ts_measurement AS ts
                FROM hrv_measurements
                WHERE ts_measurement IS NOT NULL
                  AND length(substr(ts_measurement, 12, 5)) = 5
                  AND substr(ts_measurement, 12, 5) != '00:01'
                  AND COALESCE(substr(date_utc, 1, 10), substr(ts_measurement, 1, 10)) BETWEEN ? AND ?
                """,
                (start_iso, end_iso),
            ).fetchall()

            by_day = {}
            for r in rows_ts:
                dy = str(r["day"] or "").strip()
                hhmm = str(r["hhmm"] or "").strip()
                if not dy or not hhmm:
                    continue
                src = str(r["source_file"] or "")
                ts = str(r["ts"] or "")
                is_kub = ("kubios" in src.lower())
                cur = by_day.get(dy)
                if cur is None:
                    by_day[dy] = {"hhmm": hhmm, "is_kub": is_kub, "ts": ts}
                    continue
                if (not cur["is_kub"]) and is_kub:
                    by_day[dy] = {"hhmm": hhmm, "is_kub": is_kub, "ts": ts}
                    continue
                if cur["is_kub"] == is_kub and ts > cur["ts"]:
                    by_day[dy] = {"hhmm": hhmm, "is_kub": is_kub, "ts": ts}

            minutes = []
            for _, it in sorted(by_day.items()):
                hhmm = it["hhmm"]
                try:
                    hh, mm = hhmm.split(":")
                    m = (int(hh) * 60 + int(mm)) % (24 * 60)
                except Exception:
                    continue
                if it["is_kub"]:
                    m = (m + 60) % (24 * 60)
                m = (m - 10) % (24 * 60)
                minutes.append(int(m))

            if len(minutes) >= 7:
                mean_m = sum(minutes) / len(minutes)
                var = sum((x - mean_m) ** 2 for x in minutes) / len(minutes)
                std_min = float(var ** 0.5)
                hh = int(mean_m) // 60
                mm = int(mean_m) % 60
                hhmm_avg = f"{hh:02d}:{mm:02d}"
                if std_min >= 60.0:
                    actions.append(("WAKE_TIME_VARIANCE_HIGH", "warn", "bad", {**evidence_base, "wakeup_avg": hhmm_avg, "wakeup_variance_min": std_min}))
        except Exception:
            pass

        return {
            "available": True,
            "day": day_iso,
            "baseline": {"rmssd": baseline_rmssd, "hr": baseline_hr, "sleep_quality": baseline_sleep, "ready": baseline_ready},
            "today": {"rmssd": today_rmssd, "hr": today_hr, "sleep_quality": today_sleep, "ready": today_ready, "sickness": sickness},
            "actions": actions,
        }
    finally:
        conn.close()


def get_nutrition_signals(day: str, lookback: int = 14) -> dict:
    day_iso = _day_iso(day)
    start_iso, end_iso = _date_window(day_iso, lookback)
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date_iso, kcal, protein, sugar, weight_kg
            FROM nutrition_daily
            WHERE date_iso BETWEEN ? AND ?
            ORDER BY date_iso ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
        if not rows:
            return {"available": False, "day": day_iso, "actions": [], "today": None, "baseline": None}

        series = [{"date_iso": r["date_iso"], "kcal": _safe_float(r["kcal"]), "protein": _safe_float(r["protein"]), "sugar": _safe_float(r["sugar"]), "weight": _safe_float(r["weight_kg"])} for r in rows]
        today = next((r for r in series if r["date_iso"] == day_iso), series[-1] if series else None)

        kcals = [r["kcal"] for r in series if r["kcal"] is not None]
        proteins = [r["protein"] for r in series if r["protein"] is not None]
        sugars = [r["sugar"] for r in series if r["sugar"] is not None]
        weights = [r["weight"] for r in series if r["weight"] is not None]

        actions = []
        evidence = {
            "kcal_today": today.get("kcal") if today else None,
            "protein_today": today.get("protein") if today else None,
            "sugar_today": today.get("sugar") if today else None,
            "weight_today": today.get("weight") if today else None,
            "kcal_avg_7d": None,
            "kcal_avg_prev7d": None,
            "kcal_std_7d": None,
            "protein_avg_7d": None,
            "sugar_avg_7d": None,
        }

        # 7d windows
        series7 = series[-7:]
        series_prev7 = series[-14:-7] if len(series) >= 14 else []

        kcal_avg_7 = _mean([r["kcal"] for r in series7])
        kcal_avg_prev7 = _mean([r["kcal"] for r in series_prev7])
        kcal_std_7 = _std([r["kcal"] for r in series7])
        protein_avg_7 = _mean([r["protein"] for r in series7])
        sugar_avg_7 = _mean([r["sugar"] for r in series7])

        evidence.update(
            {
                "kcal_avg_7d": kcal_avg_7,
                "kcal_avg_prev7d": kcal_avg_prev7,
                "kcal_std_7d": kcal_std_7,
                "protein_avg_7d": protein_avg_7,
                "sugar_avg_7d": sugar_avg_7,
            }
        )

        if kcal_avg_7 is not None and kcal_avg_prev7 is not None:
            diff = kcal_avg_7 - kcal_avg_prev7
            if diff >= 250:
                actions.append(("CAL_SURPLUS_TREND_UP", "warn", "bad", {**evidence, "kcal_diff_7d": diff}))
            elif diff <= -250:
                actions.append(("CAL_DEFICIT_TREND_DOWN", "warn", "bad", {**evidence, "kcal_diff_7d": diff}))

        if kcal_std_7 is not None and kcal_std_7 >= 500:
            actions.append(("CAL_VARIANCE_HIGH", "warn", "bad", evidence))

        # protein thresholds: use weight if available, else fallback
        w = _mean(weights[-7:]) if weights else None
        low_thr = (w * 1.6) if w is not None else 120.0
        if protein_avg_7 is not None and protein_avg_7 < low_thr:
            actions.append(("PROTEIN_LOW", "warn", "bad", {**evidence, "protein_low_thr": low_thr}))

        if sugar_avg_7 is not None and sugar_avg_7 >= 90:
            actions.append(("SUGAR_HIGH_CHRONIC", "warn", "bad", evidence))

        return {"available": True, "day": day_iso, "today": today, "baseline": None, "actions": actions}
    finally:
        conn.close()


def get_bodyweight_signals(day: str, lookback: int = 14) -> dict:
    day_iso = _day_iso(day)
    start_iso, end_iso = _date_window(day_iso, lookback)
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT date_iso, weight_kg AS weight
            FROM nutrition_daily
            WHERE date_iso BETWEEN ? AND ? AND weight_kg IS NOT NULL
            ORDER BY date_iso ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
        if not rows or len(rows) < 5:
            return {"available": False, "day": day_iso, "actions": [], "trend": None}
        ws = [float(r["weight"]) for r in rows if r["weight"] is not None]
        delta = _lin_trend_delta(ws)
        noise = _std(ws)

        # infer goal from active_mode
        mode = "maintenance"
        try:
            s = _load_nutrition_active_mode()
            if s:
                mode = s
        except Exception:
            mode = "maintenance"

        evidence = {"weight_start": ws[0] if ws else None, "weight_end": ws[-1] if ws else None, "trend_delta": delta, "noise_std": noise, "active_mode": mode}

        actions = []
        if delta is not None:
            if mode in ("cut", "maintenance") and delta >= 0.6:
                actions.append(("BW_TREND_UP_WRONG_GOAL", "warn", "bad", evidence))
            if mode in ("lean_bulk",) and delta <= -0.6:
                actions.append(("BW_TREND_DOWN_WRONG_GOAL", "warn", "bad", evidence))

        if delta is not None and abs(delta) <= 0.4 and noise is not None and noise >= 1.0:
            actions.append(("BW_NOISE_HIGH_TREND_STABLE", "info", "good", evidence))

        return {"available": True, "day": day_iso, "trend": delta, "noise": noise, "mode": mode, "actions": actions}
    finally:
        conn.close()


def _load_nutrition_active_mode() -> str | None:
    conn = get_nutrition_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM nutrition_settings WHERE key='active_mode' LIMIT 1").fetchone()
        if not row:
            row = conn.execute("SELECT value FROM nutrition_settings WHERE key='selected_mode' LIMIT 1").fetchone()
        if not row:
            return None
        v = (row["value"] or "").strip()
        return v or None
    finally:
        conn.close()


def get_planning_signals(day: str, lookback: int = 14) -> dict:
    day_iso = _day_iso(day)
    actions = []
    evidence = {"missed_7d": None, "planned_7d": None, "done_7d": None, "active_plans": None, "override": None}
    plan_conn = get_plans_db()
    plan_conn.row_factory = sqlite3.Row
    try:
        active_plans = plan_conn.execute("SELECT COUNT(*) AS c FROM plans WHERE is_active = 1").fetchone()["c"]
        evidence["active_plans"] = int(active_plans)
        if active_plans and int(active_plans) > 1:
            actions.append(("MULTIPLE_ACTIVE_PLANS", "warn", "bad", dict(evidence)))

        plan_row = plan_conn.execute("SELECT * FROM plans WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not plan_row:
            return {"available": False, "day": day_iso, "actions": actions, "evidence": evidence}
        plan = json.loads(plan_row["data"]) if plan_row["data"] else {}
        meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
        override = bool(meta.get("week_override") is not None or meta.get("week_type_override"))
        evidence["override"] = override
        if override:
            actions.append(("OVERRIDES_HIGH", "warn", "bad", dict(evidence)))

        base_week = plan.get("base_week") or []
        if not base_week:
            return {"available": False, "day": day_iso, "actions": actions, "evidence": evidence}

        # planned sessions in last 7 days vs actual logs
        d0 = datetime.strptime(day_iso, "%Y-%m-%d").date()
        planned = 0
        for i in range(7):
            dt = d0 - timedelta(days=i)
            idx = dt.weekday()
            day_plan = base_week[idx] if idx < len(base_week) else {}
            has_gym = bool((day_plan.get("strength_exercises") or []) or (day_plan.get("session_name") or "").strip())
            run_sessions = day_plan.get("run_sessions") or []
            has_run = bool(run_sessions and isinstance(run_sessions, list) and (run_sessions[0].get("run_type") or run_sessions[0].get("amount_value")))
            planned += 1 if (has_gym or has_run) else 0

        # done sessions
        done = _count_done_sessions_7d(day_iso)
        missed = max(0, planned - done)
        evidence.update({"planned_7d": planned, "done_7d": done, "missed_7d": missed})

        if planned >= 3 and missed >= 2:
            actions.append(("MISSED_SESSIONS_UP", "warn", "bad", dict(evidence)))
        if planned >= 3 and missed == 0 and done >= 3:
            actions.append(("STREAK_GOOD", "info", "good", dict(evidence)))

        return {"available": True, "day": day_iso, "actions": actions, "evidence": evidence}
    except Exception:
        return {"available": False, "day": day_iso, "actions": actions, "evidence": evidence}
    finally:
        plan_conn.close()


def _count_done_sessions_7d(day_iso: str) -> int:
    d0 = datetime.strptime(day_iso, "%Y-%m-%d").date()
    start = (d0 - timedelta(days=6)).isoformat()
    done = 0
    # strength
    tconn = get_training_db()
    tconn.row_factory = sqlite3.Row
    try:
        rows = tconn.execute(
            "SELECT COUNT(DISTINCT date_iso) AS c FROM workouts WHERE date_iso BETWEEN ? AND ?",
            (start, day_iso),
        ).fetchone()
        done += int(rows["c"] or 0)
    finally:
        tconn.close()
    # runs
    rconn = get_runs_db()
    rconn.row_factory = sqlite3.Row
    try:
        rows = rconn.execute(
            "SELECT COUNT(DISTINCT substr(date, 1, 10)) AS c FROM runs WHERE substr(date, 1, 10) BETWEEN ? AND ?",
            (start, day_iso),
        ).fetchone()
        done += int(rows["c"] or 0)
    finally:
        rconn.close()
    return done


def get_strength_signals(day: str, lookback: int = 28) -> dict:
    day_iso = _day_iso(day)
    d = datetime.strptime(day_iso, "%Y-%m-%d").date()
    start = (d - timedelta(days=lookback - 1)).isoformat()
    tconn = get_training_db()
    tconn.row_factory = sqlite3.Row
    try:
        rows = tconn.execute(
            """
            SELECT w.date_iso AS date_iso, s.reps, s.weight, s.rpe
            FROM sets s
            JOIN workouts w ON w.id = s.workout_id
            WHERE w.date_iso BETWEEN ? AND ?
            """,
            (start, day_iso),
        ).fetchall()
        if not rows:
            return {"available": False, "day": day_iso, "actions": []}

        # split last 14 vs prev 14
        cut = d - timedelta(days=13)
        cur_tonnage = 0.0
        prev_tonnage = 0.0
        for r in rows:
            dt = r["date_iso"]
            reps = _safe_int(r["reps"])
            w = _safe_float(r["weight"])
            if reps is None or w is None:
                continue
            ton = float(reps) * float(w)
            if dt >= cut.isoformat():
                cur_tonnage += ton
            else:
                prev_tonnage += ton

        evidence = {"tonnage_14d": cur_tonnage, "tonnage_prev14d": prev_tonnage}
        actions = []
        if prev_tonnage > 0:
            pct = (cur_tonnage - prev_tonnage) / prev_tonnage
            evidence["tonnage_pct"] = pct
            if pct >= 0.12:
                actions.append(("TONNAGE_UP_STRONG", "info", "good", dict(evidence)))
            elif pct <= -0.12:
                actions.append(("TONNAGE_DOWN_STRONG", "warn", "bad", dict(evidence)))
        return {"available": True, "day": day_iso, "actions": actions, "evidence": evidence}
    finally:
        tconn.close()


def get_running_signals(day: str, lookback: int = 28) -> dict:
    day_iso = _day_iso(day)
    d = datetime.strptime(day_iso, "%Y-%m-%d").date()
    start = (d - timedelta(days=lookback - 1)).isoformat()
    conn = get_runs_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT substr(date, 1, 10) AS date_iso, distance, avg_speed, avg_hr
            FROM runs
            WHERE substr(date, 1, 10) BETWEEN ? AND ?
            ORDER BY date_iso ASC
            """,
            (start, day_iso),
        ).fetchall()
        if not rows:
            return {"available": False, "day": day_iso, "actions": []}

        cut = d - timedelta(days=13)
        cur_eff = []
        prev_eff = []
        for r in rows:
            dt = r["date_iso"]
            speed = _safe_float(r["avg_speed"])
            hr = _safe_float(r["avg_hr"])
            if speed is None or hr in (None, 0):
                continue
            eff = speed / hr
            if dt >= cut.isoformat():
                cur_eff.append(eff)
            else:
                prev_eff.append(eff)

        cur = _mean(cur_eff)
        prev = _mean(prev_eff)
        evidence = {"eff_14d": cur, "eff_prev14d": prev}
        actions = []
        if cur is not None and prev not in (None, 0):
            pct = (cur - prev) / prev
            evidence["eff_pct"] = pct
            if pct >= 0.05:
                actions.append(("RUN_EFFICIENCY_UP", "info", "good", dict(evidence)))
            elif pct <= -0.05:
                actions.append(("RUN_EFFICIENCY_DOWN", "warn", "bad", dict(evidence)))
        return {"available": True, "day": day_iso, "actions": actions, "evidence": evidence}
    finally:
        conn.close()


def _status_bucket(kind: str, signals: dict) -> str:
    if not signals.get("available"):
        return "mixed" if kind in ("recovery", "nutrition") else "ok"
    # simple heuristics based on actions
    acts = signals.get("actions") or []
    bad = [a for a in acts if len(a) >= 3 and a[2] == "bad"]
    good = [a for a in acts if len(a) >= 3 and a[2] == "good"]
    if kind == "recovery":
        if any(a[0] == "SICKNESS_MARKED" for a in acts):
            return "low"
        if any(a[1] in ("alert", "stop") for a in bad):
            return "low"
        if bad:
            return "mixed"
        return "good" if good else "mixed"
    if kind == "nutrition":
        if any(a[1] == "warn" for a in bad):
            return "mixed"
        return "ok" if not bad else "off"
    if kind == "planning":
        return "off" if bad else "ok"
    if kind == "load":
        return "ok"
    return "ok"


def _select_domain_items(acts: List[Tuple[str, str, str, dict]], *, target: str, limit_good: int = 2, limit_bad: int = 2):
    good = [a for a in acts if a[2] == "good"]
    bad = [a for a in acts if a[2] == "bad"]
    # severity order for bad: stop > alert > warn > info
    sev_rank = {"stop": 3, "alert": 2, "warn": 1, "info": 0}
    bad.sort(key=lambda x: sev_rank.get(x[1], 0), reverse=True)
    good.sort(key=lambda x: sev_rank.get(x[1], 0), reverse=True)
    return good[:limit_good], bad[:limit_bad]


def _decision_from_actions(all_actions: List[Tuple[str, str, str, dict]]) -> Tuple[str, dict]:
    # Decision must not be driven by sleep-quality (user asked); sleep stays as normal good/bad item.
    by_id = {a[0]: a for a in all_actions}
    def has(x): return x in by_id

    if has("SICKNESS_MARKED"):
        mode = "STOP"
        driver = "SICKNESS_MARKED"
    elif has("RMSSD_DOWN_MULTI_DAY"):
        mode = "EASY_DOWN"
        driver = "RMSSD_DOWN_MULTI_DAY"
    elif has("HRV_RMSSD_BELOW_BASELINE") or has("RHR_ABOVE_NORMAL"):
        mode = "EASY_DOWN"
        driver = "HRV_RMSSD_BELOW_BASELINE" if has("HRV_RMSSD_BELOW_BASELINE") else "RHR_ABOVE_NORMAL"
    elif has("HRV_CONFLICT_RMSSD_RHR"):
        mode = "NORMAL"
        driver = "HRV_CONFLICT_RMSSD_RHR"
    elif has("HRV_RMSSD_ABOVE_BASELINE"):
        mode = "PUSH"
        driver = "HRV_RMSSD_ABOVE_BASELINE"
    else:
        mode = "NORMAL"
        driver = next(iter(by_id.keys()), None)

    # numeric params get overridden from active plan week later (if available)
    params = {
        "STOP": {"intensity_factor": 0.85, "volume_factor": 0.70, "rpe_cap": 7.0},
        "EASY_DOWN": {"intensity_factor": 0.92, "volume_factor": 0.85, "rpe_cap": 8.0},
        "NORMAL": {"intensity_factor": 1.00, "volume_factor": 1.00, "rpe_cap": 9.0},
        "PUSH": {"intensity_factor": 1.02, "volume_factor": 1.00, "rpe_cap": 9.0},
    }
    return mode, {"driver_action_id": driver, **params.get(mode, params["NORMAL"])}


def _plan_week_adjustments(day_iso: str) -> dict | None:
    """
    Pull RPE cap + volume factor + intensity factor from active plan week.
    Mirrors plan week logic used by plan-check overview.
    """
    day_iso = _day_iso(day_iso)
    conn = get_plans_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT data FROM plans WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row or not row["data"]:
            return None
        try:
            plan = json.loads(row["data"])
        except Exception:
            return None

        meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
        start_date_iso = meta.get("start_date")
        if not start_date_iso:
            return None
        try:
            start_d = datetime.strptime(start_date_iso, "%Y-%m-%d").date()
        except Exception:
            return None

        asof = datetime.strptime(day_iso, "%Y-%m-%d").date()
        weekday = start_d.weekday()
        week1_start = start_d if weekday == 0 else (start_d + timedelta(days=(7 - weekday)))
        if asof < week1_start:
            week_current = 1
        else:
            week_current = ((asof - week1_start).days // 7) + 1
        if meta.get("week_override") is not None:
            try:
                week_current = int(meta["week_override"])
            except Exception:
                pass

        block_length = int(meta.get("block_length") or plan.get("block_length") or 4)
        week_in_block = ((week_current - 1) % block_length) + 1

        wto = meta.get("week_type_override")
        if wto:
            week_type = str(wto).strip().lower()
        else:
            if week_in_block == block_length:
                week_type = "deload"
            elif week_in_block == (block_length - 1):
                week_type = "overreach"
            else:
                week_type = "build"

        weeks = plan.get("weeks") if isinstance(plan.get("weeks"), list) else []
        week_meta = weeks[week_in_block - 1] if (weeks and 1 <= week_in_block <= len(weeks) and isinstance(weeks[week_in_block - 1], dict)) else {}

        def _finite_num(val):
            try:
                v = float(val)
                return v
            except Exception:
                return None

        # volume factor: strength_volume_factor (week) * type multiplier (meta)
        plan_vol = _finite_num(week_meta.get("strength_volume_factor"))
        defaults_vol = {"build": 1.0, "overreach": 1.0, "deload": 0.7}
        vol_by_type = meta.get("volume_mult_by_type") if isinstance(meta.get("volume_mult_by_type"), dict) else {
            "build": meta.get("build_volume_mult", defaults_vol["build"]),
            "overreach": meta.get("overreach_volume_mult", defaults_vol["overreach"]),
            "deload": meta.get("deload_volume_mult", defaults_vol["deload"]),
        }
        type_vol = _finite_num(vol_by_type.get(week_type))
        base_vol = plan_vol if plan_vol is not None else 1.0
        vol = base_vol * (type_vol if type_vol is not None else 1.0)

        # rpe cap: min(week cap, type cap)
        plan_cap = _finite_num(week_meta.get("rpe_cap"))
        defaults_rpe = {"build": 9.0, "overreach": 9.5, "deload": 6.5}
        rpe_by_type = meta.get("rpe_cap_by_type") if isinstance(meta.get("rpe_cap_by_type"), dict) else {
            "build": meta.get("build_rpe_cap", defaults_rpe["build"]),
            "overreach": meta.get("overreach_rpe_cap", defaults_rpe["overreach"]),
            "deload": meta.get("deload_rpe_cap", defaults_rpe["deload"]),
        }
        type_cap = _finite_num(rpe_by_type.get(week_type))
        if plan_cap is not None and type_cap is not None:
            cap = min(plan_cap, type_cap)
        else:
            cap = plan_cap if plan_cap is not None else type_cap

        # intensity factor: optional week value, else type default / meta override
        defaults_int = {"build": 1.0, "overreach": 1.02, "deload": 0.92}
        inten_by_type = meta.get("intensity_mult_by_type") if isinstance(meta.get("intensity_mult_by_type"), dict) else {
            "build": meta.get("build_intensity_mult", defaults_int["build"]),
            "overreach": meta.get("overreach_intensity_mult", defaults_int["overreach"]),
            "deload": meta.get("deload_intensity_mult", defaults_int["deload"]),
        }
        week_int = _finite_num(week_meta.get("intensity_factor") or week_meta.get("strength_intensity_factor"))
        type_int = _finite_num(inten_by_type.get(week_type))
        intensity = (week_int if week_int is not None else 1.0) * (type_int if type_int is not None else 1.0)

        return {
            "week_type": week_type,
            "week_in_block": week_in_block,
            "volume_factor": float(vol),
            "rpe_cap": float(cap) if cap is not None else None,
            "intensity_factor": float(intensity),
            "source": "active_plan_week",
        }
    finally:
        conn.close()


def build_daily(day: str, *, force: bool = False) -> dict:
    day_iso = _day_iso(day)
    ensure_kienzl_schema_and_seed()

    if not force:
        cached = load_daily_cached(day_iso)
        if cached:
            return cached

    hrv = get_hrv_signals(day_iso, lookback=14)
    nutrition = get_nutrition_signals(day_iso, lookback=14)
    bw = get_bodyweight_signals(day_iso, lookback=14)
    planning = get_planning_signals(day_iso, lookback=14)
    strength = get_strength_signals(day_iso, lookback=28)
    running = get_running_signals(day_iso, lookback=28)

    all_actions: List[Tuple[str, str, str, dict]] = []
    for src in (hrv, nutrition, bw, planning, strength, running):
        acts = src.get("actions") or []
        for a in acts:
            if isinstance(a, (list, tuple)) and len(a) >= 4:
                all_actions.append((a[0], a[1], a[2], a[3]))

    mode, dec = _decision_from_actions(all_actions)
    decision_reason_id = f"KIENZL_DECISION_{mode}"
    driver_action_id = dec.get("driver_action_id")
    plan_adj = _plan_week_adjustments(day_iso)

    good_items = []
    bad_items = []

    # group by domain using action definition
    domain_map: Dict[str, List[Tuple[str, str, str, dict]]] = {}
    for a in all_actions:
        aid = a[0]
        dom = next((x.domain for x in CORE_ACTIONS if x.id == aid), "global")
        domain_map.setdefault(dom, []).append(a)

    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        ensure_kienzl_schema(conn)
        for dom, acts in domain_map.items():
            if decision_reason_id:
                acts = [a for a in (acts or []) if a and a[0] != decision_reason_id]
            g, b = _select_domain_items(acts, target=dom, limit_good=2, limit_bad=2)
            for (aid, sev, kind, ev) in g:
                pid, text = _pick_phrase_for_day(conn, aid, day_iso)
                if text:
                    good_items.append({"action_id": aid, "phrase_id": pid, "text": text, "severity": sev, "domain": dom, "evidence": ev})
            for (aid, sev, kind, ev) in b:
                pid, text = _pick_phrase_for_day(conn, aid, day_iso)
                if text:
                    bad_items.append({"action_id": aid, "phrase_id": pid, "text": text, "severity": sev, "domain": dom, "evidence": ev})

        reason_id = decision_reason_id
        dec_phrase_id, dec_text = _pick_phrase_for_day(conn, reason_id, day_iso) if reason_id else (None, None)
        # numeric params: prefer active plan week
        intensity_factor = (plan_adj.get("intensity_factor") if isinstance(plan_adj, dict) else None) or dec.get("intensity_factor")
        volume_factor = (plan_adj.get("volume_factor") if isinstance(plan_adj, dict) else None) or dec.get("volume_factor")
        rpe_cap = (plan_adj.get("rpe_cap") if isinstance(plan_adj, dict) else None) if (isinstance(plan_adj, dict) and ("rpe_cap" in plan_adj)) else dec.get("rpe_cap")
        decision = {
            "mode": mode,
            "intensity_factor": float(intensity_factor) if intensity_factor is not None else 1.0,
            "volume_factor": float(volume_factor) if volume_factor is not None else 1.0,
            "rpe_cap": float(rpe_cap) if rpe_cap is not None else None,
            "reason_action_id": reason_id,
            "phrase_id": dec_phrase_id,
            "text": dec_text or "",
            "evidence": (next((a[3] for a in all_actions if a[0] == driver_action_id), {}) if driver_action_id else {}),
        }

        payload = {
            "day": day_iso,
            "status": {
                "recovery": _status_bucket("recovery", hrv),
                "load": _status_bucket("load", strength),
                "nutrition": _status_bucket("nutrition", nutrition),
                "planning": _status_bucket("planning", planning),
            },
            "good": good_items[:6],
            "bad": bad_items[:6],
            "decision": decision,
            "meta": {"generated_at": _utc_iso(), "version": KIENZL_VERSION},
            "available": {
                "recovery": bool(hrv.get("available")),
                "nutrition": bool(nutrition.get("available")),
                "bodyweight": bool(bw.get("available")),
                "planning": bool(planning.get("available")),
                "strength": bool(strength.get("available")),
                "running": bool(running.get("available")),
            },
        }
    finally:
        conn.close()

    save_daily(day_iso, payload)
    return payload


def get_decision(day: str) -> dict | None:
    d = load_daily_cached(day)
    if d and isinstance(d.get("decision"), dict):
        return d.get("decision")
    d = build_daily(day, force=False)
    if isinstance(d.get("decision"), dict):
        return d.get("decision")
    return None


def get_adjustments(day: str) -> dict | None:
    dec = get_decision(day)
    if not dec:
        return None
    return {
        "mode": dec.get("mode"),
        "intensity_factor": dec.get("intensity_factor"),
        "volume_factor": dec.get("volume_factor"),
        "rpe_cap": dec.get("rpe_cap"),
        "source": "kienzl_daily",
        "day": _day_iso(day),
    }


def refresh_needed_actions(since_ts: str | None, *, limit: int = 64) -> List[str]:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        ensure_kienzl_schema(conn)
        if not since_ts:
            rows = conn.execute("SELECT DISTINCT action_id FROM kienzl_usage ORDER BY action_id LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT action_id FROM kienzl_usage WHERE ts >= ? ORDER BY action_id LIMIT ?",
                (since_ts, limit),
            ).fetchall()
        out = []
        for r in rows or []:
            try:
                aid = r["action_id"]
            except Exception:
                aid = None
            if aid:
                out.append(str(aid))
        return out
    finally:
        conn.close()


def get_state(key: str) -> str | None:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        ensure_kienzl_schema(conn)
        row = conn.execute("SELECT value FROM kienzl_state WHERE key=?", (key,)).fetchone()
        return (row["value"] if row else None)
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        conn.execute(
            "INSERT INTO kienzl_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def count_active_phrases(action_id: str) -> int:
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM kienzl_phrases WHERE action_id=? AND is_active=1",
            (action_id,),
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def insert_phrases(action_id: str, texts: List[str]) -> int:
    if not texts:
        return 0
    conn = get_training_db()
    try:
        ensure_kienzl_schema(conn)
        now = _utc_iso()
        cur = conn.cursor()
        n = 0
        for t in texts:
            txt = (t or "").strip()
            if not txt:
                continue
            cur.execute(
                "INSERT INTO kienzl_phrases (action_id, text, created_at, used_count, last_used_at, is_active) VALUES (?, ?, ?, 0, NULL, 1)",
                (action_id, txt, now),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def last_texts_for_action(action_id: str, n: int = 5) -> List[str]:
    conn = get_training_db()
    conn.row_factory = sqlite3.Row
    try:
        ensure_kienzl_schema(conn)
        rows = conn.execute(
            """
            SELECT text
            FROM kienzl_phrases
            WHERE action_id = ?
            ORDER BY last_used_at DESC, used_count DESC, id DESC
            LIMIT ?
            """,
            (action_id, int(n)),
        ).fetchall()
        return [(r["text"] or "").strip() for r in rows if r and (r["text"] or "").strip()]
    finally:
        conn.close()
