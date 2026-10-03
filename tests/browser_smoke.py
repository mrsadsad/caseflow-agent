"""Optional visual QA: requires Playwright + Edge; not a runtime dependency.

Run from the project root: python tests/browser_smoke.py
"""

from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from caseflow.runtime import Runtime
from caseflow.server import make_server
from caseflow.store import Store


def main():
    from playwright.sync_api import sync_playwright, expect

    project = Path(__file__).resolve().parents[1]
    (project / "runs").mkdir(exist_ok=True)
    (project / "docs").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / "visual.sqlite")
        store.seed()
        server = make_server(store, lambda: Runtime(store), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1480, "height": 1080}, device_scale_factor=1)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(server.origin, wait_until="networkidle")
                expect(page.locator(".case-card")).to_have_count(9)
                page.locator("#start").click()
                expect(page.locator("#run-status")).to_have_text("等待人工审批", timeout=10000)
                expect(page.locator(".approval-amount")).to_have_text("¥129.00")
                expect(page.locator("#evidence")).to_contain_text("refund-standard · v2")
                page.locator("#toast").evaluate("element => element.hidden = true")
                page.screenshot(path=str(project / "docs" / "workbench.png"), full_page=True)
                page.get_by_role("button", name="批准并入队").click()
                expect(page.locator("#run-status")).to_have_text("已批准 · 等待投递")
                page.locator('[data-view="delivery"]').click()
                page.locator("#dispatch-crash").click()
                expect(page.locator("#stat-ledger")).to_have_text("1")
                expect(page.locator("#outbox")).to_contain_text("待投递")
                page.locator("#dispatch").click()
                expect(page.locator("#outbox")).to_contain_text("已投递")
                expect(page.locator("#stat-ledger")).to_have_text("1")
                page.locator("#toast").evaluate("element => element.hidden = true")
                page.screenshot(path=str(project / "runs" / "ui-delivery.png"), full_page=True)
                # Untrusted strings must render literally, including HTML payloads.
                page.evaluate("""() => {
                    state.cases[0].request = '<img src=x onerror=window.caseflowXss=1>';
                    state.selectedCase = state.cases[0].id;
                    setView('workbench'); render();
                }""")
                expect(page.locator("#request-text")).to_have_text("<img src=x onerror=window.caseflowXss=1>")
                assert page.evaluate("window.caseflowXss") is None
                assert page.locator("#request-text img").count() == 0
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('[data-view="workbench"]').click()
                page.locator("#refresh").click()
                expect(page.locator("#request-text")).not_to_contain_text("<img")
                page.locator("#toast").evaluate("element => element.hidden = true")
                page.screenshot(path=str(project / "runs" / "ui-mobile.png"), full_page=True)
                overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert not overflow, "Mobile page overflows horizontally"
                page.locator('[data-view="delivery"]').click()
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.locator('[data-view="design"]').click()
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                assert errors == [], errors
                assert len(store.ledger()) == 1
                browser.close()
                print("Browser QA passed: create, evidence, approve, injected crash, retry, XSS text, mobile overflow, no JS errors.")
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            store.close()


if __name__ == "__main__":
    main()
