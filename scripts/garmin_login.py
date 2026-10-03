from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from garminconnect import Garmin

from garmin_sync.service import TOKENSTORE_PATH, sync_garmin_to_database


def main() -> int:
    os.umask(0o077)
    TOKENSTORE_PATH.mkdir(parents=True, exist_ok=True, mode=0o700)
    email = input("Garmin-E-Mail: ").strip()
    password = getpass.getpass("Garmin-Passwort: ")
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin-MFA-Code: ").strip(),
    )
    client.login(str(TOKENSTORE_PATH))
    for token_file in TOKENSTORE_PATH.iterdir():
        if token_file.is_file():
            token_file.chmod(0o600)
    print("Garmin erfolgreich verbunden. Das Passwort wurde nicht gespeichert.")
    result = sync_garmin_to_database()
    print(f"Erster Abruf abgeschlossen: {result.get('imported', 0)} neue Aktivitaeten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
