"""MCP tool definitions for GSC Analyst."""
import logging
import re
import time
from datetime import date, timedelta

import httpx
from mcp.types import TextContent

from app import db

log = logging.getLogger(__name__)

VERSION = "0.2.0"


async def _log_tool_call(
    user_id: str | None, tool_name: str, site_id: str | None,
    duration_ms: int, success: bool,
):
    try:
        await db.execute(
            "INSERT INTO tool_calls (user_id, tool_name, site_id, duration_ms, success) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, tool_name, site_id, duration_ms, success,
        )
    except Exception as e:
        log.warning("tool_call_log_failed tool=%s error=%s", tool_name, e)


def _friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "quota" in msg or "rate" in msg or "rateLimitExceeded" in str(e):
        return (
            "Google API quota exceeded. Data will refresh automatically "
            "within an hour. Please try again later."
        )
    if "invalid_grant" in msg or "revoked" in msg or "expired" in msg:
        return (
            "Your Google authorization has expired or been revoked. "
            "Please reconnect at the connector homepage to re-authorize."
        )
    if "connection" in msg or "timeout" in msg or "could not connect" in msg:
        return (
            "Database connection issue. This is usually temporary — "
            "please try again in a minute."
        )
    if "permission" in msg or "forbidden" in msg or "403" in msg:
        return (
            "Access denied by Google. Your account may not have permission "
            "to view this Search Console property."
        )
    return f"Something went wrong: {type(e).__name__}. Please try again shortly."


async def dispatch_tool(
    name: str, args: dict, *, user_id: str | None
) -> list[TextContent]:
    t0 = time.monotonic()
    site_id = None
    try:
        result, site_id = await _dispatch(name, args, user_id=user_id)
    except Exception as e:
        dur = int((time.monotonic() - t0) * 1000)
        log.error("tool_error tool=%s user_id=%s error=%s", name, user_id, e)
        await _log_tool_call(user_id, name, None, dur, False)
        return [TextContent(type="text", text=_friendly_error(e))]

    dur = int((time.monotonic() - t0) * 1000)
    log.info("tool_call tool=%s user_id=%s duration_ms=%d", name, user_id, dur)
    await _log_tool_call(user_id, name, site_id, dur, True)
    return [TextContent(type="text", text=result)]


async def _dispatch(
    name: str, args: dict, *, user_id: str | None
) -> tuple[str, str | None]:
    """Returns (result_text, site_id_or_none)."""
    if name == "ping":
        return f"GSC Analyst connector v{VERSION} — OK", None

    if not user_id:
        return "Error: Could not identify user from connector URL.", None

    sites = await db.get_sites_for_user(user_id)
    if not sites:
        return (
            "No Search Console properties found for your account. "
            "Please reconnect at the connector homepage."
        ), None

    site_param = args.get("site")
    if site_param:
        site_record = next(
            (s for s in sites if s["property"] == site_param), None
        )
        if not site_record:
            props = ", ".join(s["property"] for s in sites)
            return f"Site '{site_param}' not found. Your properties: {props}", None
    else:
        site_record = sites[0]

    site_id = str(site_record["id"])
    property_name = site_record["property"]

    period = args.get("period", "28d")
    days = int(period.rstrip("d"))
    detail = args.get("detail", "summary")

    if name == "site_overview":
        r = await _site_overview(site_id, property_name, days, detail)
    elif name == "analyze_changes":
        r = await _analyze_changes(site_id, property_name, days, detail)
    elif name == "ai_visibility_snapshot":
        r = await _ai_visibility_snapshot(site_id, property_name, days)
    elif name == "low_hanging_fruit":
        min_imp = args.get("min_impressions", 200)
        r = await _low_hanging_fruit(site_id, property_name, min_imp)
    elif name == "page_quick_audit":
        url = args.get("url", "")
        r = await _page_quick_audit(url, site_id)
    else:
        r = f"Unknown tool: {name}"

    return r, site_id


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _period_dates(days: int) -> tuple[date, date, date, date]:
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return start, end, prev_start, prev_end


def _pct(new: float, old: float) -> str:
    if old == 0:
        return "+∞%" if new > 0 else "—"
    delta = (new - old) / old * 100
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.0f}%"


def _delta_str(new: float, old: float) -> str:
    d = new - old
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.0f}"


_NO_DATA = (
    "[TOOL RESULT — NO DATA]\n"
    "Row count: 0 for the queried site and period.\n"
    "Do NOT supplement this result with example or estimated data."
)


# ─── site_overview ───────────────────────────────────────────────────────────

async def _site_overview(site_id: str, prop: str, days: int, detail: str) -> str:
    start, end, prev_start, prev_end = _period_dates(days)

    cur = await db.fetchrow(
        "SELECT SUM(clicks) clicks, SUM(impressions) impressions, "
        "AVG(ctr) ctr, AVG(position) position "
        "FROM daily_totals WHERE site_id=$1 AND date BETWEEN $2 AND $3",
        site_id, start, end,
    )
    prev = await db.fetchrow(
        "SELECT SUM(clicks) clicks, SUM(impressions) impressions, "
        "AVG(ctr) ctr, AVG(position) position "
        "FROM daily_totals WHERE site_id=$1 AND date BETWEEN $2 AND $3",
        site_id, prev_start, prev_end,
    )

    if not cur or not cur["clicks"]:
        return (
            f"No Search Console data found for property: {prop}\n"
            f"Period queried: {start} to {end}\n" + _NO_DATA
        )

    cc, ci = int(cur["clicks"] or 0), int(cur["impressions"] or 0)
    pc = int(prev["clicks"] or 0) if prev else 0
    pi = int(prev["impressions"] or 0) if prev else 0
    cctr = float(cur["ctr"] or 0) * 100
    cpos = float(cur["position"] or 0)

    lines = [
        f"## Site Overview — {prop}",
        f"**Period:** {start} to {end} ({days}d) vs {prev_start} to {prev_end}",
        f"*Data collected daily from Google Search Console (not real-time).*\n",
        "### Totals",
        "| Metric | Current | vs Prior |",
        "|--------|---------|----------|",
        f"| Clicks | {cc:,} | {_pct(cc, pc)} |",
        f"| Impressions | {ci:,} | {_pct(ci, pi)} |",
        f"| Avg CTR | {cctr:.2f}% | — |",
        f"| Avg Position | {cpos:.1f} | — |",
        "",
    ]

    top_n = 5 if detail == "summary" else 10

    # Top pages
    top_pages_cur = await db.fetch(
        "SELECT page, SUM(clicks) clicks FROM daily_pages "
        "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY page ORDER BY clicks DESC LIMIT $4",
        site_id, start, end, top_n,
    )
    top_pages_prev = await db.fetch(
        "SELECT page, SUM(clicks) clicks FROM daily_pages "
        "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY page ORDER BY clicks DESC LIMIT 20",
        site_id, prev_start, prev_end,
    )
    prev_page_map = {r["page"]: int(r["clicks"] or 0) for r in top_pages_prev}

    if top_pages_cur:
        lines += [f"### Top {len(top_pages_cur)} Pages", "| Page | Clicks | Δ |", "|------|--------|---|"]
        flags = []
        for r in top_pages_cur:
            cl = int(r["clicks"] or 0)
            prev_cl = prev_page_map.get(r["page"], 0)
            delta = _delta_str(cl, prev_cl)
            short = r["page"].replace("https://", "").replace("http://", "")[:60]
            lines.append(f"| {short} | {cl:,} | {delta} |")
            if prev_cl > 0 and cl < prev_cl * 0.8:
                flags.append(f"- **{short}** lost >{100 - int(cl / prev_cl * 100)}% clicks")
        if flags:
            lines += ["", "**Flags:**"] + flags
        lines.append("")

    # Top queries
    top_q_cur = await db.fetch(
        "SELECT query, SUM(clicks) clicks FROM daily_queries "
        "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY query ORDER BY clicks DESC LIMIT $4",
        site_id, start, end, top_n,
    )
    top_q_prev = await db.fetch(
        "SELECT query, SUM(clicks) clicks FROM daily_queries "
        "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY query ORDER BY clicks DESC LIMIT 20",
        site_id, prev_start, prev_end,
    )
    prev_q_map = {r["query"]: int(r["clicks"] or 0) for r in top_q_prev}

    if top_q_cur:
        lines += [f"### Top {len(top_q_cur)} Queries", "| Query | Clicks | Δ |", "|-------|--------|---|"]
        for r in top_q_cur:
            cl = int(r["clicks"] or 0)
            prev_cl = prev_q_map.get(r["query"], 0)
            delta = _delta_str(cl, prev_cl)
            lines.append(f"| {r['query'][:60]} | {cl:,} | {delta} |")

    return "\n".join(lines)


# ─── analyze_changes ─────────────────────────────────────────────────────────

async def _analyze_changes(site_id: str, prop: str, days: int, detail: str) -> str:
    start, end, prev_start, prev_end = _period_dates(days)

    cur_pages = await db.fetch(
        "SELECT page, SUM(clicks) clicks, SUM(impressions) impressions, "
        "AVG(ctr) ctr, AVG(position) position "
        "FROM daily_pages WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY page",
        site_id, start, end,
    )
    prev_pages = await db.fetch(
        "SELECT page, SUM(clicks) clicks, SUM(impressions) impressions, "
        "AVG(ctr) ctr, AVG(position) position "
        "FROM daily_pages WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY page",
        site_id, prev_start, prev_end,
    )

    if not cur_pages:
        return f"No page data for property: {prop}\nPeriod: {start} to {end}\n" + _NO_DATA

    cur_map = {r["page"]: r for r in cur_pages}
    prev_map = {r["page"]: r for r in prev_pages}
    all_pages = set(cur_map) | set(prev_map)

    total_cur = sum(int(cur_map.get(p, {}).get("clicks", 0) or 0) for p in all_pages)
    total_prev = sum(int(prev_map.get(p, {}).get("clicks", 0) or 0) for p in all_pages)

    drivers = []
    for page in all_pages:
        c = cur_map.get(page)
        p = prev_map.get(page)
        cc = int(c["clicks"] if c else 0 or 0)
        pc = int(p["clicks"] if p else 0 or 0)
        delta = cc - pc
        if delta == 0:
            continue
        drivers.append({
            "page": page, "delta": delta, "cc": cc, "pc": pc,
            "ci": int(c["impressions"] if c else 0 or 0),
            "pi": int(p["impressions"] if p else 0 or 0),
            "cpos": float(c["position"] if c else 0 or 0),
            "ppos": float(p["position"] if p else 0 or 0),
            "cctr": float(c["ctr"] if c else 0 or 0),
            "pctr": float(p["ctr"] if p else 0 or 0),
        })

    drivers.sort(key=lambda x: abs(x["delta"]), reverse=True)
    max_items = 5 if detail == "summary" else 10
    top = drivers[:max_items]

    total_delta = total_cur - total_prev
    lines = [
        f"## Traffic Change Analysis — {prop}",
        f"**Period:** {start} to {end} vs {prev_start} to {prev_end}",
        f"*Based on daily-batch data from GSC, not real-time.*",
        f"**Total click delta:** {_delta_str(total_cur, total_prev)} "
        f"({_pct(total_cur, total_prev)})\n",
    ]

    # Day-over-day spikes
    daily = await db.fetch(
        "SELECT date, SUM(clicks) clicks FROM daily_totals "
        "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY date ORDER BY date",
        site_id, start, end,
    )
    prev_day_clicks = None
    spike_dates = []
    for row in daily:
        dc = int(row["clicks"] or 0)
        if prev_day_clicks and prev_day_clicks > 0:
            pct = (dc - prev_day_clicks) / prev_day_clicks
            if abs(pct) > 0.15:
                spike_dates.append((str(row["date"]), pct))
        prev_day_clicks = dc

    if spike_dates:
        lines.append("### Sudden Shifts")
        for d, pct in spike_dates[:3]:
            arrow = "up" if pct > 0 else "down"
            lines.append(f"- {d}: {abs(pct)*100:.0f}% {arrow} day-over-day — possible algorithm update or external event")
        lines.append("")

    if not top:
        lines.append("No significant page-level changes detected.")
        return "\n".join(lines)

    lines += [f"### Top {len(top)} Page Drivers (by |delta clicks|)", ""]
    for d in top:
        short = d["page"].replace("https://", "").replace("http://", "")[:70]
        contribution = (abs(d["delta"]) / max(abs(total_delta), 1)) * 100

        causes = []
        pos_drop = d["ppos"] > 0 and d["cpos"] > d["ppos"] + 1
        ctr_drop = d["pctr"] > 0 and d["cctr"] < d["pctr"] * 0.85
        imp_drop = d["pi"] > 0 and d["ci"] < d["pi"] * 0.85
        ai_flag = imp_drop and ctr_drop and not pos_drop

        if pos_drop:
            causes.append(f"position dropped {d['ppos']:.1f} -> {d['cpos']:.1f} [high confidence]")
        if ctr_drop and not pos_drop:
            causes.append(f"CTR fell {d['pctr']*100:.1f}% -> {d['cctr']*100:.1f}% at stable position [medium confidence]")
        if imp_drop and not pos_drop:
            causes.append(f"impressions down {d['pi']:,} -> {d['ci']:,} [medium confidence]")
        if ai_flag:
            causes.append(
                "possible AI Overview impact — CTR and impressions both fell "
                "while position stable [estimate, NOT confirmed fact, low confidence]"
            )
        if not causes:
            causes.append("cause unclear — check for content or indexing changes")

        arrow = "DOWN" if d["delta"] < 0 else "UP"
        lines.append(
            f"**{short}**\n"
            f"{arrow} {abs(d['delta']):,} clicks ({_pct(d['cc'], d['pc'])}) "
            f"| {contribution:.0f}% of total change\n"
            f"Likely cause: {'; '.join(causes)}\n"
        )

    return "\n".join(lines)


# ─── ai_visibility_snapshot ──────────────────────────────────────────────────

async def _ai_visibility_snapshot(site_id: str, prop: str, days: int) -> str:
    start, end, prev_start, prev_end = _period_dates(days)

    cur_ai = await db.fetch(
        "SELECT appearance_type, SUM(clicks) clicks, SUM(impressions) impressions "
        "FROM daily_ai_appearance WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY appearance_type ORDER BY impressions DESC",
        site_id, start, end,
    )
    prev_ai = await db.fetch(
        "SELECT appearance_type, SUM(clicks) clicks, SUM(impressions) impressions "
        "FROM daily_ai_appearance WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
        "GROUP BY appearance_type ORDER BY impressions DESC",
        site_id, prev_start, prev_end,
    )

    if not cur_ai:
        return (
            f"No AI appearance data for property: {prop}\n"
            f"Period: {start} to {end}\n" + _NO_DATA + "\n"
            "Possible reasons: site has no AI Overview appearances, "
            "or Google has not exposed this data via the API for this property."
        )

    prev_map = {r["appearance_type"]: r for r in prev_ai}
    lines = [
        f"## AI Search Visibility — {prop}",
        f"**Period:** {start} to {end} ({days}d)",
        "*Data from GSC searchAppearance dimension. AI-specific types may be limited.*\n",
        "### Appearances by Type",
        "| Type | Impressions | Clicks | vs Prior |",
        "|------|-------------|--------|----------|",
    ]

    for r in cur_ai:
        at = r["appearance_type"]
        ci = int(r["impressions"] or 0)
        cc = int(r["clicks"] or 0)
        p = prev_map.get(at)
        pi = int(p["impressions"] or 0) if p else 0
        lines.append(f"| {at} | {ci:,} | {cc:,} | {_pct(ci, pi)} |")

    lines.append("")
    ai_types = {r["appearance_type"] for r in cur_ai}
    ai_related = [t for t in ai_types if any(k in t.upper() for k in ["AI", "OVERVIEW", "SGE"])]
    if ai_related:
        lines += [
            "### Note",
            f"AI-related appearance types detected: {', '.join(ai_related)}",
            "Page-level AI appearance breakdown is not available via the GSC API. "
            "Cross-referencing with CTR data is estimated at site level only.",
        ]

    return "\n".join(lines)


# ─── low_hanging_fruit ───────────────────────────────────────────────────────

async def _low_hanging_fruit(site_id: str, prop: str, min_impressions: int) -> str:
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=27)

    rows = await db.fetch(
        "SELECT q.query, p.page, AVG(q.position) avg_pos, "
        "SUM(q.impressions) impressions, AVG(q.ctr) avg_ctr "
        "FROM daily_queries q "
        "JOIN daily_pages p ON p.site_id = q.site_id AND p.date = q.date "
        "WHERE q.site_id = $1 AND q.date BETWEEN $2 AND $3 "
        "AND q.position BETWEEN 8 AND 15 "
        "GROUP BY q.query, p.page "
        "HAVING SUM(q.impressions) >= $4 "
        "ORDER BY SUM(q.impressions) DESC LIMIT 50",
        site_id, start, end, min_impressions,
    )

    if not rows:
        rows = await db.fetch(
            "SELECT query, NULL::text AS page, AVG(position) avg_pos, "
            "SUM(impressions) impressions, AVG(ctr) avg_ctr "
            "FROM daily_queries "
            "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
            "AND position BETWEEN 8 AND 15 "
            "GROUP BY query "
            "HAVING SUM(impressions) >= $4 "
            "ORDER BY impressions DESC LIMIT 50",
            site_id, start, end, min_impressions,
        )

    if not rows:
        return (
            f"No queries found for property: {prop}\n"
            f"Filter: position 8-15, impressions >= {min_impressions}, last 28 days\n"
            + _NO_DATA
        )

    TOP5_CTR = 0.15

    def est_upside(imp: int, ctr: float) -> int:
        return int(imp * max(TOP5_CTR - ctr, 0))

    results = []
    for r in rows:
        pos = float(r["avg_pos"])
        imp = int(r["impressions"])
        ctr = float(r["avg_ctr"])
        results.append({
            "query": r["query"], "page": r["page"],
            "pos": pos, "imp": imp, "ctr": ctr,
            "upside": est_upside(imp, ctr),
        })

    results.sort(key=lambda x: x["upside"], reverse=True)
    top10 = results[:10]

    lines = [
        f"## Low-Hanging Fruit — {prop}",
        f"**Queries ranked 8-15, >={min_impressions} impressions (last 28 days)**",
        "*Upside estimate assumes reaching avg top-5 CTR of ~15%. Actual results vary.*\n",
        "| Query | Position | Impressions | Est. extra clicks/mo |",
        "|-------|----------|-------------|----------------------|",
    ]
    for r in top10:
        lines.append(f"| {r['query'][:55]} | {r['pos']:.1f} | {r['imp']:,} | +{r['upside']:,} |")

    lines += [
        "",
        "### Optimization hints",
        "- **Position 8-10:** Update title tag and intro to better match query intent. Add an FAQ section.",
        "- **Position 11-15:** Content likely needs more depth. Expand coverage and improve internal linking.",
    ]

    return "\n".join(lines)


# ─── page_quick_audit ────────────────────────────────────────────────────────

def _extract_text(html: str) -> str:
    """Strip tags, collapse whitespace — rough visible text."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first(pattern: str, html: str, flags: int = re.S | re.I) -> str | None:
    m = re.search(pattern, html, flags)
    return m.group(1).strip() if m else None


async def _page_quick_audit(url: str, site_id: str) -> str:
    if not url:
        return "Error: 'url' parameter is required."
    if not url.startswith(("http://", "https://")):
        return "Error: 'url' must start with http:// or https://"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; GSCAnalystBot/1.0; "
            "+https://gsc-analyst.app/bot)"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=10.0
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return f"Fetch timed out after 10s for: {url}\nSome servers block automated requests."
    except httpx.RequestError as e:
        return f"Could not reach {url}: {type(e).__name__}."

    if resp.status_code == 403:
        return f"Access denied (403) for: {url}\nThis server blocks automated fetches."
    if resp.status_code == 404:
        return f"Page not found (404): {url}"
    if resp.status_code != 200:
        return f"Unexpected response {resp.status_code} from: {url}"

    html = resp.text

    # ── Extract elements ──────────────────────────────────────────────────
    title = _first(r"<title[^>]*>(.*?)</title>", html) or "(no title)"

    h1 = _first(r"<h1[^>]*>(.*?)</h1>", html)
    if h1:
        h1 = re.sub(r"<[^>]+>", "", h1).strip()

    h2_matches = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S | re.I)
    h2s = [re.sub(r"<[^>]+>", "", h).strip() for h in h2_matches[:10]]

    meta_desc = _first(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html
    ) or _first(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', html
    )

    visible_text = _extract_text(html)
    word_count = len(visible_text.split())

    # Structured data
    has_schema = bool(re.search(
        r'<script[^>]+type=["\']application/ld\+json["\']', html, re.I
    ))
    schema_type = None
    if has_schema:
        schema_type = _first(r'"@type"\s*:\s*"([^"]+)"', html)

    # Robots noindex
    is_noindex = bool(re.search(
        r'<meta[^>]+name=["\']robots["\'][^>]+content=[^>]*noindex', html, re.I
    ) or re.search(
        r'<meta[^>]+content=[^>]*noindex[^>]+name=["\']robots["\']', html, re.I
    ))

    # ── GSC cross-reference ───────────────────────────────────────────────
    gsc_row = None
    if site_id:
        end = date.today() - timedelta(days=2)
        start = end - timedelta(days=27)
        # Try exact URL match; also try with/without trailing slash
        alt_url = url.rstrip("/") if url.endswith("/") else url + "/"
        gsc_row = await db.fetchrow(
            "SELECT SUM(clicks) clicks, SUM(impressions) impressions, "
            "AVG(position) position "
            "FROM daily_pages "
            "WHERE site_id=$1 AND date BETWEEN $2 AND $3 "
            "AND page IN ($4, $5)",
            site_id, start, end, url, alt_url,
        )

    # ── Format output ─────────────────────────────────────────────────────
    lines = [
        f"## Page Audit — {url}",
        "",
        f"**Title:** {title}",
        f"**H1:** {h1 or '(none found)'}",
    ]

    if h2s:
        lines.append(f"**H2s ({len(h2s)}):** " + " / ".join(
            f'"{h[:60]}"' for h in h2s
        ))
    else:
        lines.append("**H2s:** (none found)")

    lines += [
        f"**Meta description:** {meta_desc[:160] if meta_desc else '(missing)'}",
        f"**Word count (approx):** {word_count:,}",
        f"**Structured data:** {'Yes — type: ' + schema_type if has_schema and schema_type else ('Yes (type unknown)' if has_schema else 'No')}",
        f"**Noindex:** {'Yes — page is blocked from search indexing' if is_noindex else 'No'}",
    ]

    # GSC metrics
    if gsc_row and gsc_row["clicks"] is not None:
        lines += [
            "",
            "### GSC Metrics (last 28 days)",
            f"Clicks: {int(gsc_row['clicks']):,} | "
            f"Impressions: {int(gsc_row['impressions']):,} | "
            f"Avg position: {float(gsc_row['position']):.1f}",
        ]
    else:
        lines += [
            "",
            "### GSC Metrics",
            "No data found for this URL in the last 28 days "
            "(page may not have received clicks, or URL doesn't match exactly).",
        ]

    return "\n".join(lines)
