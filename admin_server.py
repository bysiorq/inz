"""
Simple administrator web server for the breathalyzer system.

This module starts a Flask-based HTTP server which provides a minimal
admin panel. The panel allows an authorised administrator to log in,
view a table of employee entry measurements and add new employees to
the local JSON database. All traffic is served over plain HTTP and
does not require any SSL certificates.

If the pymongo library is installed the server persists measurement logs
to a MongoDB instance. Records include the employee's identifier, pin,
full name, recorded BAC and the timestamp of the measurement. When
pymongo is unavailable the schedule is populated from the local CSV logs.
The admin panel renders these records in a simple HTML table and
exposes a form for adding new employees. When a new employee is created
the system generates a unique four-digit pin which has not previously been
assigned and appends the new record to the employees JSON file.
"""

from flask import Flask, request, redirect, url_for, session, render_template_string

from collections import deque
from datetime import datetime
import json
import os

# Import Mongo client for logging to the database
try:
    from pymongo import MongoClient  # type: ignore
except Exception:
    MongoClient = None  # if PyMongo is unavailable we fall back to CSV only

# Prosty globalny klient + flaga wyłączenia (żeby nie blokować GUI przy każdym logowaniu)
_MONGO_CLIENT = None
_MONGO_DISABLED = False


from config import CONFIG

# ---------------------------------------------------------------------
# Flask setup
# ---------------------------------------------------------------------

app = Flask(__name__)
# W produkcji klucz powinien być losowy i trzymany poza repozytorium
app.secret_key = "supersecretkey"

# ---------------------------------------------------------------------
# Mongo (opcjonalne)
# ---------------------------------------------------------------------

if MongoClient is not None:
    _mongo_client = MongoClient(CONFIG.get("mongo_uri", "mongodb://localhost:27017"))
    _mongo_db = _mongo_client[CONFIG.get("mongodb_db_name", "alkotester")]
    _entries_collection = _mongo_db["entries"]
else:
    _mongo_client = None
    _mongo_db = None
    _entries_collection = None

# ---------------------------------------------------------------------
# Proste cache w pamięci, żeby nie mielić tych samych plików non stop
# ---------------------------------------------------------------------

_EMP_CACHE = {
    "mtime": 0.0,
    "map": {},  # employee_id -> pin
}

_CSV_CACHE = {
    "mtime": 0.0,
    "entries": [],  # gotowa lista słowników dla schedule()
}


def _base_dir() -> str:
    """Katalog główny projektu (jeden poziom wyżej niż plik admin_server.py)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------
# PIN / ID helpers
# ---------------------------------------------------------------------

def _generate_unique_pin() -> str:
    """Generate a unique four-digit PIN not already present in employees.json."""
    import random

    existing_pins = set()
    try:
        emp_path = CONFIG["employees_json"]
        if not os.path.isabs(emp_path):
            emp_path = os.path.join(_base_dir(), emp_path)
        with open(emp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for emp in data.get("employees", []):
                pin = emp.get("pin")
                if pin:
                    existing_pins.add(str(pin))
    except Exception:
        pass

    while True:
        candidate = f"{random.randint(0, 9999):04d}"
        if candidate not in existing_pins:
            return candidate


    def _log_to_mongo_async(self, ts, emp_id, emp_name, emp_pin, promille, pass_ok: bool):
        """Nieelastyczne logowanie do Mongo – w osobnym wątku, z krótkimi timeoutami."""
        global _MONGO_CLIENT, _MONGO_DISABLED

        if MongoClient is None or _MONGO_DISABLED:
            return

    def worker():
        global _MONGO_CLIENT, _MONGO_DISABLED
        try:
            if _MONGO_CLIENT is None:
                _MONGO_CLIENT = MongoClient(
                    CONFIG.get("mongo_uri", "mongodb://localhost:27017"),
                    serverSelectionTimeoutMS=200,
                    connectTimeoutMS=200,
                    socketTimeoutMS=200,
                )
            db = _MONGO_CLIENT[CONFIG.get("mongodb_db_name", "alkotester")]
            col = db["entries"]
            doc = {
                "datetime": ts,
                "employee_id": emp_id,
                "employee_name": emp_name,
                "employee_pin": emp_pin,
                "promille": float(promille),
                "result": "pass" if pass_ok else "deny",
                "fallback_pin": bool(self.fallback_pin_flag),
            }
            col.insert_one(doc)
        except Exception as e:
            print(f"[Mongo] wyłączam logowanie do Mongo po błędzie: {e}")
            _MONGO_DISABLED = True

    threading.Thread(target=worker, daemon=True).start()

    def trigger_gate_and_log(self, pass_ok: bool, promille: float):
        emp_name = self.current_emp_name or "<nieznany>"
        emp_id = self.current_emp_id or "<none>"
        ts = datetime.now().isoformat()

        print(f"[LOG] {ts} emp={emp_name} ({emp_id}) promille={promille:.3f} pass_ok={pass_ok}")

        # Obsługa bramki (to zostawiamy w głównym wątku – jest szybkie)
        if pass_ok:
            GPIO.output(CONFIG["gate_gpio"], GPIO.HIGH)

            def pulse():
                time.sleep(CONFIG["gate_pulse_sec"])
                GPIO.output(CONFIG["gate_gpio"], GPIO.LOW)

            threading.Thread(target=pulse, daemon=True).start()

            log_csv(
                os.path.join(CONFIG["logs_dir"], "events.csv"),
                ["datetime", "event", "employee_name", "employee_id"],
                [ts, "gate_open", emp_name, emp_id]
            )
        else:
            log_csv(
                os.path.join(CONFIG["logs_dir"], "events.csv"),
                ["datetime", "event", "employee_name", "employee_id"],
                [ts, "deny_access", emp_name, emp_id]
            )

        # Zapisz pomiar do pliku (szybkie IO)
        log_csv(
            os.path.join(CONFIG["logs_dir"], "measurements.csv"),
            ["datetime", "employee_name", "employee_id", "promille", "fallback_pin"],
            [ts, emp_name, emp_id, f"{promille:.3f}", int(self.fallback_pin_flag)]
        )

        # Dodatkowo zapisz log do bazy MongoDB, jeżeli pymongo jest dostępne
        # → ALE w tle, z krótkim timeoutem
        emp_pin = None
        try:
            entry = self.facedb.emp_by_id.get(self.current_emp_id or "")
            if entry:
                emp_pin = entry.get("pin")
        except Exception:
            emp_pin = None

        self._log_to_mongo_async(ts, emp_id, emp_name, emp_pin, promille, pass_ok)



def _generate_next_emp_id() -> str:
    """
    Zwróć kolejne ID pracownika jako string.
    Przeszukuje employees.json, bierze max(numer) + 1. Nienumeryczne ID są ignorowane.
    """
    ids = []
    try:
        emp_path = CONFIG["employees_json"]
        if not os.path.isabs(emp_path):
            emp_path = os.path.join(_base_dir(), emp_path)
        with open(emp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for emp in data.get("employees", []):
                try:
                    ids.append(int(emp.get("id")))
                except Exception:
                    continue
    except Exception:
        pass
    return str(max(ids) + 1 if ids else 1)


# ---------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------

def _load_emp_pin_map() -> dict:
    """
    Wczytaj employees.json do słownika {id -> pin} z prostym cache po mtime.
    Dzięki temu nie czytamy JSON-a setki razy przy każdym odświeżeniu.
    """
    global _EMP_CACHE

    emp_path = CONFIG["employees_json"]
    if not os.path.isabs(emp_path):
        emp_path = os.path.join(_base_dir(), emp_path)

    try:
        mtime = os.path.getmtime(emp_path)
    except OSError:
        _EMP_CACHE = {"mtime": 0.0, "map": {}}
        return {}

    if _EMP_CACHE["mtime"] == mtime and _EMP_CACHE["map"]:
        return _EMP_CACHE["map"]

    try:
        with open(emp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        m = {}
        for rec in data.get("employees", []):
            eid = str(rec.get("id"))
            epin = rec.get("pin")
            if eid and epin:
                m[eid] = epin
        _EMP_CACHE = {"mtime": mtime, "map": m}
        return m
    except Exception:
        _EMP_CACHE = {"mtime": 0.0, "map": {}}
        return {}


def _format_dt(dt_str: str) -> str:
    """Z ISO 8601 robimy ładniejsze 'dd.mm.yyyy HH:MM:SS'."""
    if not dt_str:
        return ""
    try:
        dt_obj = datetime.fromisoformat(dt_str)
        return dt_obj.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return dt_str


def _decision_from_promille(prom: float) -> str:
    """Zwraca 'Odmowa' / 'Przepuszczony' / 'Ponów' na podstawie progów z CONFIG."""
    try:
        th_deny = float(CONFIG.get("threshold_deny", 0.5))
        th_pass = float(CONFIG.get("threshold_pass", 0.0))
    except Exception:
        th_deny = 0.5
        th_pass = 0.0

    if prom >= th_deny:
        return "Odmowa"
    if prom <= th_pass:
        return "Przepuszczony"
    return "Ponów"


def _auth_source_from_fallback(fallback_flag: int | bool) -> str:
    """Opis źródła weryfikacji: AI vs PIN fallback."""
    try:
        flag = int(fallback_flag)
    except Exception:
        flag = 0
    return "PIN (fallback)" if flag else "AI"


def _load_entries_from_csv() -> list[dict]:
    """
    Wczytaj logi z logs/measurements.csv z cache po mtime:
    - jeżeli CSV się nie zmienił → zwracamy gotowe entries z pamięci,
    - jeżeli się zmienił → parsujemy od nowa tylko ostatnie ~500 wierszy.
    """
    global _CSV_CACHE

    base = _base_dir()
    log_dir = CONFIG.get("logs_dir", "logs")
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(base, log_dir)
    log_path = os.path.join(log_dir, "measurements.csv")

    try:
        mtime = os.path.getmtime(log_path)
    except OSError:
        _CSV_CACHE = {"mtime": 0.0, "entries": []}
        return []

    if _CSV_CACHE["mtime"] == mtime and _CSV_CACHE["entries"]:
        return _CSV_CACHE["entries"]

    entries: list[dict] = []
    emp_pin_map = _load_emp_pin_map()

    try:
        # Bierzemy tylko ostatnie 500 linii (plus nagłówek)
        with open(log_path, "r", encoding="utf-8") as f:
            last_lines = deque(f, maxlen=501)
        lines = list(last_lines)
        if not lines:
            _CSV_CACHE = {"mtime": mtime, "entries": []}
            return []

        # Pierwsza linia to nagłówki
        for row in lines[1:]:
            cols = row.strip().split(";")
            # datetime;employee_name;employee_id;promille;fallback_pin
            if len(cols) < 4:
                continue
            ts = cols[0]
            name = cols[1]
            emp_id = cols[2]
            prom_str = cols[3]
            fallback_str = cols[4] if len(cols) > 4 else "0"

            emp_pin = emp_pin_map.get(str(emp_id))

            try:
                prom = float(prom_str.replace(",", "."))
            except Exception:
                prom = 0.0

            dt_fmt = _format_dt(ts)
            result = _decision_from_promille(prom)
            auth_source = _auth_source_from_fallback(fallback_str)

            entries.append(
                {
                    "employee_pin": emp_pin,
                    "employee_name": name,
                    "promille": prom,
                    "datetime": dt_fmt,
                    "result": result,
                    "auth_source": auth_source,
                }
            )

        _CSV_CACHE = {"mtime": mtime, "entries": entries}
        return entries
    except Exception:
        _CSV_CACHE = {"mtime": 0.0, "entries": []}
        return []


# ---------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """Display and process the administrator login form."""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if (
            username == CONFIG.get("admin_username")
            and password == CONFIG.get("admin_password")
        ):
            session["logged_in"] = True
            return redirect(url_for("schedule"))
        error = "Zły login lub hasło"
    return render_template_string(_LOGIN_TEMPLATE, error=error)


@app.route("/logout")
def logout():
    """Log out the current administrator and redirect to the login page."""
    session.pop("logged_in", None)
    return redirect(url_for("login"))


def _ensure_logged_in() -> bool:
    """Zwróć True/False; jeśli False, caller robi redirect do /login."""
    return bool(session.get("logged_in"))


# ---------------------------------------------------------------------
# Główny widok harmonogramu
# ---------------------------------------------------------------------

@app.route("/schedule", methods=["GET"])
def schedule():
    """
    Widok:
      - tabela z wejściami (PIN, imię, nazwisko, promil, czas, wynik, źródło),
      - formularz dodania pracownika,
      - info o PIN-ie ostatnio dodanego pracownika.
    """
    if not _ensure_logged_in():
        return redirect(url_for("login"))

    # info po dodaniu pracownika (GET parametry)
    new_pin = request.args.get("new_pin")
    emp_name = request.args.get("emp_name")
    if new_pin and emp_name:
        info = f"Dodano pracownika {emp_name}. PIN: {new_pin}"
    else:
        info = session.pop("info", None)

    entries: list[dict] = []

    # 1) Próba z MongoDB (jeśli jest dostępny + podłączony)
    if _entries_collection is not None:
        try:
            cursor = (
                _entries_collection.find()
                .sort("datetime", -1)
                .limit(500)
            )
            for doc in cursor:
                pin = doc.get("employee_pin")
                name = doc.get("employee_name")
                prom_val = doc.get("promille")
                dt_str = doc.get("datetime")
                fb_flag = doc.get("fallback_pin", 0)

                try:
                    prom = float(prom_val)
                except Exception:
                    prom = 0.0

                dt_fmt = _format_dt(dt_str)
                result = _decision_from_promille(prom)
                auth_source = _auth_source_from_fallback(fb_flag)

                entries.append(
                    {
                        "employee_pin": pin,
                        "employee_name": name,
                        "promille": prom,
                        "datetime": dt_fmt,
                        "result": result,
                        "auth_source": auth_source,
                    }
                )
        except Exception:
            entries = []

    # 2) Fallback CSV – jeśli baza jest wyłączona albo pusta
    if not entries:
        entries = _load_entries_from_csv()

    return render_template_string(_SCHEDULE_TEMPLATE, entries=entries, info=info)


# ---------------------------------------------------------------------
# Dodawanie pracownika
# ---------------------------------------------------------------------

@app.route("/add_employee", methods=["POST"])
def add_employee():
    """
    Dodaj nowego pracownika:
      - imię + nazwisko z formularza,
      - generujemy unikalny 4-cyfrowy PIN,
      - dopisujemy do employees.json.
    """
    if not _ensure_logged_in():
        return redirect(url_for("login"))

    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    if not first_name or not last_name:
        return redirect(url_for("schedule"))

    full_name = f"{first_name} {last_name}"
    new_pin = _generate_unique_pin()
    new_id = _generate_next_emp_id()

    # Wczytaj / zainicjuj strukturę employees.json
    emp_path = CONFIG["employees_json"]
    if not os.path.isabs(emp_path):
        emp_path = os.path.join(_base_dir(), emp_path)

    try:
        with open(emp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        employees = data.get("employees", [])
    except Exception:
        data = {"employees": []}
        employees = []

    employees.append({"id": new_id, "name": full_name, "pin": new_pin})
    data["employees"] = employees

    try:
        with open(emp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # Inwaliduj cache employees po zapisie
    _EMP_CACHE["mtime"] = 0.0
    _EMP_CACHE["map"] = {}

    # przekierowanie z widocznym PIN-em
    return redirect(url_for("schedule", new_pin=new_pin, emp_name=full_name))


# ---------------------------------------------------------------------
# Uruchamianie serwera
# ---------------------------------------------------------------------

def run_server():
    """
    Start HTTP admin server.

    Port można nadpisać w CONFIG["admin_port"].
    Brak SSL; prosty tryb HTTP do LAN.
    """
    port = int(CONFIG.get("admin_port", 80))
    app.run(
        host="0.0.0.0",
        port=port,
        ssl_context=None,
        debug=False,
        threaded=True,
        use_reloader=False,
    )


# ---------------------------------------------------------------------
# Szablony HTML
# ---------------------------------------------------------------------

_LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>Logowanie administratora</title>
    <style>
        body { font-family: sans-serif; margin: 2em; }
        form { max-width: 300px; }
        input { width: 100%; padding: 8px; margin-top: 8px; }
        button { padding: 8px 16px; margin-top: 12px; width: 100%; }
        .error { color: red; }
    </style>
</head>
<body>
    <h1>Logowanie administratora</h1>
    {% if error %}
    <p class="error">{{ error }}</p>
    {% endif %}
    <form method="post" action="{{ url_for('login') }}">
        <label>Nazwa użytkownika:</label><br>
        <input type="text" name="username" placeholder="login" autofocus required><br>
        <label>Hasło:</label><br>
        <input type="password" name="password" placeholder="hasło" required><br>
        <button type="submit">Zaloguj</button>
    </form>
</body>
</html>
"""


_SCHEDULE_TEMPLATE = """
<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>Harmonogram wejść</title>
    <style>
        body { font-family: sans-serif; margin: 2em; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 2em; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
        form input { margin-right: 8px; }
    </style>
</head>
<body>
    <h1>Harmonogram wejść pracowników</h1>
    {% if info %}
    <p style="color: green; font-weight: bold;">{{ info }}</p>
    {% endif %}
    <table>
        <thead>
            <tr>
                <th>PIN</th>
                <th>Imię</th>
                <th>Nazwisko</th>
                <th>Promil [‰]</th>
                <th>Godzina</th>
                <th>Wynik</th>
                <th>Weryfikacja</th>
            </tr>
        </thead>
        <tbody>
        {% for entry in entries %}
            <tr>
                <td>{{ entry.employee_pin }}</td>
                <td>{{ (entry.employee_name.split(' ')[0] if entry.employee_name else '') }}</td>
                <td>{{ (entry.employee_name.split(' ', 1)[1] if entry.employee_name and ' ' in entry.employee_name else '') }}</td>
                <td>{{ '%.3f'|format(entry.promille) }}</td>
                <td>{{ entry.datetime }}</td>
                <td>{{ entry.result }}</td>
                <td>{{ entry.auth_source }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    <h2>Dodaj nowego pracownika</h2>
    <form method="post" action="{{ url_for('add_employee') }}">
        <input type="text" name="first_name" placeholder="Imię" required>
        <input type="text" name="last_name" placeholder="Nazwisko" required>
        <button type="submit">Dodaj</button>
    </form>
    <p><a href="{{ url_for('logout') }}">Wyloguj</a></p>
</body>
</html>
"""


if __name__ == "__main__":
    run_server()
