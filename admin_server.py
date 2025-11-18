"""
Simple administrator web server for the breathalyzer system.

This module starts a Flask-based HTTP server which provides a minimal
admin panel.  The panel allows an authorised administrator to log in,
view a table of employee entry measurements and add new employees to
the local JSON database.  All traffic is served over plain HTTP and
does not require any SSL certificates.

If the pymongo library is installed the server persists measurement logs
to a MongoDB instance.  Records include the employee's identifier, pin,
full name, recorded BAC and the timestamp of the measurement.  When
pymongo is unavailable the schedule is populated from the local CSV logs.
The admin panel renders these records in a simple HTML table and
exposes a form for adding new employees.  When a new employee is created
the system generates a unique four‑digit pin which has not previously been
assigned and appends the new record to the employees JSON file.
"""

from flask import Flask, request, redirect, url_for, session, render_template_string

# Additional imports for improved performance and formatting
from collections import deque
from datetime import datetime
import json
import os
# Try to import pymongo; if unavailable, use a fallback to CSV logs.
try:
    from pymongo import MongoClient  # type: ignore
except Exception:
    MongoClient = None  # type: ignore
from datetime import datetime

from config import CONFIG

# Initialise Flask application
app = Flask(__name__)
# Session secret key; in a real deployment this should be a random
# value stored securely outside of source control
app.secret_key = "supersecretkey"

# Initialise Mongo client and select database/collection
if MongoClient is not None:
    # Set up MongoDB connection if the library is available.
    _mongo_client = MongoClient(CONFIG.get("mongo_uri", "mongodb://localhost:27017"))
    _mongo_db = _mongo_client[CONFIG.get("mongodb_db_name", "alkotester")]
    _entries_collection = _mongo_db["entries"]
else:
    _mongo_client = None
    _mongo_db = None
    _entries_collection = None

def _generate_unique_pin() -> str:
    """Generate a unique four‑digit PIN not already present in employees.json."""
    import random
    existing_pins = set()
    try:
        with open(CONFIG["employees_json"], "r", encoding="utf-8") as f:
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


def _generate_next_emp_id() -> str:
    """Return the next numeric employee ID as a string.

    The function scans existing IDs in employees.json and returns
    one greater than the highest integer ID.  Non‑numeric IDs are
    ignored.  If no numeric IDs exist, '1' is returned.
    """
    ids = []
    try:
        with open(CONFIG["employees_json"], "r", encoding="utf-8") as f:
            data = json.load(f)
            for emp in data.get("employees", []):
                try:
                    ids.append(int(emp.get("id")))
                except Exception:
                    continue
    except Exception:
        pass
    return str(max(ids) + 1 if ids else 1)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Display and process the administrator login form."""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        # Simple credential check using configuration values
        if (username == CONFIG.get("admin_username") and
                password == CONFIG.get("admin_password")):
            session["logged_in"] = True
            return redirect(url_for("schedule"))
        error = "Zły login lub hasło"
    return render_template_string(_LOGIN_TEMPLATE, error=error)


@app.route("/logout")
def logout():
    """Log out the current administrator and redirect to the login page."""
    session.pop("logged_in", None)
    return redirect(url_for("login"))


def _ensure_logged_in():
    """Redirect to login page if the user is not authenticated."""
    if not session.get("logged_in"):
        return False
    return True


@app.route("/schedule", methods=["GET"])
def schedule():
    """Render the table of employee entry logs and the employee creation form."""
    if not _ensure_logged_in():
        return redirect(url_for("login"))
    # Obsłuż parametry GET po dodaniu pracownika.  Jeżeli występują, wyświetl
    # komunikat o nowym pracowniku i przydzielonym PIN-ie; w przeciwnym razie
    # pobierz ewentualny komunikat z sesji (jednorazowy).
    new_pin = request.args.get("new_pin")
    emp_name = request.args.get("emp_name")
    if new_pin and emp_name:
        info = f"Dodano pracownika {emp_name}. PIN: {new_pin}"
    else:
        info = session.pop("info", None)

    entries: list[dict] = []
    # Spróbuj pobrać logi z MongoDB
    if _entries_collection is not None:
        try:
            cursor = _entries_collection.find().sort("datetime", -1).limit(500)
            raw_entries = list(cursor)
            for doc in raw_entries:
                pin = doc.get("employee_pin")
                name = doc.get("employee_name")
                prom_val = doc.get("promille")
                dt_str = doc.get("datetime")
                # Sformatuj datę do bardziej czytelnej postaci
                try:
                    dt_obj = datetime.fromisoformat(dt_str)
                    dt_fmt = dt_obj.strftime("%d.%m.%Y %H:%M:%S")
                except Exception:
                    dt_fmt = dt_str
                # Oblicz wynik decyzji na podstawie promili i progów z konfiguracji
                result = ""
                try:
                    pr = float(prom_val)
                    if pr >= CONFIG.get("threshold_deny", 0.5):
                        result = "Odmowa"
                    elif pr <= CONFIG.get("threshold_pass", 0.0):
                        result = "Przepuszczony"
                    else:
                        result = "Ponów"
                except Exception:
                    result = ""
                entries.append({
                    "employee_pin": pin,
                    "employee_name": name,
                    "promille": prom_val,
                    "datetime": dt_fmt,
                    "result": result,
                })
        except Exception:
            entries = []
    # Jeżeli w bazie nic nie ma lub wystąpił błąd, czytaj plik CSV
    if not entries:
        try:
            # Bazowy katalog projektu (jeden poziom wyżej niż katalog alkotester)
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = CONFIG.get("logs_dir", "logs")
            if not os.path.isabs(log_dir):
                log_dir = os.path.join(base, log_dir)
            log_path = os.path.join(log_dir, "measurements.csv")
            # Czytaj tylko ostatnie 500 wierszy dla wydajności
            with open(log_path, "r", encoding="utf-8") as f:
                last_lines = deque(f, maxlen=501)
            last_lines = list(last_lines)
            if last_lines:
                # Pomijamy pierwszy wiersz z nagłówkami, jeśli istnieje
                for row in last_lines[1:]:
                    cols = row.strip().split(";")
                    if len(cols) < 4:
                        continue
                    ts = cols[0]
                    name = cols[1]
                    emp_id = cols[2]
                    prom_str = cols[3]
                    # Ustal pin na podstawie employees.json
                    emp_pin = None
                    try:
                        emp_path = CONFIG["employees_json"]
                        if not os.path.isabs(emp_path):
                            emp_path = os.path.join(base, emp_path)
                        with open(emp_path, "r", encoding="utf-8") as f_emp:
                            data_emp = json.load(f_emp)
                            for rec in data_emp.get("employees", []):
                                if str(rec.get("id")) == str(emp_id):
                                    emp_pin = rec.get("pin")
                                    break
                    except Exception:
                        emp_pin = None
                    # Konwersja promili na liczbę zmiennoprzecinkową
                    try:
                        prom = float(prom_str.replace(",", "."))
                    except Exception:
                        prom = 0.0
                    # Format daty
                    try:
                        dt_obj = datetime.fromisoformat(ts)
                        dt_fmt = dt_obj.strftime("%d.%m.%Y %H:%M:%S")
                    except Exception:
                        dt_fmt = ts
                    # Wyznacz rezultat na podstawie progów
                    result = ""
                    try:
                        if prom >= CONFIG.get("threshold_deny", 0.5):
                            result = "Odmowa"
                        elif prom <= CONFIG.get("threshold_pass", 0.0):
                            result = "Przepuszczony"
                        else:
                            result = "Ponów"
                    except Exception:
                        result = ""
                    entries.append({
                        "employee_pin": emp_pin,
                        "employee_name": name,
                        "promille": prom,
                        "datetime": dt_fmt,
                        "result": result,
                    })
        except Exception:
            entries = []
    return render_template_string(_SCHEDULE_TEMPLATE, entries=entries, info=info)


@app.route("/add_employee", methods=["POST"])
def add_employee():
    """Handle submission of the new employee form.

    Accepts first and last name, generates a unique four digit pin and
    appends a new record to employees.json.  After creation the user
    remains on the schedule page.  Only accessible when logged in.
    """
    if not _ensure_logged_in():
        return redirect(url_for("login"))
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    if not first_name or not last_name:
        # if inputs missing, just reload
        return redirect(url_for("schedule"))
    full_name = f"{first_name} {last_name}"
    new_pin = _generate_unique_pin()
    new_id = _generate_next_emp_id()
    try:
        # Load current employees
        with open(CONFIG["employees_json"], "r", encoding="utf-8") as f:
            data = json.load(f)
        employees = data.get("employees", [])
    except Exception:
        data = {"employees": []}
        employees = []
    employees.append({"id": new_id, "name": full_name, "pin": new_pin})
    data["employees"] = employees
    try:
        with open(CONFIG["employees_json"], "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # Po dodaniu pracownika przekieruj do harmonogramu z numerem PIN i nazwą jako parametry GET,
    # aby administrator mógł zobaczyć przydzielony kod.
    return redirect(url_for("schedule", new_pin=new_pin, emp_name=full_name))


def run_server():
    """Entry point for starting the HTTP admin server.

    The server listens on a standard HTTP port and does not use SSL.
    If you wish to run on a different port you can override it by
    adding an ``admin_port`` entry to the ``CONFIG`` dictionary.
    """
    # Bind to all interfaces to allow LAN access.  Jeśli w konfiguracji
    # podano port, użyj go; w przeciwnym razie skorzystaj z 80.  Praca w
    # trybie wielowątkowym (threaded=True) zapobiega blokowaniu pętli GUI,
    # a wyłączenie reloadera (use_reloader=False) zmniejsza narzut CPU.
    port = int(CONFIG.get("admin_port", 80))
    app.run(host="0.0.0.0", port=port, ssl_context=None, debug=False,
            threaded=True, use_reloader=False)


# Minimal HTML templates defined as module constants.  These could be
# extracted to separate files or replaced with Jinja templates in a
# larger application.

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