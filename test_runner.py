"""
ChannelView - Unified Test Runner
Runs comprehensive diagnostic tests and outputs structured results.
Used by CI/CD pipeline and local development.

Usage:
    python test_runner.py              # Run all tests
    python test_runner.py --section auth   # Run specific section
    python test_runner.py --verbose    # Show full details
"""
import os, sys, json, time, subprocess, requests, sqlite3, signal, argparse

PORT = int(os.environ.get('TEST_PORT', 5199))
BASE = f"http://localhost:{PORT}"
RESULTS = []
FAILURES = []

def test(name, section, passed, detail=""):
    RESULTS.append({"name": name, "section": section, "passed": bool(passed), "detail": detail})
    icon = "\u2705" if passed else "\u274c"
    print(f"  {icon} {name}: {detail}")
    if not passed:
        FAILURES.append(f"[{section}] {name}: {detail}")

def api(method, path, token=None, csrf=None, api_key=None, **kwargs):
    url = BASE + path
    headers = kwargs.pop("headers", {})
    if token:
        headers["Cookie"] = f"token={token}"
        if csrf:
            headers["Cookie"] += f"; csrf_token={csrf}"
            headers["X-CSRF-Token"] = csrf
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        return requests.request(method, url, headers=headers, timeout=10, **kwargs)
    except Exception as e:
        class FakeResp:
            status_code = 0
            text = str(e)
            def json(self): return {"error": self.text}
            headers = {}
        return FakeResp()

def setup_test_data(token, csrf):
    """Create standard test data: interview, questions, candidate."""
    r = api("POST", "/api/interviews", token=token, csrf=csrf, json={
        "title": "CI Test Interview", "description": "Automated test", "department": "Sales"
    })
    iid = r.json().get("id", "")
    api("POST", f"/api/interviews/{iid}/questions", token=token, csrf=csrf, json={
        "text": "Why do you want this role?", "question_order": 1
    })
    api("POST", f"/api/interviews/{iid}/questions", token=token, csrf=csrf, json={
        "text": "Describe a challenge you overcame", "question_order": 2
    })
    r = api("POST", "/api/candidates", token=token, csrf=csrf, json={
        "interview_id": iid, "first_name": "Test", "last_name": "Candidate",
        "email": "candidate@test.com", "send_invite": False
    })
    cid = r.json().get("id", "")
    ctoken = r.json().get("token", "")
    requests.post(f"{BASE}/api/interview/{ctoken}/start", timeout=10)
    return iid, cid, ctoken

def run_tests():
    parser = argparse.ArgumentParser(description='ChannelView Test Runner')
    parser.add_argument('--section', type=str, help='Run only this section')
    parser.add_argument('--verbose', action='store_true', help='Verbose output')
    args = parser.parse_args()

    # Start server
    print(f"Starting ChannelView server on port {PORT}...")
    env = {**os.environ, "PORT": str(PORT), "FLASK_ENV": "development",
           "STRIPE_SECRET_KEY": "", "STRIPE_PRICE_ID": "", "STRIPE_WEBHOOK_SECRET": ""}
    proc = subprocess.Popen([sys.executable, "app.py"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)

    try:
        # Auth setup
        s = requests.Session()
        r = s.post(f"{BASE}/api/auth/register", json={
            "email": "ci@test.com", "password": "CITest1234!",
            "name": "CI Owner", "agency_name": "CI Insurance Agency"
        })
        token = r.cookies.get("token", "")
        csrf = s.cookies.get("csrf_token") or r.cookies.get("csrf_token", "")
        iid, cid, ctoken = setup_test_data(token, csrf)

        # === Section: Auth ===
        S = "Auth"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/auth/me", token=token)
        test("Auth me endpoint", S, r.status_code == 200, f"Name: {r.json().get('name')}")

        r = api("POST", "/api/auth/login", json={"email": "ci@test.com", "password": "CITest1234!"})
        test("Login", S, r.status_code == 200, f"Has token: {bool(r.cookies.get('token'))}")

        r = api("POST", "/api/auth/login", json={"email": "ci@test.com", "password": "wrong"})
        test("Bad password rejected", S, r.status_code == 401, f"Status {r.status_code}")

        r = api("GET", "/api/dashboard")
        test("Unauthed dashboard rejected", S, r.status_code in (401, 302), f"Status {r.status_code}")

        # === Section: CRUD ===
        S = "CRUD"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/interviews", token=token)
        test("List interviews", S, r.status_code == 200 and len(r.json()) > 0, f"Count: {len(r.json())}")

        r = api("GET", f"/api/interviews/{iid}", token=token)
        test("Get interview detail", S, r.status_code == 200, f"Title: {r.json().get('title')}")

        r = api("GET", "/api/candidates", token=token)
        test("List candidates", S, r.status_code == 200, f"Status {r.status_code}")

        r = api("GET", f"/api/candidates/{cid}", token=token)
        test("Get candidate detail", S, r.status_code == 200, f"Name: {r.json().get('first_name')}")

        # === Section: Candidate Flow ===
        S = "Candidate Flow"
        print(f"\n\U0001f4cb {S}")
        r = requests.get(f"{BASE}/api/interview/{ctoken}/mobile-config", timeout=10)
        test("Mobile config", S, r.status_code == 200, f"Questions: {len(r.json().get('questions', []))}")

        r = requests.get(f"{BASE}/api/interview/{ctoken}/load-progress", timeout=10)
        test("Load progress", S, r.status_code == 200, f"Can resume: {r.json().get('can_resume')}")

        r = requests.post(f"{BASE}/api/interview/{ctoken}/connection-status",
                          json={"bandwidth_kbps": 1000}, timeout=10)
        test("Connection status", S, r.status_code == 200, f"Res: {r.json().get('recommended_resolution')}")

        # === Section: Analytics ===
        S = "Analytics"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/analytics", token=token)
        test("Analytics endpoint", S, r.status_code == 200, f"Status {r.status_code}")

        r = api("GET", "/api/analytics/funnel", token=token)
        test("Funnel analytics", S, r.status_code == 200, f"Stages: {len(r.json().get('stages', []))}")

        r = api("GET", "/api/dashboard", token=token)
        test("Dashboard", S, r.status_code == 200, f"Status {r.status_code}")

        # === Section: Integrations ===
        S = "Integrations"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/integrations/events/types", token=token)
        test("Event types", S, r.status_code == 200, f"Types: {len(r.json().get('event_types', []))}")

        r = api("GET", "/api/integrations/zapier", token=token)
        test("Zapier config", S, r.status_code == 200, f"Status {r.status_code}")

        r = api("GET", "/api/integrations", token=token)
        test("List integrations", S, r.status_code == 200, f"Status {r.status_code}")

        # === Section: Bulk Ops ===
        S = "Bulk Ops"
        print(f"\n\U0001f4cb {S}")
        r = api("POST", "/api/candidates/bulk-invite", token=token, csrf=csrf, json={
            "interview_id": iid,
            "candidates": [
                {"first_name": "Bulk1", "last_name": "Test", "email": "bulk1@test.com"},
                {"first_name": "Bulk2", "last_name": "Test", "email": "bulk2@test.com"},
            ]
        })
        test("Bulk invite", S, r.status_code == 200 and r.json().get("created") == 2, f"Created: {r.json().get('created')}")

        r = api("GET", "/api/candidates/pipeline", token=token)
        test("Pipeline view", S, r.status_code == 200, f"Stages: {len(r.json().get('stages', []))}")

        r = api("PUT", f"/api/candidates/{cid}/pipeline-stage", token=token, csrf=csrf, json={
            "stage": "in_review"
        })
        test("Pipeline stage move", S, r.status_code == 200, f"Stage: {r.json().get('stage')}")

        # === Section: Compliance ===
        S = "Compliance"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/compliance/retention", token=token)
        test("Retention settings", S, r.status_code == 200, f"Days: {r.json().get('default_retention_days')}")

        r = api("GET", "/api/compliance/log", token=token)
        test("Audit log", S, r.status_code == 200, f"Entries: {len(r.json().get('entries', []))}")

        r = api("GET", "/api/compliance/retention/check", token=token)
        test("Retention check", S, r.status_code == 200, f"Status {r.status_code}")

        # === Section: Automation ===
        S = "Automation"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/automation/settings", token=token)
        test("Automation settings", S, r.status_code == 200, f"Auto-score: {r.json().get('auto_score_enabled')}")

        # === Section: Billing ===
        S = "Billing"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/billing/status", token=token)
        test("Billing status", S, r.status_code == 200, f"Plan: {r.json().get('plan')}")

        # === Section: Health ===
        S = "Health"
        print(f"\n\U0001f4cb {S}")
        r = requests.get(f"{BASE}/health", timeout=5)
        test("Health endpoint", S, r.json().get("status") == "healthy", f"Status: {r.json().get('status')}")

        r = requests.get(f"{BASE}/health/ready", timeout=5)
        test("Readiness probe", S, r.json().get("ready") == True, f"Ready: {r.json().get('ready')}")

        # === Section: Tenant Isolation ===
        S = "Tenant Isolation"
        print(f"\n\U0001f4cb {S}")

        # Create second user
        r2 = requests.post(f"{BASE}/api/auth/register", json={
            "email": "tenant2@test.com", "password": "Tenant2Pass!",
            "name": "Tenant Two", "agency_name": "Other Agency"
        })
        token2 = r2.cookies.get("token", "")
        csrf2 = r2.cookies.get("csrf_token", "")

        # Tenant 2 should NOT see tenant 1's interviews
        r = api("GET", "/api/interviews", token=token2)
        test("Tenant isolation: interviews", S, len(r.json()) == 0, f"Visible: {len(r.json())}")

        # Tenant 2 should NOT see tenant 1's candidates
        r = api("GET", "/api/candidates", token=token2)
        cands = r.json().get("candidates", []) if isinstance(r.json(), dict) else r.json()
        test("Tenant isolation: candidates", S, len(cands) == 0, f"Visible: {len(cands)}")

        # Tenant 2 should NOT access tenant 1's candidate
        r = api("GET", f"/api/candidates/{cid}", token=token2)
        test("Tenant isolation: candidate detail", S, r.status_code == 404, f"Status {r.status_code}")

        # Tenant 2 should NOT see tenant 1's pipeline
        r = api("GET", "/api/candidates/pipeline", token=token2)
        total = sum(len(v) for v in r.json().get("pipeline", {}).values())
        test("Tenant isolation: pipeline", S, total == 0, f"Total candidates: {total}")

        # Tenant 2 should NOT see tenant 1's compliance log
        r = api("GET", "/api/compliance/log", token=token2)
        test("Tenant isolation: compliance", S, len(r.json().get("entries", [])) == 0,
             f"Entries: {len(r.json().get('entries', []))}")

        # Tenant 2 should NOT see tenant 1's integrations
        r = api("GET", "/api/integrations/events", token=token2)
        test("Tenant isolation: events", S, len(r.json().get("events", [])) == 0,
             f"Events: {len(r.json().get('events', []))}")

        # === Section: Email System ===
        S = "Email"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/config/email-backend", token=token)
        test("Email backend config", S, r.status_code == 200, f"Backend: {r.json().get('backend')}")

        r = api("GET", "/api/notifications/preferences", token=token)
        test("Notification preferences", S, r.status_code == 200, f"Status {r.status_code}")

        r = api("PUT", "/api/notifications/preferences", token=token, csrf=csrf, json={
            "notify_interview_started": True, "notify_interview_completed": True,
            "notify_candidate_invited": False, "notify_daily_digest": True
        })
        test("Update notification prefs", S, r.status_code == 200, f"Status {r.status_code}")

        # Verify saved
        r = api("GET", "/api/notifications/preferences", token=token)
        test("Prefs saved correctly", S,
             r.json().get("notify_daily_digest") == True and r.json().get("notify_candidate_invited") == False,
             f"Digest: {r.json().get('notify_daily_digest')}, Invited: {r.json().get('notify_candidate_invited')}")

        # === Section: Performance ===
        S = "Performance"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/system/db-stats", token=token)
        test("DB stats endpoint", S, r.status_code == 200, f"Status {r.status_code}")

        r = api("GET", "/api/system/performance", token=token)
        test("Performance metrics", S, r.status_code == 200, f"Status {r.status_code}")

        # === Section: Billing & Plans ===
        S = "Billing"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/billing/status", token=token)
        test("Billing status", S, r.status_code == 200, f"Plan: {r.json().get('plan')}")

        r = api("GET", "/api/billing/plans", token=token)
        test("Plan list", S, r.status_code == 200, f"Status {r.status_code}")

        r = api("GET", "/api/billing/usage", token=token)
        test("Usage stats", S, r.status_code == 200, f"Trial: {r.json().get('is_trial')}")

        r = api("POST", "/api/billing/check-feature", token=token, csrf=csrf, json={"feature": "ai_scoring"})
        test("Feature check", S, r.status_code in (200, 403), f"Status {r.status_code}")

        r = api("GET", "/api/billing/invoices", token=token)
        test("Invoice history", S, r.status_code == 200, f"Count: {len(r.json().get('invoices', []))}")

        # === Section: Onboarding ===
        S = "Onboarding"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/onboarding/status", token=token)
        test("Onboarding status", S, r.status_code == 200, f"Progress: {r.json().get('progress_pct', 0)}%")
        test("Onboarding has data", S, r.status_code == 200 and isinstance(r.json(), dict), f"Keys: {list(r.json().keys())[:4]}")

        r = api("POST", "/api/onboarding/complete", token=token, csrf=csrf)
        test("Complete onboarding", S, r.status_code == 200, f"Status {r.status_code}")

        # === Section: Monitoring ===
        S = "Monitoring"
        print(f"\n\U0001f4cb {S}")
        r = api("GET", "/api/system/monitoring", token=token)
        test("System monitoring", S, r.status_code in (200, 403), f"Status {r.status_code}")

        r = api("GET", "/api/health")
        test("Health check", S, r.status_code == 200, f"Status: {r.json().get('status')}")

        # === Section: Pages ===
        S = "Pages"
        print(f"\n\U0001f4cb {S}")
        for page in ["dashboard", "candidates", "interviews", "ai", "analytics", "settings",
                      "billing", "automation", "kanban", "integrations", "compliance", "system"]:
            r = requests.get(f"{BASE}/{page}", timeout=5)
            test(f"/{page}", S, r.status_code == 200, f"Status {r.status_code}")

    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except: proc.kill()
        print("\nServer stopped.")

    # Summary
    passed = sum(1 for r in RESULTS if r["passed"])
    total = len(RESULTS)
    pct = round(100 * passed / total, 1) if total else 0

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed}/{total} passed ({pct}%)")
    print(f"{'='*60}")

    if FAILURES:
        print(f"\n  FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"    \u274c {f}")

    # Save results
    with open("test_results.json", "w") as f:
        json.dump({
            "passed": passed, "total": total, "pct": pct,
            "failures": FAILURES, "results": RESULTS
        }, f, indent=2)

    # Exit with non-zero if failures (for CI)
    sys.exit(0 if passed == total else 1)

if __name__ == "__main__":
    run_tests()
