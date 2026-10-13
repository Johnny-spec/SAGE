"""Exercise reviewed Solution publication against an isolated development preview."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8014")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context()
        for width, height in ((1440, 1000), (390, 844)):
            solution_id = f"studio-{uuid4().hex[:8]}"
            created = context.request.post(
                args.url + "/v1/admin/solutions",
                data={
                    "solution_id": solution_id,
                    "title": f"Release studio {width} {solution_id}",
                    "owner": "preview",
                    "supported_profiles": ["durable"],
                },
            )
            assert created.ok, created.text()
            page = context.new_page()
            page.set_viewport_size({"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error, *, errors=errors: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(args.url + "/admin")
            page.locator('[data-view="solutions"]').click()
            expect(page.locator(f'#swSolution option[value="{solution_id}"]')).to_have_count(1)
            page.locator("#swSolution").select_option(solution_id)
            page.locator("#swNew").click()
            expect(page.locator("#wfSolution")).to_have_value(solution_id)
            page.locator("#wfTitle").fill(f"Echo release {width}")
            page.locator("#wfId").fill(f"echo-{width}")
            page.locator("#wfKind").select_option("skill")
            page.locator("#wfAdd").click()
            panel = page.locator("#wfProperties")
            panel.get_by_label("Target", exact=True).select_option("echo")
            panel.get_by_label("Input JSON", exact=True).fill('{"text":"hello"}')
            panel.get_by_label("Input JSON", exact=True).press("Tab")
            page.locator("#wfRelease").click()
            expect(page.locator("#swSolution")).to_have_value(solution_id)
            expect(page.locator("#swResolve")).to_be_enabled()
            page.locator("#swResolve").click()
            expect(page.locator("#swStatus")).to_have_text("Dependencies resolved")
            expect(page.locator("#swDependencies")).to_contain_text("echo")
            expect(page.locator("#swBuild")).to_be_disabled()
            page.locator("#swConfirm").check()
            with page.expect_download() as download:
                page.locator("#swBundle").click()
            download.value.save_as(args.output / f"solution-{width}.zip")
            page.locator("#swBuild").click()
            expect(page.locator("#swResult h3")).to_have_text(f"{solution_id}@1.0.0 · valid")
            page.screenshot(path=str(args.output / f"workbench-{width}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            if width > 760:
                page.locator("#swResult").get_by_role(
                    "button", name="Publish (development)",
                ).click()
                expect(page.locator("#swStatus")).to_have_text("Published", timeout=30000)
            else:
                candidate_id = page.locator("#swResult > p").inner_text()
                page.locator("#swResult").get_by_role("button", name="Submit for approval").click()
                expect(page.locator('[data-view-panel="catalog"]')).to_be_visible()
                approval_row = page.locator("#approvalList tr").filter(has_text=candidate_id)
                approval_row.get_by_role("textbox").fill("Reviewed browser acceptance")
                approval_row.get_by_role("button", name="Approve", exact=True).click()
                candidate_row = page.locator("#candidateList tr").filter(
                    has_text=f"Release studio {width} {solution_id} 1.0.0",
                )
                candidate_row.get_by_role("button", name="Activate", exact=True).click()
                expect(candidate_row).to_contain_text("active", timeout=30000)
                page.locator('[data-view="solutions"]').click()
                expect(page.locator("#swStatus")).not_to_have_text("Working...")

            page.locator("#swAi > summary").click()
            page.locator("#swGoal").fill("Echo a user message")
            page.locator("#swAiTargets").select_option("echo")
            page.locator("#swSuggest").click()
            expect(page.locator("#swStatus")).to_contain_text("offline demo")
            expect(page.locator("#swProposal")).to_be_hidden()
            generation = context.request.get(args.url + "/v1/admin/status").json()[
                "active_generation_id"
            ]
            proposal = {
                "solution_id": solution_id,
                "title": "Suggested echo",
                "description": "Draft",
                "draft": {
                    "solution_id": solution_id,
                    "title": "Suggested echo",
                    "design": {},
                    "definition": {
                        "apiVersion": "sage.dev/v1beta1",
                        "kind": "WorkflowTemplate",
                        "workflowId": "suggested",
                        "version": "1.0.0",
                        "steps": [
                            {
                                "id": "echo",
                                "kind": "skill",
                                "target": "echo",
                                "inheritInput": False,
                                "bindings": {
                                    "text": {"source": "workflow-input", "path": "/message"}
                                },
                            },
                        ],
                    },
                },
                "generation_id": generation,
                "risks": {"skill:echo": "read-only"},
                "warnings": [],
                "recommendations": [{"step_id": "echo", "reason": "Use text <img src=x>"}],
            }
            page.route(
                "**/v1/admin/solution-assist",
                lambda route, *, proposal=proposal: route.fulfill(json=proposal),
            )
            page.locator("#swSuggest").click()
            expect(page.locator("#swProposal")).to_be_visible()
            expect(page.locator("#swApply")).to_be_disabled()
            assert page.locator("#swRecommendations img").count() == 0
            page.locator("#swAiConfirm").check()
            page.screenshot(path=str(args.output / f"ai-proposal-{width}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            page.locator("#swApply").click()
            expect(page.locator("#wfTitle")).to_have_value("Suggested echo")
            expect(panel.get_by_label("JSON Pointer", exact=True)).to_have_value("/message")
            assert not errors, json.dumps(errors)
            page.close()
        context.close()
        browser.close()
    print("Desktop/mobile workbench build, publish and reviewed AI apply passed")


if __name__ == "__main__":
    main()
