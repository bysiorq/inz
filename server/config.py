import os

CONFIG = {
    "admin_username": "admin",
    "admin_password": "jakies_silne_haslo",

    # URI i DB nazwa z env (ustawisz w Render)
    "mongo_uri": os.environ.get("MONGO_URI", ""),
    "mongodb_db_name": os.environ.get("MONGODB_DB_NAME", "alkotester"),

    # ścieżki lokalne na serwerze
    "employees_json": os.path.join(os.path.dirname(__file__), "employees.json"),
    "logs_dir": os.path.join(os.path.dirname(__file__), "logs"),

    # na Render i tak nie używane, ale może zostać:
    "admin_port": 80,
}
