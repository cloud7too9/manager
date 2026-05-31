# Schlankes Basis-Image
FROM python:3.11-slim

# System-Pakete:
#   git              -> Repo-Updates im deploy.sh
#   docker-ce-cli    -> "docker"-Kommando (spricht den gemounteten Host-Socket an)
#   docker-compose-plugin -> "docker compose up -d --build" im deploy.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/apps/manager

# Erst Dependencies (besseres Layer-Caching), dann Code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static ./static

EXPOSE 8000

# 1 Worker reicht: Background-Tasks (Deploys) sollen im selben Prozess laufen
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
