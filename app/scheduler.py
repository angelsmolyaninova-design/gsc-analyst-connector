import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    from app.collector import daily_collect_all

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        daily_collect_all,
        CronTrigger(hour=6, minute=0),
        id="daily_collect",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    log.info("scheduler_started")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")
