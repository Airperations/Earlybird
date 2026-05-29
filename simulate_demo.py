"""
Earlybird — Demo Simulation Script
Simulates a real incident scenario for judges.

Usage: python simulate_demo.py

Scenario:
  - Withdrawal endpoint starts returning HTTP 502 for LATAM users
  - Agent detects it immediately via Sentry webhook
  - 3 minutes later, a "Freshdesk ticket" arrives
  - Dashboard shows: Agent Won with +180s lead time
"""

import os
import sys
import argparse
import requests
import time
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def guard_against_production(api_base: str, force: bool) -> None:
    """
    This script injects FAKE incidents and a FAKE Freshdesk ticket. Running it
    against a live deployment pollutes the 30-day win-rate metric. Refuse to run
    against a non-local host unless explicitly forced AND interactively confirmed.
    """
    host = (urlparse(api_base).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return
    log(f"⚠️  Target '{api_base}' is NOT localhost — this injects fake data into a real system.")
    if not force:
        log("   Refusing to run. Re-run with --force if you really mean it.")
        sys.exit(1)
    if sys.stdin.isatty():
        answer = input("   Type CONFIRM to proceed against this non-local target: ").strip()
        if answer != "CONFIRM":
            log("   Aborted.")
            sys.exit(1)
    else:
        log("   Non-interactive shell with --force — refusing to guess. Aborting.")
        sys.exit(1)


def simulate_sentry_webhook():
    """Simulate a Sentry error on /withdraw."""
    payload = {
        "project_slug": "payments-api",
        "event": {
            "title": "GatewayTimeout: Payment provider did not respond",
            "message": "GatewayTimeout: Payment provider did not respond",
            "platform": "python",
            "release": "v1.42.0",
            "tags": [
                ["environment", "production"],
                ["country_code", "MX"],
                ["http.status_code", "502"],
            ],
            "request": {
                "url": "https://api.airdrive.com/api/v1/withdraw/confirm",
                "method": "POST",
            },
            "exception": {
                "values": [{
                    "type": "GatewayTimeout",
                    "value": "Payment provider did not respond within 30s",
                    "stacktrace": {
                        "frames": [
                            {"function": "confirm_withdrawal", "filename": "payments/views.py", "lineno": 142},
                            {"function": "call_provider", "filename": "payments/gateway.py", "lineno": 87},
                        ]
                    }
                }]
            },
            "user": {"id": "user_demo_mx_001"},
            "contexts": {"response": {"status_code": 502}},
        }
    }
    return payload


def simulate_freshdesk_ticket():
    """Simulate a Freshdesk ticket arriving after the incident."""
    ticket = {
        "id": 99901,
        "subject": "No puedo hacer retiro - error 502",
        "description_text": "Hola, estoy intentando hacer un retiro y me sale error. Estoy en México.",
        "requester": {"email": "user_demo@gmail.com"},
        "tags": [{"name": "MX"}, {"name": "withdrawal"}, {"name": "error"}],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": {},
    }
    return ticket


def main():
    global API_BASE
    parser = argparse.ArgumentParser(description="Earlybird end-to-end demo simulation")
    parser.add_argument("--api-base", default=API_BASE, help="API base URL (default: %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="Allow running against a non-local API (requires interactive CONFIRM)")
    args = parser.parse_args()

    API_BASE = args.api_base
    guard_against_production(API_BASE, args.force)

    print("\n" + "="*60)
    print("  EARLYBIRD — DEMO SIMULATION")
    print("="*60 + "\n")

    # ── Phase 1: Check API is running ────────────────────────────────────────
    log("Checking API health...")
    try:
        r = requests.get(f"{API_BASE}/health", timeout=5)
        r.raise_for_status()
        log(f"✅ API is running: {r.json()}")
    except Exception as e:
        log(f"❌ API not reachable: {e}")
        log("   Run: uvicorn app.main:app --reload")
        return

    print()

    # ── Phase 2: Send Sentry webhook ──────────────────────────────────────────
    log("📡 Sending simulated Sentry webhook (withdrawal error in Mexico)...")
    sentry_payload = simulate_sentry_webhook()
    try:
        r = requests.post(f"{API_BASE}/webhooks/sentry/", json=sentry_payload, timeout=10)
        r.raise_for_status()
        resp = r.json()
        agent_timestamp = resp.get("received_at")
        log(f"✅ Webhook accepted. Agent timestamp: {agent_timestamp}")
    except Exception as e:
        log(f"❌ Webhook failed: {e}")
        return

    print()

    # ── Phase 3: Wait for processing ─────────────────────────────────────────
    log("⏳ Waiting 5 seconds for async processing pipeline...")
    time.sleep(5)

    # ── Phase 4: Check incident was created ──────────────────────────────────
    log("📋 Checking incident was detected...")
    try:
        r = requests.get(f"{API_BASE}/dashboard/incidents", timeout=5)
        incidents = r.json().get("incidents", [])
        if incidents:
            inc = incidents[0]
            log(f"✅ Incident detected: {inc.get('title', 'Unknown')}")
            log(f"   Severity: {inc.get('severity')} | Score: {inc.get('score')}")
            log(f"   Agent Alert: {inc.get('agent_alert_timestamp')}")
        else:
            log("⚠️  No incidents yet. Worker may still be processing.")
    except Exception as e:
        log(f"❌ Error: {e}")

    print()

    # ── Phase 5: Simulate Freshdesk ticket (3 min later) ─────────────────────
    log("⏳ Simulating 3-minute delay (Freshdesk ticket arrives late)...")
    log("   In a real scenario, this is the user opening a support ticket.")
    time.sleep(3)  # Shortened for demo; represents ~3 minutes in real trial

    log("🎫 Injecting simulated Freshdesk ticket...")
    ticket = simulate_freshdesk_ticket()
    try:
        r = requests.post(f"{API_BASE}/freshdesk/webhook", json=ticket, timeout=5)
        r.raise_for_status()
        log(f"✅ Freshdesk ticket injected: ID {ticket['id']}")
    except Exception as e:
        log(f"⚠️  Freshdesk webhook: {e}")

    log("⏳ Waiting for matcher to run...")
    time.sleep(5)

    print()

    # ── Phase 6: Show results ─────────────────────────────────────────────────
    log("🏆 FINAL RESULTS:")
    try:
        r = requests.get(f"{API_BASE}/dashboard/summary", timeout=5)
        summary = r.json()
        bounty = summary.get("bounty_metric", {})
        race = summary.get("race_results", {})
        lead = summary.get("lead_time", {})

        print(f"\n  Win Rate:     {bounty.get('win_rate_percent')}% (target ≥80%)")
        print(f"  Status:       {bounty.get('status')}")
        print(f"  Agent Won:    {race.get('agent_won')}")
        print(f"  Agent Lost:   {race.get('agent_lost')}")
        print(f"  Avg Lead Time: {lead.get('average_human')}")
        print()
        print(f"  🖥️  Full Dashboard: http://localhost:8501")
        print(f"  📊  API Docs:      http://localhost:8000/docs")
        print()
    except Exception as e:
        log(f"❌ Error fetching results: {e}")

    print("="*60)
    print("  Demo complete. Check Slack for the alert!")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
