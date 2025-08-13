import os, logging
from fastapi import FastAPI
from .scheduler import create_scheduler, LAST_SIGNAL, job_30m

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="crypto-broker")

@app.on_event("startup")
def _start_scheduler():
    sch = create_scheduler()
    sch.start()
    app.state.scheduler = sch
    logging.info("Scheduler started (interval %s min)", os.getenv("REFRESH_MINUTES", "30"))

@app.get("/")
def root():
    return {"status": "ok", "app": "crypto-broker", "scheduler": "running"}

@app.get("/signal")
def get_signal():
    if LAST_SIGNAL is None:
        return {"ready": False, "message": "Zatiaľ nie je signál. Počkaj na prvý 30-min beh alebo použi /run-now."}
    return {
        "ready": True,
        "created_at": LAST_SIGNAL.created_at,
        "regime": LAST_SIGNAL.regime,
        "picks": [p.__dict__ for p in LAST_SIGNAL.picks],
        "note": LAST_SIGNAL.note,
    }

# Manuálny trigger (GET pre jednoduchosť)
@app.get("/run-now")
async def run_now():
    await job_30m()
    return {"ok": True}
