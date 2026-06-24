"""
GA4 -> Supabase sync for Marginal Pilgrims.

Pulls data from the Google Analytics Data API and writes it into six
Supabase tables, replacing the manual CSV-export-and-paste workflow.

Two table "shapes" are handled differently:

1. Snapshot tables (no date column): ga_metrics, ga_acquisition,
   ga_demographics_country, ga_demographics_city
   -> wiped and fully replaced every run (matches existing manual pattern).

2. Date-indexed tables: ga_tech_overview, ga_traffic_acquisition
   -> rows within the rolling lookback window are deleted and replaced,
      older rows are left alone.

Env vars required:
  GA4_PROPERTY_ID            e.g. "506718409" (no "properties/" prefix)
  GA4_SERVICE_ACCOUNT_JSON   full contents of the service account key file
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""

import os
import json
import datetime as dt

from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from supabase import create_client

LOOKBACK_DAYS = 8  # rolling window, re-pulled and overwritten every run


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def get_ga_client():
    creds_info = json.loads(os.environ["GA4_SERVICE_ACCOUNT_JSON"])
    credentials = service_account.Credentials.from_service_account_info(creds_info)
    return BetaAnalyticsDataClient(credentials=credentials)


def get_supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


PROPERTY = f"properties/{os.environ.get('GA4_PROPERTY_ID', '')}"


def run_report(client, dimensions, metrics, start_date, end_date):
    """Run a GA4 report and return rows as list of dicts keyed by name."""
    request = RunReportRequest(
        property=PROPERTY,
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=100000,
    )
    response = client.run_report(request)

    rows = []
    for row in response.rows:
        record = {}
        for i, dim in enumerate(dimensions):
            record[dim] = row.dimension_values[i].value
        for i, metric in enumerate(metrics):
            val = row.metric_values[i].value
            try:
                record[metric] = float(val)
            except (TypeError, ValueError):
                record[metric] = 0.0
        rows.append(record)
    return rows


def safe_div(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator


def ga_date_to_iso(yyyymmdd: str) -> str:
    return dt.datetime.strptime(yyyymmdd, "%Y%m%d").date().isoformat()


# ---------------------------------------------------------------------------
# Snapshot tables (full wipe + replace each run)
# ---------------------------------------------------------------------------

def sync_ga_metrics(ga, sb, start_date, end_date):
    rows = run_report(
        ga,
        dimensions=["pagePath"],
        metrics=["screenPageViews", "activeUsers", "screenPageViewsPerUser",
                 "userEngagementDuration", "eventCount"],
        start_date=start_date,
        end_date=end_date,
    )

    payload = []
    for r in rows:
        active_users = r["activeUsers"]
        payload.append({
            "ga_page_path": r["pagePath"],
            "views": int(r["screenPageViews"]),
            "active_users": int(active_users),
            "views_per_user": r["screenPageViewsPerUser"],
            "avg_engagement_time": safe_div(r["userEngagementDuration"], active_users),
            "event_count": int(r["eventCount"]),
            "recorded_at": dt.datetime.utcnow().isoformat(),
        })

    sb.table("ga_metrics").delete().neq("id", 0).execute()
    if payload:
        sb.table("ga_metrics").insert(payload).execute()
    print(f"ga_metrics: wrote {len(payload)} rows")


def sync_ga_acquisition(ga, sb, start_date, end_date):
    rows = run_report(
        ga,
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["totalUsers", "newUsers", "engagedSessions",
                 "userEngagementDuration", "eventCount", "keyEvents"],
        start_date=start_date,
        end_date=end_date,
    )

    payload = []
    for r in rows:
        total_users = r["totalUsers"]
        new_users = r["newUsers"]
        payload.append({
            "channel_group": r["sessionDefaultChannelGroup"],
            "total_users": int(total_users),
            "new_users": int(new_users),
            "returning_users": int(max(total_users - new_users, 0)),
            "avg_engagement_time": safe_div(r["userEngagementDuration"], total_users),
            "engaged_sessions_per_user": safe_div(r["engagedSessions"], total_users),
            "event_count": int(r["eventCount"]),
            "key_events": int(r["keyEvents"]),
            "user_key_event_rate": safe_div(r["keyEvents"], total_users),
            "recorded_at": dt.datetime.utcnow().isoformat(),
        })

    sb.table("ga_acquisition").delete().neq("id", 0).execute()
    if payload:
        sb.table("ga_acquisition").insert(payload).execute()
    print(f"ga_acquisition: wrote {len(payload)} rows")


def sync_ga_demographics(ga, sb, start_date, end_date, dimension, table_name, column_name):
    rows = run_report(
        ga,
        dimensions=[dimension],
        metrics=["activeUsers", "newUsers", "engagedSessions", "engagementRate",
                 "userEngagementDuration", "eventCount", "keyEvents", "totalRevenue"],
        start_date=start_date,
        end_date=end_date,
    )

    payload = []
    for r in rows:
        active_users = r["activeUsers"]
        payload.append({
            column_name: r[dimension],
            "active_users": int(active_users),
            "new_users": int(r["newUsers"]),
            "engaged_sessions": int(r["engagedSessions"]),
            "engagement_rate": r["engagementRate"],
            "engaged_sessions_per_user": safe_div(r["engagedSessions"], active_users),
            "avg_engagement_time": safe_div(r["userEngagementDuration"], active_users),
            "event_count": int(r["eventCount"]),
            "key_events": int(r["keyEvents"]),
            "user_key_event_rate": safe_div(r["keyEvents"], active_users),
            "total_revenue": r["totalRevenue"],
            "recorded_at": dt.datetime.utcnow().isoformat(),
        })

    sb.table(table_name).delete().neq("id", 0).execute()
    if payload:
        sb.table(table_name).insert(payload).execute()
    print(f"{table_name}: wrote {len(payload)} rows")


# ---------------------------------------------------------------------------
# Date-indexed tables (delete window + replace)
# ---------------------------------------------------------------------------

def sync_ga_tech_overview(ga, sb, start_date, end_date, window_start_iso):
    payload = []
    dimension_map = {
        "device": "deviceCategory",
        "os": "operatingSystem",
        "browser": "browser",
    }

    for dimension_type, ga_dimension in dimension_map.items():
        rows = run_report(
            ga,
            dimensions=["date", ga_dimension],
            metrics=["activeUsers"],
            start_date=start_date,
            end_date=end_date,
        )
        for r in rows:
            payload.append({
                "dimension_type": dimension_type,
                "dimension_value": r[ga_dimension],
                "active_users": int(r["activeUsers"]),
                "snapshot_date": ga_date_to_iso(r["date"]),
                "recorded_at": dt.datetime.utcnow().isoformat(),
            })

    sb.table("ga_tech_overview").delete().gte("snapshot_date", window_start_iso).execute()
    if payload:
        sb.table("ga_tech_overview").insert(payload).execute()
    print(f"ga_tech_overview: wrote {len(payload)} rows")


def sync_ga_traffic_acquisition(ga, sb, start_date, end_date, window_start_iso):
    rows = run_report(
        ga,
        dimensions=["date", "sessionDefaultChannelGroup"],
        metrics=["sessions", "engagedSessions", "engagementRate",
                 "userEngagementDuration", "eventCount", "keyEvents"],
        start_date=start_date,
        end_date=end_date,
    )

    payload = []
    for r in rows:
        sessions = r["sessions"]
        payload.append({
            "channel": r["sessionDefaultChannelGroup"],
            "sessions": int(sessions),
            "engaged_sessions": int(r["engagedSessions"]),
            "engagement_rate": r["engagementRate"],
            "avg_engagement_time": safe_div(r["userEngagementDuration"], sessions),
            "events_per_session": safe_div(r["eventCount"], sessions),
            "event_count": int(r["eventCount"]),
            "key_events": int(r["keyEvents"]),
            "snapshot_date": ga_date_to_iso(r["date"]),
            "recorded_at": dt.datetime.utcnow().isoformat(),
        })

    sb.table("ga_traffic_acquisition").delete().gte("snapshot_date", window_start_iso).execute()
    if payload:
        sb.table("ga_traffic_acquisition").insert(payload).execute()
    print(f"ga_traffic_acquisition: wrote {len(payload)} rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = dt.date.today()
    window_start = today - dt.timedelta(days=LOOKBACK_DAYS)

    start_date = f"{LOOKBACK_DAYS}daysAgo"
    end_date = "yesterday"  # today's GA4 data is incomplete, skip it

    ga = get_ga_client()
    sb = get_supabase()

    sync_ga_metrics(ga, sb, start_date, end_date)
    sync_ga_acquisition(ga, sb, start_date, end_date)
    sync_ga_demographics(ga, sb, start_date, end_date, "country", "ga_demographics_country", "country")
    sync_ga_demographics(ga, sb, start_date, end_date, "city", "ga_demographics_city", "city")
    sync_ga_tech_overview(ga, sb, start_date, end_date, window_start.isoformat())
    sync_ga_traffic_acquisition(ga, sb, start_date, end_date, window_start.isoformat())

    print("GA4 sync complete.")


if __name__ == "__main__":
    main()
