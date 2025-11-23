import os
import os.path

BASE_DIR = os.path.dirname(__file__)

CONFIG = {
    "admin_username": os.environ.get("ADMIN_USERNAME", "admin"),
    "admin_password": os.environ.get("ADMIN_PASSWORD", "admin"),

    "mongo_uri": os.environ.get("MONGO_URI", ""),
    "mongodb_db_name": os.environ.get("MONGODB_DB_NAME", "alkotester"),

    "employees_json": os.path.join(BASE_DIR, "employees.json"),
    "logs_dir": os.path.join(BASE_DIR, "logs"),

    "admin_port": 80,
}
