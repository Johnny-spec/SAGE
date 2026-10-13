"""Check workflow MCP details with synthetic metadata, without publishing or invoking tools."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8014")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    tool = {
        "kind": "tool", "target": "tool:mcp:knowledge:search", "name": "search",
        "server_id": "knowledge", "server_alias": "Knowledge MCP", "version": "1.2.0",
        "description": '<img src=x onerror="window.injected=true"> Search approved articles.',
        "enabled": True, "risk": "read-only", "tool_timeout_seconds": 45,
        "max_output_bytes": 8192, "content_digest": "sha256:" + "a" * 64,
        "schema_digest": "sha256:" + "b" * 64,
        "input_schema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "Search phrase"},
            "limit": {"type": "integer", "default": 5, "minimum": 1},
        }},
        "tool_output_schema": {"type": "object", "properties": {"matches": {"type": "array"}}},
    }
    other = {**tool, "target": "tool:mcp:archive:search", "server_id": "archive",
             "server_alias": "Archive MCP", "description": "Search archived records.",
             "tool_output_schema": None}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for width, height in ((1440, 1000), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error, *, errors=errors: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.route("**/v1/admin/solutions", lambda route: route.fulfill(json=[
                {"solution_id": "preview", "title": "Preview", "supported_profiles": []},
            ]))
            page.route("**/v1/admin/workflow-drafts?*", lambda route: route.fulfill(
                json={"items": [], "total": 0},
            ))
            page.route("**/v1/admin/workflow-targets", lambda route: route.fulfill(json={
                "generation_id": "test", "targets": [tool, other],
            }))
            page.goto(args.url + "/admin")
            page.locator('[data-view="workflows"]').click()
            page.locator("#wfNew").click()
            page.locator("#wfKind").select_option("tool")
            page.locator("#wfAdd").click()
            details = page.get_by_role("region", name="MCP tool details")
            expect(details).to_contain_text("Knowledge MCP")
            expect(details).to_contain_text("Search phrase")
            expect(details).to_contain_text("string · Required")
            expect(details).to_contain_text("integer · Optional")
            expect(details).to_contain_text("45 s")
            expect(details).to_contain_text("8192 bytes")
            expect(page.locator(".wf-node strong")).to_have_text("search")
            assert not details.locator("img").count()
            assert not page.evaluate("Boolean(window.injected)")
            input_json = page.locator("#wfProperties").get_by_label("Input JSON", exact=True)
            expect(input_json).to_have_value("{}")
            details.locator("summary", has_text="Published digests").click()
            expect(details).to_contain_text(tool["schema_digest"])
            details.locator("summary", has_text="Tool output schema").click()
            expect(details.locator("pre").last).to_contain_text("matches")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert details.evaluate("element => element.scrollWidth <= element.clientWidth")
            page.evaluate("document.querySelector('.wf-inspector').scrollTop = 0; scrollTo(0, 0)")
            page.screenshot(path=str(args.output / f"tool-details-{width}.png"), full_page=True)
            target_select = page.locator("#wfProperties").get_by_label("Target", exact=True)
            target_select.select_option(other["target"])
            expect(details).to_contain_text("Archive MCP")
            expect(details).not_to_contain_text("Knowledge MCP")
            details.locator("summary", has_text="Tool output schema").click()
            expect(details).to_contain_text("Not declared")
            page.route("**/v1/admin/workflow-targets", lambda route: route.fulfill(json={
                "generation_id": "empty", "targets": [],
            }))
            page.locator("#wfNew").click()
            page.locator("#wfAdd").click()
            expect(page.locator(".wf-tool-unavailable")).to_have_text(
                "No published MCP tools in the active Generation.",
            )
            assert not errors, errors
            page.close()
        browser.close()
    print("PASS: desktop/mobile MCP details, parameters, switching, empty state and escaping.")


if __name__ == "__main__":
    main()