import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _keep_alive_ping():
    from app import db
    try:
        row = await db.fetchrow("SELECT 1 AS ok")
        log.info("keep_alive_ping success result=%s", row["ok"] if row else None)
    except Exception as e:
        log.error("keep_alive_ping failed error=%s", e)


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
    _scheduler.add_job(
        _keep_alive_ping,
        IntervalTrigger(days=3),
        id="keep_alive",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    _scheduler.start()

    for job in _scheduler.get_jobs():
        log.info("scheduled_job id=%s next_run=%s", job.id, job.next_run_time)

    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")
