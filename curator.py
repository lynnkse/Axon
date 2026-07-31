#!/usr/bin/env python3
"""
CuratorNode — Axon skill/knowledge maintenance process.

Runs continuously. Once per CURATOR_INTERVAL_HOURS it:
  1. Finds stale agent-created rules (low use, old age)
  2. Finds stale insights (low use, old age)
  3. Archives zero-use entries past the hard cutoff
  4. Logs candidates that need LLM review (future: auto-review via Haiku)

Stale thresholds:
  - Rules:   created_by='agent', use_count=0, age > RULE_ARCHIVE_DAYS  → archive
  - Rules:   created_by='agent', use_count < 2, age > RULE_REVIEW_DAYS → log for review
  - Insights: use_count=0, age > INSIGHT_ARCHIVE_DAYS                  → archive
  - Insights: use_count < 2, age > INSIGHT_REVIEW_DAYS                 → log for review
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [curator] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CURATOR_INTERVAL_HOURS = 24
RULE_ARCHIVE_DAYS      = 60   # agent skill unused for 60d → archive
RULE_REVIEW_DAYS       = 14   # agent skill low-use for 14d → flag
INSIGHT_ARCHIVE_DAYS   = 90   # insight never used for 90d → archive
INSIGHT_REVIEW_DAYS    = 30   # insight low-use for 30d → flag


def _headers() -> dict:
    return {
        "apikey": config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }


def _get(url: str) -> list:
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"GET failed: {url} — {e}")
        return []


def _patch(table: str, row_id: str, payload: dict):
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}?id=eq.{row_id}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="PATCH", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log.warning(f"PATCH {table} id={row_id} failed: {e}")


def _age_days(ts_str: str) -> float:
    """Return age in days from ISO timestamp string to now."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return 0.0


def curate_rules():
    """Review agent-created rules for staleness."""
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rules"
        f"?created_by=eq.agent&active=eq.true"
        f"&select=id,name,short_description,use_count,created_at,last_used"
    )
    rules = _get(url)
    archived = reviewed = 0

    for r in rules:
        age = _age_days(r.get("created_at", ""))
        use = r.get("use_count", 0) or 0
        name = r.get("name", r["id"])

        if use == 0 and age > RULE_ARCHIVE_DAYS:
            log.info(f"Archiving stale rule '{name}' (age={age:.0f}d, use={use})")
            _patch("rules", r["id"], {"active": False, "curator_note": f"Auto-archived: 0 uses in {age:.0f} days"})
            archived += 1
        elif use < 2 and age > RULE_REVIEW_DAYS:
            log.info(f"Review candidate rule '{name}' (age={age:.0f}d, use={use}) — low engagement")
            reviewed += 1

    log.info(f"Rules: archived={archived}, flagged_for_review={reviewed}, total_agent_skills={len(rules)}")


def curate_insights():
    """Review insights for staleness."""
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/insights"
        f"?select=id,content,use_count,created_at,last_used&limit=200"
    )
    insights = _get(url)
    archived = reviewed = 0

    for ins in insights:
        age = _age_days(ins.get("created_at", ""))
        use = ins.get("use_count", 0) or 0
        snippet = (ins.get("content", "")[:60]).replace("\n", " ")

        if use == 0 and age > INSIGHT_ARCHIVE_DAYS:
            log.info(f"Archiving stale insight (age={age:.0f}d): {snippet!r}")
            _patch("insights", ins["id"], {"archived": True, "curator_note": f"Auto-archived: 0 uses in {age:.0f} days"})
            archived += 1
        elif use < 2 and age > INSIGHT_REVIEW_DAYS:
            log.info(f"Review candidate insight (age={age:.0f}d, use={use}): {snippet!r}")
            reviewed += 1

    log.info(f"Insights: archived={archived}, flagged_for_review={reviewed}, total={len(insights)}")


def run_once():
    log.info("=== Curator run starting ===")
    try:
        curate_rules()
    except Exception as e:
        log.error(f"curate_rules failed: {e}")
    try:
        curate_insights()
    except Exception as e:
        log.error(f"curate_insights failed: {e}")
    log.info("=== Curator run complete ===")


def main():
    log.info(f"CuratorNode started — interval={CURATOR_INTERVAL_HOURS}h")
    while True:
        run_once()
        next_run = CURATOR_INTERVAL_HOURS * 3600
        log.info(f"Sleeping {CURATOR_INTERVAL_HOURS}h until next run")
        time.sleep(next_run)


if __name__ == "__main__":
    main()
