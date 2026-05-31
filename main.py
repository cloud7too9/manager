"""
main.py - Apps Manager Backend (FastAPI)

Liest apps.json (Single Source of Truth), reichert sie mit dem Docker-
Live-Status an und stellt Endpunkte fuer Start/Stop/Update sowie
GitHub-Webhooks und dynamisches App-Management bereit.
"""

import os
import json
import hmac
import hashlib
import asyncio
import logging
from pathlib import Path
from typing import Optional

import docker
from docker.errors import NotFound, APIError, DockerException
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles

# --- Konfiguration (via Environment ueberschreibbar) -------------------------
APPS_CONFIG = Path(os.getenv("APPS_CONFIG", "/opt/apps/manager/apps.json"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")          # leer = Signaturpruefung aus
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("apps-manager")

app = FastAPI(title="Apps Manager", version="1.1.0")


# --- Hilfsfunktionen ---------------------------------------------------------
def get_docker() -> docker.DockerClient:
    """Docker-Client erzeugen. Faellt der Socket aus, geben wir 503 zurueck."""
    try:
        return docker.from_env()          # nutzt /var/run/docker.sock
    except DockerException as exc:
        log.error("Docker nicht erreichbar: %s", exc)
        raise HTTPException(status_code=503, detail="Docker-Daemon nicht erreichbar")


def load_apps() -> list[dict]:
    """apps.json laden und robust gegen fehlende/ungueltige Datei sein."""
    if not APPS_CONFIG.exists():
        return []
    try:
        data = json.loads(APPS_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"apps.json ungueltig: {exc}")
    return data.get("apps", [])


def save_apps(apps: list[dict]) -> None:
    """Schreibt die aktualisierte App-Liste robust zurueck in die apps.json."""
    try:
        APPS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with open(APPS_CONFIG, "w", encoding="utf-8") as f:
            json.dump({"apps": apps}, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.error("Fehler beim Schreiben der apps.json: %s", exc)
        raise HTTPException(status_code=500, detail="Konfiguration konnte nicht gespeichert werden")


def find_app(app_id: str) -> dict:
    """Eine App-Definition anhand der ID holen oder 404 werfen."""
    for app_def in load_apps():
        if app_def.get("id") == app_id:
            return app_def
    raise HTTPException(status_code=404, detail=f"App '{app_id}' nicht gefunden")


def container_status(client: docker.DockerClient, name: str) -> dict:
    """Live-Status eines Containers ermitteln. 'missing' falls nicht vorhanden."""
    try:
        container = client.containers.get(name)
        return {"state": container.status, "running": container.status == "running"}
    except NotFound:
        return {"state": "missing", "running": False}
    except APIError as exc:
        log.warning("Docker-API-Fehler bei %s: %s", name, exc)
        return {"state": "error", "running": False}


def verify_signature(body: bytes, signature: Optional[str]) -> bool:
    """GitHub-Webhook-Signatur (X-Hub-Signature-256) per HMAC pruefen."""
    if not WEBHOOK_SECRET:
        return True
    if not signature:
        return False
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


async def run_deploy(app_def: dict) -> None:
    """deploy.sh ausfuehren. Falls das Verzeichnis fehlt, wird initial geklont."""
    app_path = Path(app_def["path"])
    branch = app_def.get("branch", "main")
    
    # Automatisches Klonen, falls der Zielordner noch nicht existiert
    if not app_path.exists() and app_def.get("repo_url"):
        log.info("Ordner fehlt. Starte initiales Git Clone fuer %s", app_def["id"])
        app_path.parent.mkdir(parents=True, exist_ok=True)
        
        clone_proc = await asyncio.create_subprocess_exec(
            "git", "clone", "-b", branch, app_def["repo_url"], str(app_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await clone_proc.communicate()
        
        # Fallback: Erzeuge Standard-deploy.sh vor Ort, falls nicht im Repository vorhanden
        deploy_script = app_path / "deploy.sh"
        if not deploy_script.exists():
            default_script = (
                "#!/usr/bin/env bash\nset -euo pipefail\n"
                "BRANCH=\"${1:-main}\"\ngit fetch origin\ngit checkout \"${BRANCH}\"\n"
                "git reset --hard \"origin/${BRANCH}\"\ngit clean -fd -e deploy.sh -e docker-compose.yml\n"
                "docker compose up -d --build\n"
            )
            deploy_script.write_text(default_script, encoding="utf-8")
            deploy_script.chmod(0o755)

    deploy_script = app_path / "deploy.sh"
    if not deploy_script.exists():
        log.error("deploy.sh fehlt fuer '%s' (%s)", app_def["id"], deploy_script)
        return

    log.info("Deploy gestartet: %s (Branch %s)", app_def["id"], branch)
    proc = await asyncio.create_subprocess_exec(
        "bash", str(deploy_script), branch,
        cwd=str(app_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    log.info(
        "Deploy beendet: %s (rc=%s)\n%s",
        app_def["id"], proc.returncode, stdout.decode(errors="replace"),
    )


# --- API-Endpunkte -----------------------------------------------------------
@app.get("/api/systems")
def list_systems():
    """Alle Apps aus apps.json + aktueller Docker-Status."""
    client = get_docker()
    systems = []
    for app_def in load_apps():
        status = container_status(client, app_def["container_name"])
        systems.append({**app_def, "status": status["state"], "running": status["running"]})
    return {"systems": systems}


@app.post("/api/systems")
async def add_system(request: Request):
    """Fuegt eine neue App dynamisch zur Verwaltung hinzu und trigger das Klonen."""
    try:
        data = await request.json()
        app_id = data["id"].strip().lower()
        name = data["name"].strip()
        repo_url = data["repo_url"].strip()
        port = int(data["port"])
        branch = data.get("branch", "main").strip() or "main"
        
        if not app_id or not name or not repo_url:
            raise HTTPException(status_code=400, detail="Pflichtfelder unvollstaendig")
            
        apps = load_apps()
        if any(a.get("id") == app_id for a in apps):
            raise HTTPException(status_code=400, detail=f"App-ID '{app_id}' existiert bereits")
            
        new_app = {
            "id": app_id,
            "name": name,
            "repo_url": repo_url,
            "path": f"/opt/apps/services/{app_id}",
            "container_name": f"{app_id}-container",
            "port": port,
            "branch": branch
        }
        
        apps.append(new_app)
        save_apps(apps)
        
        # Initialen Deployment- & Klon-Prozess asynchron im Hintergrund starten
        asyncio.create_task(run_deploy(new_app))
        return {"ok": True, "message": "App erfolgreich registriert."}
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungueltiges Port-Format")


@app.delete("/api/systems/{app_id}")
def delete_system(app_id: str):
    """Entfernt eine App aus der Konfigurationsdatei."""
    apps = load_apps()
    filtered_apps = [a for a in apps if a.get("id") != app_id]
    
    if len(apps) == len(filtered_apps):
        raise HTTPException(status_code=404, detail="App nicht gefunden")
        
    save_apps(filtered_apps)
    return {"ok": True, "message": "App erfolgreich entfernt"}


@app.post("/api/systems/{app_id}/start")
def start_container(app_id: str):
    """Container manuell starten."""
    app_def = find_app(app_id)
    client = get_docker()
    try:
        client.containers.get(app_def["container_name"]).start()
    except NotFound:
        raise HTTPException(status_code=404, detail="Container existiert nicht – zuerst deployen")
    except APIError as exc:
        raise HTTPException(status_code=500, detail=f"Start fehlgeschlagen: {exc}")
    return {"ok": True, "action": "start", "app": app_id}


@app.post("/api/systems/{app_id}/stop")
def stop_container(app_id: str):
    """Container manuell stoppen."""
    app_def = find_app(app_id)
    client = get_docker()
    try:
        client.containers.get(app_def["container_name"]).stop()
    except NotFound:
        raise HTTPException(status_code=404, detail="Container existiert nicht")
    except APIError as exc:
        raise HTTPException(status_code=500, detail=f"Stop fehlgeschlagen: {exc}")
    return {"ok": True, "action": "stop", "app": app_id}


@app.post("/api/systems/{app_id}/update")
async def manual_update(app_id: str):
    """Manuelles Update ueber das UI (ohne GitHub-Signatur)."""
    app_def = find_app(app_id)
    asyncio.create_task(run_deploy(app_def))
    return {"ok": True, "action": "update", "app": app_id}


@app.post("/api/webhook/{app_id}")
async def github_webhook(
    app_id: str,
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
):
    """GitHub-Push-Webhook: validiert Signatur und stoesst Deploy im Hintergrund an."""
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Ungueltige Webhook-Signatur")

    app_def = find_app(app_id)
    asyncio.create_task(run_deploy(app_def))
    return {"ok": True, "queued": app_id}


@app.get("/api/health")
def health():
    return {"status": "ok"}


# --- Statisches UI -----------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    log.warning("Static-Verzeichnis fehlt: %s", STATIC_DIR)