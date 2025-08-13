def create_scheduler() -> AsyncIOScheduler:
    """
    Spúšťaj job presne o 07:30, 13:00 a 22:00 (podľa TZ v .env).
    """
    from apscheduler.triggers.cron import CronTrigger

    global _scheduler
    if _scheduler is None:
        tz = os.getenv("TZ", "Europe/Bratislava")
        _scheduler = AsyncIOScheduler(timezone=tz)

        # ráno 07:30
        _scheduler.add_job(
            job_30m,
            CronTrigger(hour=7, minute=30),
            id="job_morning",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # obed 13:00
        _scheduler.add_job(
            job_30m,
            CronTrigger(hour=13, minute=0),
            id="job_noon",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # večer 22:00
        _scheduler.add_job(
            job_30m,
            CronTrigger(hour=22, minute=0),
            id="job_evening",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return _scheduler
