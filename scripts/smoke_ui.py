"""Desktop/mobile UI smoke using installed Edge and synthetic MCP test responses."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8011")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    tool = {
        "name": "clauses/search", "description": "Test query",
        "schema_digest": "sha256:" + "a" * 64,
        "input_schema": {"type": "object", "required": ["count"], "properties": {
            "count": {"type": "integer", "minimum": 1}, "enabled": {"type": "boolean"},
        }}, "output_schema": {"type": "object"},
    }
    server = {
        "server_id": "ui-test", "alias": "UI test", "transport": "streamable-http",
        "url": "https://example.test/mcp", "status": "discovered", "enabled": True,
        "config_digest": "sha256:" + "b" * 64, "tools": [tool],
        "discovered_at": "2026-09-09T00:00:00Z",
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for width, height in ((1440, 1000), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error, *, errors=errors: errors.append(str(error)))
            page.route("**/v1/runs/ui-root", lambda route: route.fulfill(
                json={"status": "running", "run_id": "ui-root", "solution_id": None},
            ))
            page.route("**/v1/human-tasks", lambda route: route.fulfill(json=[
                {"task_id": "child-approval", "run_id": "ui-child", "title": "Approve child write",
                 "subject": {"root_run_id": "ui-root", "arguments": {"value": 1}}},
                {"task_id": "other-approval", "run_id": "other", "title": "Unrelated",
                 "subject": {}},
            ]))
            page.goto(args.url)
            page.evaluate("sessionStorage.setItem('sage.durable-run', 'ui-root')")
            page.reload()
            expect(page.locator("#serviceStatus")).to_contain_text("ready")
            expect(page.locator("#skillCount")).to_contain_text("loaded")
            page.select_option("#modeSelect", "direct")
            page.select_option("#skillSelect", "echo")
            page.locator("#argumentsInput").fill('{"text":"Browser feedback result"}')
            page.locator("#messageInput").fill("Run the selected Skill")
            page.locator("#sendButton").click()
            result_message = page.locator(".message.assistant").last
            feedback = result_message.locator(".feedback-panel")
            expect(feedback).to_be_visible(timeout=30000)
            expect(result_message).to_contain_text("Browser feedback result")
            expect(result_message.locator(".result-metadata")).to_contain_text("skill:echo")
            feedback.get_by_role("button", name="4 out of 5").click()
            feedback.get_by_label("Feedback issue").select_option("incomplete")
            feedback.get_by_label("Feedback comment").fill("Add one more detail.")
            feedback.get_by_role(
                "checkbox", name="Allow regression evaluation"
            ).check()
            with page.expect_response(
                lambda response: "/feedback" in response.url
                and response.request.method == "POST"
            ) as submitted_feedback:
                feedback.get_by_role("button", name="Submit feedback").click()
            assert submitted_feedback.value.status == 201
            expect(feedback.locator(".feedback-status")).to_have_text("Feedback recorded")
            page.select_option("#skillSelect", "contract-review")
            page.locator("#argumentsInput").fill(json.dumps({
                "contract_text": "甲方向乙方交付服务，乙方应付款。<img src=x onerror=alert(1)>",
                "party_role": "seller",
            }))
            page.locator("#messageInput").fill("Review this contract")
            page.locator("#sendButton").click()
            contract_result = page.locator(".message.assistant").last
            expect(contract_result.locator('[data-block-id="issues"] table')).to_be_visible(
                timeout=30000
            )
            expect(contract_result.locator('[data-block-id="citations"]')).to_be_visible()
            expect(contract_result).to_contain_text("not a final legal determination")
            assert contract_result.locator("img").count() == 0
            page.screenshot(path=str(args.output / f"result-feedback-{width}.png"), full_page=True)
            page.locator("#refreshDurable").click()
            expect(page.locator("#durableTasks h3")).to_have_text("Approve child write")
            page.select_option("#modeSelect", "durable-skill")
            expect(page.locator("#argumentsControl")).to_be_visible()
            expect(page.locator("#skillSelect")).to_be_enabled()
            assert page.locator("#messages").bounding_box()["height"] >= 200
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(args.output / f"workbench-{width}.png"), full_page=True)

            records = []
            calls = []

            def route_mcp(route, request, *, calls=calls, records=records):
                path = route.request.url.split("/v1/admin/mcp/servers", 1)[1]
                if path == "":
                    result = [server, {**server, "server_id": "ui-other", "alias": "Other"}]
                elif path.endswith("/tool-risk-reviews"):
                    result = [{"tool_name": tool["name"], "risk": "read-only",
                               "expected_schema_digest": tool["schema_digest"],
                               "expected_server_config_digest": server["config_digest"]}]
                elif path.endswith("/tool-tests") and route.request.method == "POST":
                    request = route.request.post_data_json
                    assert request["arguments"] == {"count": 2, "enabled": False}
                    calls.append(request)
                    result = {"test_id": "test-ui", "tool_name": tool["name"],
                              "created_at": server["discovered_at"], "status": "completed",
                              "result": {"content": [{"type": "text", "text":
                                  '<img src=x onerror="window.injected=true">'}]}}
                    records[:] = [result]
                elif path.endswith("/tool-tests"):
                    result = records
                else:
                    route.abort()
                    return
                route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

            page.route("**/v1/admin/mcp/servers**", route_mcp)
            page.goto(args.url + "/admin")
            page.locator('[data-view="quality"]').click()
            feedback_row = page.locator("#qualityFeedbackList button.resource-item").first
            expect(feedback_row).to_be_visible(timeout=30000)
            feedback_row.click()
            expect(page.locator("#qualityCandidateForm")).to_be_visible()
            page.locator("#qualityRubric").fill("The response must include one more detail.")
            page.locator("#qualityExpectedResult").fill(
                '{"text":"Browser feedback result with one more detail"}'
            )
            page.locator("#qualityTags").fill("browser, regression")
            with page.expect_response(
                lambda response: response.url.endswith("/evaluation-candidates")
                and response.request.method == "POST"
            ) as frozen_sample:
                page.get_by_role("button", name="Freeze evaluation sample").click()
            assert frozen_sample.value.status == 201
            sample = frozen_sample.value.json()
            expect(page.locator(".quality-frozen")).to_contain_text(sample["candidate_id"])
            page.locator(
                f'#qualityCandidateOptions input[value="{sample["candidate_id"]}"]'
            ).check()
            page.locator("#qualitySampleSetVersion").fill(
                f"ui-{width}-{uuid4().hex[:6]}"
            )
            page.locator("#qualitySampleSetTitle").fill(f"Browser quality {width}")
            with page.expect_response(
                lambda response: response.url.endswith("/evaluation-sample-sets")
                and response.request.method == "POST"
            ) as created_set:
                page.get_by_role("button", name="Create sample set").click()
            assert created_set.value.status == 201
            expect(page.locator("#qualityComparisonPanel")).to_be_visible()
            page.locator("#qualityBaselineDigest").fill("sha256:" + "a" * 64)
            page.locator("#qualityCandidateDigest").fill("sha256:" + "b" * 64)
            outcomes = json.loads(page.locator("#qualityOutcomes").input_value())
            assert len(outcomes) == 1
            outcomes[0].update({
                "baseline_score": 0.2,
                "candidate_score": 0.8,
                "candidate_result_digest": "sha256:" + "c" * 64,
            })
            page.locator("#qualityOutcomes").fill(json.dumps(outcomes))
            with page.expect_response(
                lambda response: response.url.endswith("/comparisons")
                and response.request.method == "POST"
            ) as imported_comparison:
                page.get_by_role("button", name="Import comparison").click()
            assert imported_comparison.value.status == 201
            expect(page.locator("#qualityComparisonList")).to_contain_text("improved")
            page.screenshot(path=str(args.output / f"quality-{width}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator('[data-view="mcp"]').click()
            page.locator("#mcpList button").first.click()
            page.get_by_role("button", name="Test tool", exact=True).click()
            expect(page.locator("#mcpTestReview")).to_have_text("Reviewed")
            page.locator('[data-name="count"]').fill("2")
            page.locator('[data-name="enabled"]').select_option("false")
            page.locator("#runMcpTest").click()
            expect(page.locator("#mcpTestResult")).to_contain_text("completed")
            page.locator("#refreshMcpTests").click()
            expect(page.locator("#mcpTestHistory")).to_contain_text("completed")
            assert len(calls) == 1
            assert not page.evaluate("Boolean(window.injected)")
            assert page.locator("#mcpTestResult img").count() == 0
            page.screenshot(path=str(args.output / f"admin-{width}.png"), full_page=True)
            overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
                .filter(item => item.getBoundingClientRect().right > innerWidth + 1)
                .slice(0, 10).map(item => ({tag: item.tagName, id: item.id,
                    className: item.className, right: item.getBoundingClientRect().right}))""")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), overflow
            assert not errors, errors
            page.locator("#mcpList button").nth(1).click()
            expect(page.locator("#mcpToolTest")).to_be_hidden()
            page.close()
        browser.close()
    print("Desktop/mobile checks passed; synthetic MCP invoked once per viewport.")


if __name__ == "__main__":
    main()