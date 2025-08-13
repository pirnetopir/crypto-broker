import os, logging
from fastapi import FastAPI
from .scheduler import create_scheduler
from .services.notifier import send_email

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
