"""Collect GSC data and store in Postgres."""
import asyncio
import logging
import time
from datetime import date, timedelta

import asyncpg

from app import db
from app.gsc_client import (
    build_service,
    query_search_analytics,
    get_search_appearance_types,
)

log = logging.getLogger(__name__)

BACKFILL_DAYS = 90
# GSC data lags ~2 days
DATA_LAG_DAYS = 2


def _dates_for_backfill() -> list[date]:
    end = date.today() - timedelta(days=DATA_LAG_DAYS)
    return [end - timedelta(days=i) for i in range(BACKFILL_DAYS)]


def _yesterday_gsc() -> date:
    return date.today() - timedelta(days=DATA_LAG_DAYS)


async def _upsert_totals(conn: asyncpg.Connection, site_id: str, d: date, row: dict):
    await conn.execute(
        """
        INSERT INTO daily_totals (site_id, date, clicks, impressions, ctr, position)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (site_id, date) DO UPDATE
          SET clicks=$3, impressions=$4, ctr=$5, position=$6
        """,
        site_id, d,
        int(row.get("clicks", 0)),
        int(row.get("impressions", 0)),
        float(row.get("ctr", 0)),
        float(row.get("position", 0)),
    )


async def _upsert_queries(conn: asyncpg.Connection, site_id: str, d: date, rows: list):
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO daily_queries (site_id, date, query, clicks, impressions, ctr, position)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (site_id, date, query) DO UPDATE
          SET clicks=$4, impressions=$5, ctr=$6, position=$7
        """,
        [
            (site_id, d, r["keys"][0],
             int(r.get("clicks", 0)), int(r.get("impressions", 0)),
             float(r.get("ctr", 0)), float(r.get("position", 0)))
            for r in rows
        ],
    )


async def _upsert_pages(conn: asyncpg.Connection, site_id: str, d: date, rows: list):
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO daily_pages (site_id, date, page, clicks, impressions, ctr, position)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (site_id, date, page) DO UPDATE
          SET clicks=$4, impressions=$5, ctr=$6, position=$7
        """,
        [
            (site_id, d, r["keys"][0],
             int(r.get("clicks", 0)), int(r.get("impressions", 0)),
             float(r.get("ctr", 0)), float(r.get("position", 0)))
            for r in rows
        ],
    )


async def _upsert_ai_appearance(
    conn: asyncpg.Connection, site_id: str, d: date, rows: list
):
    if not rows:
        return
    await conn.executemany(
        """
        INSERT INTO daily_ai_appearance
          (site_id, date, appearance_type, clicks, impressions)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (site_id, date, appearance_type) DO UPDATE
          SET clicks=$4, impressions=$5
        """,
        [
            (site_id, d, r["keys"][0],
             int(r.get("clicks", 0)), int(r.get("impressions", 0)))
            for r in rows
        ],
    )


def _collect_one_date_sync(service, property_uri: str, d: date) -> dict:
    ds = d.isoformat()
    totals_rows = query_search_analytics(service, property_uri, ds, ds, [], row_limit=1)
    totals = totals_rows[0] if totals_rows else {}

    query_rows = query_search_analytics(
        service, property_uri, ds, ds, ["query"], row_limit=500
    )
    page_rows = query_search_analytics(
        service, property_uri, ds, ds, ["page"], row_limit=200
    )
    # AI appearances — use searchAppearance dimension
    # TODO: As of June 2026, check if AI Overviews / AI Mode appear as distinct
    # searchAppearance values (e.g. "AI_OVERVIEW") via the Search Analytics API.
    # Currently collecting all searchAppearance types; filter for AI-specific ones
    # in the tools layer once Google officially documents the values.
    ai_rows = get_search_appearance_types(service, property_uri, ds, ds)

    time.sleep(0.3)  # gentle QPS pacing
    return {
        "totals": totals,
        "queries": query_rows,
        "pages": page_rows,
        "ai": ai_rows,
    }


async def collect_for_date(site_record, d: date, service) -> None:
    site_id = str(site_record["id"])
    property_uri = site_record["property"]

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, _collect_one_date_sync, service, property_uri, d
    )

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _upsert_totals(conn, site_id, d, data["totals"])
            await _upsert_queries(conn, site_id, d, data["queries"])
            await _upsert_pages(conn, site_id, d, data["pages"])
            await _upsert_ai_appearance(conn, site_id, d, data["ai"])


async def backfill(site_record, user_record) -> None:
    """Backfill last 90 days for a site. Called after OAuth."""
    log.info("backfill_start site=%s", site_record["property"])
    try:
        service = build_service(user_record["google_refresh_token"])
    except Exception as e:
        log.error("backfill build_service failed site=%s error=%s", site_record["property"], e)
        return

    dates = _dates_for_backfill()
    for i, d in enumerate(dates):
        for attempt in range(3):
            try:
                await collect_for_date(site_record, d, service)
                break
            except Exception as e:
                wait = 2 ** attempt
                log.warning(
                    "backfill error site=%s date=%s attempt=%d error=%s",
                    site_record["property"], d, attempt, e,
                )
                if attempt < 2:
                    await asyncio.sleep(wait)
        if i % 10 == 9:
            await asyncio.sleep(1)  # extra breathing room every 10 days

    log.info("backfill_done site=%s", site_record["property"])


async def daily_collect_all() -> None:
    """Collect yesterday's GSC data for all active sites."""
    users = await db.fetch(
        "SELECT * FROM users WHERE is_active = true"
    )
    d = _yesterday_gsc()
    log.info("daily_collect date=%s users=%d", d, len(users))

    for user in users:
        sites = await db.get_sites_for_user(str(user["id"]))
        try:
            service = build_service(user["google_refresh_token"])
        except Exception as e:
            log.error(
                "user_deactivated_token_failure user_id=%s email=%s error=%s "
                "reason=refresh_token_invalid_or_revoked action_needed=user_must_reconnect",
                user["id"], user["email"], e,
            )
            # Mark inactive to avoid repeated failures on bad tokens.
            # The MCP connector will surface a reconnect message on next tool call
            # (see main.py RECONNECT_MESSAGE) instead of silently returning stale data.
            await db.execute(
                "UPDATE users SET is_active = false WHERE id = $1", user["id"]
            )
            continue

        for site in sites:
            try:
                await collect_for_date(site, d, service)
            except Exception as e:
                log.error(
                    "daily_collect site_error site=%s date=%s error=%s",
                    site["property"], d, e,
                )
