FROM python:3.12-slim

# Żeby logi od razu leciały na stdout
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Najpierw zależności
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Potem reszta kodu
COPY . .

# Northflank zwykle daje PORT w ENV, a Twój run_server go używa
# (port = int(os.environ.get("PORT", CONFIG.get("admin_port", 8000))))
# więc nie musimy nic hardcodować.

CMD ["python", "admin_server.py"]
