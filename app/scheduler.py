import os, logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Jediný globálny scheduler pre celý proces
_scheduler: AsyncIOScheduler | None = None

async def job_30m():
    # Tu neskôr: fetch CoinGecko -> indikátory -> skóre -> email so signálom
    logging.info("job_30m tick @ %s", datetime.utcnow().isoformat() + "Z")

def create_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        tz = os.getenv("TZ", "UTC")
        minutes = int(os.getenv("REFRESH_MINUTES", "30"))
        _scheduler = AsyncIOScheduler(timezone=tz)
        _scheduler.add_job(
            job_30m,
            IntervalTrigger(minutes=minutes),
            id="job_30m",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return _scheduler
