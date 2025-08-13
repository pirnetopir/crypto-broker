import os, logging
from fastapi import FastAPI

from fastapi import Request
from fastapi.templating import Jinja2Templates


# dôležité: importovať modul, NIE premennú
from . import scheduler as sched
from .services.notifier import send_email
from .services.coingecko import ping as cg_ping_api

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="crypto-broker")

templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def _start_scheduler():
    sch = sched.create_scheduler()
    sch.start()
    app.state.scheduler = sch
    logging.info("Scheduler started (interval %s min)", os.getenv("REFRESH_MINUTES", "30"))

@app.get("/")
def root():
    return {"status": "ok", "app": "crypto-broker", "scheduler": "running"}

@app.get("/dashboard")
def dashboard(request: Request):
    # tieto hodnoty len na zobrazenie v hlavičke
    preselect = os.getenv("PRESELECT", "80")
    tz = os.getenv("TZ", "Europe/Bratislava")
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "preselect": preselect, "tz": tz},
    )

@app.get("/signal")
def get_signal():
    if sched.LAST_SIGNAL is None:
        return {"ready": False, "message": "Zatiaľ nie je signál. Počkaj na prvý 30-min beh alebo použi /run-now."}
    return {
        "ready": True,
        "created_at": sched.LAST_SIGNAL.created_at,
        "regime": sched.LAST_SIGNAL.regime,
        "picks": [p.__dict__ for p in sched.LAST_SIGNAL.picks],
        "note": sched.LAST_SIGNAL.note,
    }

@app.get("/run-now")
async def run_now():
    await sched.job_30m()
    return {"ok": True}

@app.get("/test-email")
def test_email():
    try:
        send_email(
            subject="Krypto Broker – test email",
            html="<h3>Fungujem ✅</h3><p>Toto je test z tvojej aplikácie na Renderi.</p>",
        )
        return {"ok": True, "sent_to": os.getenv("EMAIL_TO")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/cg-ping")
async def cg_ping_route():
    data = await cg_ping_api()
    return {"ok": True, "plan": os.getenv("COINGECKO_PLAN"), "resp": data}
