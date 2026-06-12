"""Thin wrapper around Google Search Console Search Analytics API."""
import time
import logging
from datetime import date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from app.crypto import decrypt_token

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def build_service(refresh_token_enc: str):
    import os
    creds = Credentials(
        token=None,
        refresh_token=decrypt_token(refresh_token_enc),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def query_search_analytics(
    service,
    property_uri: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    row_limit: int = 500,
    search_appearance: str | None = None,
) -> list[dict]:
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": row_limit,
        "dataState": "final",
    }
    if search_appearance:
        body["dimensionFilterGroups"] = [{
            "filters": [{
                "dimension": "searchAppearance",
                "operator": "equals",
                "expression": search_appearance,
            }]
        }]

    results = []
    start_row = 0
    while True:
        body["startRow"] = start_row
        resp = (
            service.searchanalytics()
            .query(siteUrl=property_uri, body=body)
            .execute()
        )
        rows = resp.get("rows", [])
        results.extend(rows)
        if len(rows) < row_limit:
            break
        start_row += row_limit
        time.sleep(0.5)  # respect QPS limits

    return results


def list_sites(service) -> list[str]:
    resp = service.sites().list().execute()
    return [s["siteUrl"] for s in resp.get("siteEntry", [])]


def get_search_appearance_types(
    service,
    property_uri: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Collect data per searchAppearance type."""
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["searchAppearance"],
        "rowLimit": 100,
        "dataState": "final",
    }
    try:
        resp = (
            service.searchanalytics()
            .query(siteUrl=property_uri, body=body)
            .execute()
        )
        return resp.get("rows", [])
    except Exception as e:
        log.warning("searchAppearance query failed: %s", e)
        return []
