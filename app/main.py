import os, logging
from fastapi import FastAPI
from .scheduler import create_scheduler, LAST_SIGNAL

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
        return {"ready": False, "message": "Zatiaľ nie je signál. Počkaj na prvý 30-min beh."}
    return {
        "ready": True,
        "created_at": LAST_SIGNAL.created_at,
        "regime": LAST_SIGNAL.regime,
        "picks": [p.__dict__ for p in LAST_SIGNAL.picks],
        "note": LAST_SIGNAL.note,
    }

# Ponecháme /test-email pre rýchly test SMTP
from .services.notifier import send_email
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
