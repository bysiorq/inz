FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /server

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# zakładam, że requirements.txt jest w katalogu głównym repo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# reszta kodu
COPY . .

EXPOSE 8000

CMD ["gunicorn", "-b", "0.0.0.0:8000", "server.admin_server:app"]
