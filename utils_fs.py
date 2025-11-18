"""
utils_fs.py
============

Zbiór pomocniczych funkcji obsługujących system plików: tworzenie
katalogów, generowanie łańcucha z aktualną datą/godziną oraz zapis
danych do plików CSV.  Funkcje te są wykorzystywane przez główną
aplikację do utrzymania struktury katalogów i logowania zdarzeń.
"""

import os
import json
from datetime import datetime
from config import CONFIG


def ensure_dirs():
    """Utwórz katalogi wymagane przez aplikację jeżeli ich nie ma."""
    for katalog in [CONFIG["data_dir"], CONFIG["faces_dir"], CONFIG["index_dir"], CONFIG["logs_dir"]]:
        os.makedirs(katalog, exist_ok=True)
    # Upewnij się, że plik employees.json istnieje
    if not os.path.exists(CONFIG["employees_json"]):
        with open(CONFIG["employees_json"], "w", encoding="utf-8") as f:
            json.dump({"employees": []}, f, ensure_ascii=False, indent=2)


def now_str() -> str:
    """Zwróć aktualną datę i godzinę jako łańcuch w formacie HH:MM DD.MM.YYYY."""
    return datetime.now().strftime("%H:%M %d.%m.%Y")


def log_csv(path: str, header: list, row_values: list):
    """
    Dodaj wiersz do pliku CSV.  Jeśli plik nie istnieje, zapisz
    najpierw nagłówek z listy ``header``.
    Wartości w ``row_values`` zostaną zamienione na ciąg znaków i
    rozdzielone średnikami.
    """
    istnialo = os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if not istnialo:
            f.write(";".join(header) + "\n")
        f.write(";".join(map(str, row_values)) + "\n")