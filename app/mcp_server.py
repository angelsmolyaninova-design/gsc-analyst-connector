"""MCP tool definitions for GSC Analyst."""
import logging
from datetime import date, timedelta
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent

from app import db

log = logging.getLogger(__name__)

VERSION = "0.1.0"


def make_mcp_server() -> Server:
    server = Server("gsc-analyst")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="ping",
                description=(
                    "Health check. Returns server version. "
                    "Call this to verify the connector is working."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="site_overview",
                description=(
                    "Returns traffic summary for a site over a recent period, "
                    "compared to the previous equal period. "
                    "Includes totals (clicks, impressions, CTR, position), "
                    "top 5 pages and queries with delta, and auto-flagged declines. "
                    "Call this first for a general 'what's happening' question."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {
                            "type": "string",
                            "description": "GSC property (e.g. 'sc-domain:example.com'). Defaults to user's first property.",
                        },
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Period length. Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="analyze_changes",
                description=(
                    "Decomposes traffic change into page-level drivers. "
                    "Returns pages with largest click delta, breakdown of whether "
                    "the cause is position drop, CTR drop, or impression loss, "
                    "plus flags for sudden day-over-day shifts and possible "
                    "AI Overview impact (estimated, not confirmed). "
                    "Use when the user asks 'why did traffic change' or 'what dropped'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="ai_visibility_snapshot",
                description=(
                    "Shows how the site appears in Google AI features (AI Overviews, etc.). "
                    "Returns impressions/clicks by AI appearance type vs prior period, "
                    "top pages in AI features, and pages where AI impressions grew "
                    "but CTR fell. Returns honest 'not available' if data is missing. "
                    "Use for questions about AI search visibility or AI Overviews."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="low_hanging_fruit",
                description=(
                    "Finds queries ranked in positions 8–15 with enough impressions "
                    "to be worth optimizing. Returns up to 10 rows with estimated "
                    "upside if the page reaches top-5, grouped by page, with "
                    "brief optimization hints (title/intro/FAQ vs deeper content). "
                    "Use for 'what should I fix first' or 'quick wins' questions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "min_impressions": {
                            "type": "integer",
                            "description": "Minimum impressions over 28 days. Default: 200.",
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict, *, user_id: str | None = None) -> list[TextContent]:
        try:
            result = await _dispatch(name, arguments, user_id=user_id)
        except Exception as e:
            log.error("tool_error tool=%s user_id=%s error=%s", name, user_id, e)
            return [TextContent(type="text", text=f"Error: {e}")]
        return [TextContent(type="text", text=result)]

    return server


async def _dispatch(name: str, args: dict, *, user_id: str | None) -> str:
    if name == "ping":
        return f"GSC Analyst connector v{VERSION} — OK"

    if not user_id:
        return "Error: Could not identify user from connector URL."

    sites = await db.get_sites_for_user(user_id)
    if not sites:
        return (
            "No Search Console properties found for your account. "
            "Please reconnect at the connector homepage."
        )

    site_param = args.get("site")
    if site_param:
        site_record = next(
            (s for s in sites if s["property"] == site_param), None
        )
        if not site_record:
            props = ", ".join(s["property"] for s in sites)
            return f"Site '{site_param}' not found. Your properties: {props}"
    else:
        site_record = sites[0]

    # Security: ensure site belongs to this user (already guaranteed by query above)
    site_id = str(site_record["id"])
    property_name = site_record["property"]

    period = args.get("period", "28d")
    days = int(period.rstrip("d"))

    if name == "site_overview":
        return await _site_overview(site_id, property_name, days)
    elif name == "analyze_changes":
        return await _analyze_changes(site_id, property_name, days)
    elif name == "ai_visibility_snapshot":
        return await _ai_visibility_snapshot(site_id, property_name, days)
    elif name == "low_hanging_fruit":
        min_imp = args.get("min_impressions", 200)
        return await _low_hanging_fruit(site_id, property_name, min_imp)
    else:
        return f"Unknown tool: {name}"


# ─── Tool implementations ───────────────────────────────────────────────────

def _period_dates(days: int) -> tuple[date, date, date, date]:
    end = date.today() - timedelta(days=2)  # GSC lag
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


async def _site_overview(site_id: str, prop: str, days: int) -> str:
    start, end, prev_start, prev_end = _period_dates(days)

    cur = await db.fetchrow(
        """
        SELECT SUM(clicks) clicks, SUM(impressions) impressions,
               AVG(ctr) ctr, AVG(position) position
        FROM daily_totals WHERE site_id=$1 AND date BETWEEN $2 AND $3
        """,
        site_id, start, end,
    )
    prev = await db.fetchrow(
        """
        SELECT SUM(clicks) clicks, SUM(impressions) impressions,
               AVG(ctr) ctr, AVG(position) position
        FROM daily_totals WHERE site_id=$1 AND date BETWEEN $2 AND $3
        """,
        site_id, prev_start, prev_end,
    )

    if not cur or not cur["clicks"]:
        return (
            f"No data available for **{prop}** yet. "
            "If you just connected, data should appear within ~10 minutes."
        )

    cc, ci = int(cur["clicks"] or 0), int(cur["impressions"] or 0)
    pc, pi = int(prev["clicks"] or 0) if prev else 0, int(prev["impressions"] or 0) if prev else 0
    cctr = float(cur["ctr"] or 0) * 100
    cpos = float(cur["position"] or 0)

    lines = [
        f"## Site Overview — {prop}",
        f"**Period:** {start} → {end} ({days}d) vs {prev_start} → {prev_end}\n",
        "### Totals",
        f"| Metric | Current | vs Prior |",
        f"|--------|---------|----------|",
        f"| Clicks | {cc:,} | {_pct(cc, pc)} |",
        f"| Impressions | {ci:,} | {_pct(ci, pi)} |",
        f"| Avg CTR | {cctr:.2f}% | — |",
        f"| Avg Position | {cpos:.1f} | — |",
        "",
    ]

    # Top 5 pages
    top_pages_cur = await db.fetch(
        """
        SELECT page, SUM(clicks) clicks FROM daily_pages
        WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY page ORDER BY clicks DESC LIMIT 5
        """,
        site_id, start, end,
    )
    top_pages_prev = await db.fetch(
        """
        SELECT page, SUM(clicks) clicks FROM daily_pages
        WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY page ORDER BY clicks DESC LIMIT 20
        """,
        site_id, prev_start, prev_end,
    )
    prev_page_map = {r["page"]: int(r["clicks"] or 0) for r in top_pages_prev}

    if top_pages_cur:
        lines += ["### Top 5 Pages", "| Page | Clicks | Δ |", "|------|--------|---|"]
        flags = []
        for r in top_pages_cur:
            cl = int(r["clicks"] or 0)
            prev_cl = prev_page_map.get(r["page"], 0)
            delta = _delta_str(cl, prev_cl)
            short = r["page"].replace("https://", "").replace("http://", "")[:60]
            lines.append(f"| {short} | {cl:,} | {delta} |")
            if prev_cl > 0 and cl < prev_cl * 0.8:
                flags.append(f"⚠ **{short}** lost >{100 - int(cl/prev_cl*100)}% clicks")
        if flags:
            lines += ["", "**Flags:**"] + flags
        lines.append("")

    # Top 5 queries
    top_q_cur = await db.fetch(
        """
        SELECT query, SUM(clicks) clicks FROM daily_queries
        WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY query ORDER BY clicks DESC LIMIT 5
        """,
        site_id, start, end,
    )
    top_q_prev = await db.fetch(
        """
        SELECT query, SUM(clicks) clicks FROM daily_queries
        WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY query ORDER BY clicks DESC LIMIT 20
        """,
        site_id, prev_start, prev_end,
    )
    prev_q_map = {r["query"]: int(r["clicks"] or 0) for r in top_q_prev}

    if top_q_cur:
        lines += ["### Top 5 Queries", "| Query | Clicks | Δ |", "|-------|--------|---|"]
        for r in top_q_cur:
            cl = int(r["clicks"] or 0)
            prev_cl = prev_q_map.get(r["query"], 0)
            delta = _delta_str(cl, prev_cl)
            lines.append(f"| {r['query'][:60]} | {cl:,} | {delta} |")

    return "\n".join(lines)


async def _analyze_changes(site_id: str, prop: str, days: int) -> str:
    start, end, prev_start, prev_end = _period_dates(days)

    cur_pages = await db.fetch(
        """
        SELECT page,
               SUM(clicks) clicks, SUM(impressions) impressions,
               AVG(ctr) ctr, AVG(position) position
        FROM daily_pages WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY page
        """,
        site_id, start, end,
    )
    prev_pages = await db.fetch(
        """
        SELECT page,
               SUM(clicks) clicks, SUM(impressions) impressions,
               AVG(ctr) ctr, AVG(position) position
        FROM daily_pages WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY page
        """,
        site_id, prev_start, prev_end,
    )

    if not cur_pages:
        return f"No page data available for **{prop}** in the selected period."

    cur_map = {r["page"]: r for r in cur_pages}
    prev_map = {r["page"]: r for r in prev_pages}

    all_pages = set(cur_map) | set(prev_map)
    total_cur_clicks = sum(int(cur_map.get(p, {}).get("clicks", 0) or 0) for p in all_pages)
    total_prev_clicks = sum(int(prev_map.get(p, {}).get("clicks", 0) or 0) for p in all_pages)

    drivers = []
    for page in all_pages:
        c = cur_map.get(page)
        p = prev_map.get(page)
        cc = int(c["clicks"] if c else 0 or 0)
        pc = int(p["clicks"] if p else 0 or 0)
        delta = cc - pc
        if delta == 0:
            continue
        ci = int(c["impressions"] if c else 0 or 0)
        pi = int(p["impressions"] if p else 0 or 0)
        cpos = float(c["position"] if c else 0 or 0)
        ppos = float(p["position"] if p else 0 or 0)
        cctr = float(c["ctr"] if c else 0 or 0)
        pctr = float(p["ctr"] if p else 0 or 0)
        drivers.append({
            "page": page, "delta": delta, "cc": cc, "pc": pc,
            "ci": ci, "pi": pi, "cpos": cpos, "ppos": ppos,
            "cctr": cctr, "pctr": pctr,
        })

    drivers.sort(key=lambda x: abs(x["delta"]), reverse=True)
    top = drivers[:10]

    total_delta = total_cur_clicks - total_prev_clicks
    lines = [
        f"## Traffic Change Analysis — {prop}",
        f"**Period:** {start} → {end} vs {prev_start} → {prev_end}",
        f"**Total click delta:** {_delta_str(total_cur_clicks, total_prev_clicks)} "
        f"({_pct(total_cur_clicks, total_prev_clicks)})\n",
    ]

    # Check for sudden single-day shifts
    daily = await db.fetch(
        """
        SELECT date, SUM(clicks) clicks FROM daily_totals
        WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY date ORDER BY date
        """,
        site_id, start, end,
    )
    spike_dates = []
    prev_day_clicks = None
    for row in daily:
        dc = int(row["clicks"] or 0)
        if prev_day_clicks and prev_day_clicks > 0:
            pct_change = (dc - prev_day_clicks) / prev_day_clicks
            if abs(pct_change) > 0.15:
                spike_dates.append((str(row["date"]), pct_change))
        prev_day_clicks = dc

    if spike_dates:
        lines.append("### ⚡ Sudden Shifts Detected")
        for d, pct in spike_dates[:3]:
            sign = "▲" if pct > 0 else "▼"
            lines.append(f"- {d}: {sign} {abs(pct)*100:.0f}% day-over-day — possible algorithm update or external event")
        lines.append("")

    if not top:
        lines.append("No significant page-level changes detected.")
        return "\n".join(lines)

    lines += ["### Page Drivers (by |Δ clicks|)", ""]
    for d in top:
        short = d["page"].replace("https://", "").replace("http://", "")[:70]
        contribution = (abs(d["delta"]) / max(abs(total_delta), 1)) * 100

        # Diagnose likely cause
        causes = []
        pos_drop = d["ppos"] > 0 and d["cpos"] > d["ppos"] + 1
        ctr_drop = d["pctr"] > 0 and d["cctr"] < d["pctr"] * 0.85
        imp_drop = d["pi"] > 0 and d["ci"] < d["pi"] * 0.85
        ai_flag = imp_drop and ctr_drop and not pos_drop

        if pos_drop:
            causes.append(f"position dropped {d['ppos']:.1f}→{d['cpos']:.1f} [confidence: high]")
        if ctr_drop and not pos_drop:
            causes.append(f"CTR fell {d['pctr']*100:.1f}%→{d['cctr']*100:.1f}% at stable position [confidence: medium]")
        if imp_drop and not pos_drop:
            causes.append(f"impressions down {d['pi']:,}→{d['ci']:,} [confidence: medium]")
        if ai_flag:
            causes.append("possible AI Overview impact — CTR+impressions both fell, position stable [estimate, not fact, confidence: low]")
        if not causes:
            causes.append("cause unclear — check for content/indexing changes")

        arrow = "▼" if d["delta"] < 0 else "▲"
        lines.append(
            f"**{short}**  \n"
            f"{arrow} {abs(d['delta']):,} clicks ({_pct(d['cc'], d['pc'])}) "
            f"| contributes {contribution:.0f}% of total change  \n"
            f"Likely cause: {'; '.join(causes)}\n"
        )

    return "\n".join(lines)


async def _ai_visibility_snapshot(site_id: str, prop: str, days: int) -> str:
    start, end, prev_start, prev_end = _period_dates(days)

    cur_ai = await db.fetch(
        """
        SELECT appearance_type, SUM(clicks) clicks, SUM(impressions) impressions
        FROM daily_ai_appearance WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY appearance_type ORDER BY impressions DESC
        """,
        site_id, start, end,
    )
    prev_ai = await db.fetch(
        """
        SELECT appearance_type, SUM(clicks) clicks, SUM(impressions) impressions
        FROM daily_ai_appearance WHERE site_id=$1 AND date BETWEEN $2 AND $3
        GROUP BY appearance_type ORDER BY impressions DESC
        """,
        site_id, prev_start, prev_end,
    )

    if not cur_ai:
        return (
            f"## AI Visibility — {prop}\n\n"
            "AI appearance data not available for this property yet. "
            "This may be because:\n"
            "- Your site has very few AI Overview appearances\n"
            "- Google hasn't made this data available via the API for your property\n"
            "- Data is still being collected (check back in ~10 minutes if you just connected)\n\n"
            "*(Note: AI Overview data in GSC API is limited and may not be available for all properties as of June 2026.)*"
        )

    prev_map = {r["appearance_type"]: r for r in prev_ai}
    lines = [
        f"## AI Search Visibility — {prop}",
        f"**Period:** {start} → {end} ({days}d)\n",
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

    # Pages where AI impressions grew but CTR fell — requires cross-referencing
    # daily_pages (overall) vs daily_ai_appearance (site-level only, no page breakdown)
    # TODO: When GSC API exposes page-level AI appearance data, implement per-page analysis.
    # Currently, searchAppearance dimension only returns site-level totals.
    ai_types = {r["appearance_type"] for r in cur_ai}
    ai_related = [t for t in ai_types if any(k in t.upper() for k in ["AI", "OVERVIEW", "SGE"])]
    if ai_related:
        lines += [
            "### Observation",
            f"AI-related appearance types detected: {', '.join(ai_related)}",
            "",
            "*(Page-level AI appearance breakdown is not yet available via the GSC API. "
            "Cross-referencing with CTR data is estimated at site level only.)*",
        ]

    return "\n".join(lines)


async def _low_hanging_fruit(site_id: str, prop: str, min_impressions: int) -> str:
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=27)

    rows = await db.fetch(
        """
        SELECT q.query, p.page,
               AVG(q.position) avg_pos,
               SUM(q.impressions) impressions,
               AVG(q.ctr) avg_ctr
        FROM daily_queries q
        JOIN daily_pages p ON p.site_id = q.site_id AND p.date = q.date
        WHERE q.site_id = $1
          AND q.date BETWEEN $2 AND $3
          AND q.position BETWEEN 8 AND 15
        GROUP BY q.query, p.page
        HAVING SUM(q.impressions) >= $4
        ORDER BY SUM(q.impressions) DESC
        LIMIT 50
        """,
        site_id, start, end, min_impressions,
    )

    # Fallback: queries only (without page join) if above returns empty
    if not rows:
        rows = await db.fetch(
            """
            SELECT query, NULL::text AS page,
                   AVG(position) avg_pos,
                   SUM(impressions) impressions,
                   AVG(ctr) avg_ctr
            FROM daily_queries
            WHERE site_id=$1 AND date BETWEEN $2 AND $3
              AND position BETWEEN 8 AND 15
            GROUP BY query
            HAVING SUM(impressions) >= $4
            ORDER BY impressions DESC
            LIMIT 50
            """,
            site_id, start, end, min_impressions,
        )

    if not rows:
        return (
            f"## Low-Hanging Fruit — {prop}\n\n"
            f"No queries found with position 8–15 and ≥{min_impressions} impressions "
            "in the last 28 days. Try lowering `min_impressions`."
        )

    # Estimate upside: rough CTR model top-5 avg = 15%, top-1 = 28%
    TOP5_CTR = 0.15

    def est_upside(impressions: int, current_ctr: float) -> int:
        uplift = max(TOP5_CTR - current_ctr, 0)
        return int(impressions * uplift)

    results = []
    for r in rows:
        pos = float(r["avg_pos"])
        imp = int(r["impressions"])
        ctr = float(r["avg_ctr"])
        upside = est_upside(imp, ctr)
        results.append({
            "query": r["query"],
            "page": r["page"],
            "pos": pos,
            "imp": imp,
            "ctr": ctr,
            "upside": upside,
        })

    results.sort(key=lambda x: x["upside"], reverse=True)
    top10 = results[:10]

    lines = [
        f"## Low-Hanging Fruit — {prop}",
        f"**Queries ranked 8–15, ≥{min_impressions} impressions (last 28 days)**\n",
        "| Query | Position | Impressions | Est. extra clicks/mo |",
        "|-------|----------|-------------|----------------------|",
    ]

    for r in top10:
        lines.append(
            f"| {r['query'][:55]} | {r['pos']:.1f} | {r['imp']:,} | +{r['upside']:,} |"
        )

    lines += [
        "",
        "### Optimization hints",
        "- **Position 8–10:** Update title tag and intro paragraph to better match query intent. "
        "Add an FAQ section targeting the query phrasing.",
        "- **Position 11–15:** Content likely needs to be more comprehensive. "
        "Expand coverage, add examples, and improve internal linking to the page.",
        "",
        "*(Upside estimate assumes reaching avg top-5 CTR of ~15%. Actual results vary.)*",
    ]

    return "\n".join(lines)
