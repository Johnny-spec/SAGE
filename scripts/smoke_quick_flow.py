"""UI-only development publication and real Temporal execution acceptance."""

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from scripts.demo_publication import workflow_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8014")
    parser.add_argument("--temporal", default="127.0.0.1:7233")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"errors": [], "runs": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=args.headless)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        try:
            page.goto(args.url + "/admin")
            page.wait_for_function("() => typeof state !== 'undefined' && state.status !== null")
            assert page.evaluate("environment") == "development"
            page.locator('[data-view="agents"]').click()
            agent_id = "a" + uuid4().hex[:4]
            page.locator("#newAgent").click()
            page.locator("#agentId").fill(agent_id)
            page.locator("#agentName").fill(agent_id)
            page.locator("#agentInstructions").fill("Use echo to repeat the message.")
            page.locator('#agentCapabilities input[value="skill:echo"]').check()
            page.locator("#agentMemory").select_option("none")
            page.locator("#agentForm").get_by_role("button", name="Save draft", exact=True).click()
            agent_row = page.locator("#agentList button.resource-item").filter(has_text=agent_id)
            publish_agent = agent_row.locator("+ button")
            expect(publish_agent).to_have_text("Quick publish")
            publish_agent.click()
            expect(agent_row.locator(".status-badge")).to_have_text("active", timeout=60000)
            report["agent_published"] = agent_id
            page.locator('[data-view="mcp"]').click()
            page.locator("#newMcp").click()
            alias = "m" + uuid4().hex[:4]
            page.locator("#mcpAlias").fill(alias)
            page.locator("#mcpUrl").fill("http://127.0.0.1:8766/mcp")
            page.locator("#mcpForm").get_by_role("button", name="Save configuration").click()
            expect(page.locator("#mcpServerId")).not_to_have_value("")
            page.locator("#testMcp").click()
            page.wait_for_function("() => state.selectedMcp?.status === 'reachable'")
            page.locator("#discoverMcp").click()
            tool = page.locator("#mcpTools button.resource-item").filter(has_text="calculate_total")
            expect(tool).to_be_visible(timeout=30000)
            tool.click()
            page.locator("#registerMcpTool").click()
            expect(page.locator("#mcpTools .status-badge")).to_have_text("active", timeout=60000)
            page.wait_for_function("() => !quickPublishing")
            tool.click()
            skill_id = "k" + uuid4().hex[:4]
            page.locator("#mcpSkillName").fill(skill_id)
            page.locator("#mcpImportForm").get_by_role("button", name="Import as Skill").click()
            page.wait_for_function(
                "name => state.skills.some(item => item.name === name && "
                "publicationStatus(item, 'skill') === 'active')",
                arg=skill_id, timeout=60000,
            )
            page.wait_for_function("() => !quickPublishing")
            report["mcp_native_and_skill_published"] = True
            page.screenshot(path=str(args.output / "mcp-published.png"), full_page=True)
            for width, height in ((1440, 1000), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                solution_id = "s" + uuid4().hex[:4]
                page.locator('[data-view="solutions"]').click()
                page.wait_for_function("() => !window.solutionWorkbench.inert")
                page.locator("#newSolution").click()
                expect(page.locator("#solutionForm")).to_be_visible()
                page.locator("#solutionId").fill(solution_id)
                page.locator("#solutionTitle").fill(solution_id)
                page.locator("#solutionOwner").fill("quick-flow-test")
                page.locator('#solutionProfiles input[value="durable"]').check()
                page.locator("#solutionForm").get_by_role("button", name="Create Solution").click()
                expect(page.locator("#swSolution")).to_have_value(solution_id)
                page.wait_for_function("() => !window.solutionWorkbench.inert")
                page.locator("#swNew").click()
                expect(page.locator("#wfSolution")).to_have_value(solution_id)
                page.locator("#wfTitle").fill("Quick flow")
                page.locator("#wfId").fill(solution_id)
                panel = page.locator("#wfProperties")
                for kind, target, payload in (
                    ("skill", "echo", {"text": "Quick flow works"}),
                    ("skill", skill_id, {"unit_price": 12, "quantity": 3, "discount_percent": 10}),
                    ("tool", None, {"unit_price": 12, "quantity": 3, "discount_percent": 10}),
                ):
                    page.locator("#wfKind").select_option(kind)
                    page.locator("#wfAdd").click()
                    choices = panel.get_by_label("Target", exact=True)
                    if target is None:
                        target = page.evaluate("state.tools[0].tool_id")
                    choices.select_option(target)
                    panel.get_by_label("Input JSON", exact=True).fill(json.dumps(payload))
                    panel.get_by_label("Input JSON", exact=True).press("Tab")
                page.locator("#wfRelease").click()
                expect(page.locator("#swQuickRun")).to_be_visible()
                page.wait_for_function("() => !window.solutionWorkbench.inert")
                with page.expect_response(
                    lambda response: response.url.endswith("/v1/durable-runs")
                    and response.request.method == "POST", timeout=90000,
                ) as started:
                    page.locator("#swQuickRun").click()
                assert started.value.ok, started.value.text()
                run = started.value.json()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(
                        lambda workflow_id=run["workflow_id"]: asyncio.run(
                            workflow_result(args.temporal, workflow_id),
                        ),
                    ).result(timeout=130)
                page.wait_for_function("() => !window.solutionWorkbench.inert")
                page.locator("#swRunRefresh").click()
                expect(page.locator("#swRunStatus")).to_contain_text("completed", timeout=30000)
                expect(page.locator("#swRunResult")).to_contain_text("32.4")
                expect(page.locator("#swReview")).to_be_hidden()
                report["runs"].append({
                    "width": width, "solution_id": solution_id,
                    "run_id": run["run_id"], "result": result,
                })
                page.screenshot(path=str(args.output / f"completed-{width}.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                if width == 1440:
                    page.locator("#swQuick").click()
                    expect(page.locator("#swStatus")).to_have_text("Published", timeout=60000)
                    expect(page.locator("#swVersion")).to_have_value("1.0.1")
                page.reload()
                page.wait_for_function(
                    "() => typeof state !== 'undefined' && state.status !== null",
                )
                page.locator('[data-view="solutions"]').click()
                page.wait_for_function("() => !window.solutionWorkbench.inert")
                page.locator("#swSolution").select_option(solution_id)
                history = page.locator("#swHistoryList tbody tr")
                expect(history.filter(has_text="active").first).to_be_visible()
            assert not report["errors"], report["errors"]
            report["status"] = "passed"
        except Exception as error:
            report.update(status="failed", failure=str(error))
            page.screenshot(path=str(args.output / "failure.png"), full_page=True)
            raise
        finally:
            context.tracing.stop(path=str(args.output / "trace.zip"))
            context.close()
            browser.close()
            (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()