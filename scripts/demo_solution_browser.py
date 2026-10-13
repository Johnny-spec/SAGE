"""Record a real, UI-only Solution creation and publication walkthrough."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8015")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    solution_id = "s" + uuid4().hex[:4]
    report = {"url": args.url, "solution_id": solution_id, "steps": [], "errors": []}
    stage = "open"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge", headless=args.headless, slow_mo=500,
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            record_video_dir=str(args.output / "video"),
            record_video_size={"width": 1440, "height": 1000},
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        def capture(name: str) -> None:
            page.screenshot(path=str(args.output / f"{name}.png"), full_page=True)
            report["steps"].append(name)
            print(f"{name}: {page.locator('#swStatus').inner_text()}", flush=True)

        try:
            page.goto(args.url + "/admin")
            page.wait_for_function("() => typeof state !== 'undefined' && state.status !== null")
            assert page.evaluate("environment") == "development", "Use a development preview"
            page.locator('[data-view="solutions"]').click()
            page.wait_for_function("() => !window.solutionWorkbench.inert")
            page.locator("#swSolution").select_option("")
            report["initial"] = {
                "create_form_visible": page.locator("#solutionForm").is_visible(),
                "new_workflow_enabled": page.locator("#swNew").is_enabled(),
                "resolve_enabled": page.locator("#swResolve").is_enabled(),
            }
            capture("01-new-solution-entry")

            stage = "create-solution-form"
            page.get_by_text("Definitions and manual Bundle import", exact=True).click()
            page.locator("#solutionId").fill(solution_id)
            page.locator("#solutionTitle").fill(solution_id.upper())
            page.locator("#solutionOwner").fill("browser-demo")
            page.locator('#solutionProfiles input[value="durable"]').check()
            capture("02-create-form")
            with page.expect_response(
                lambda response: response.url.endswith("/v1/admin/solutions")
                and response.request.method == "POST",
            ) as created:
                page.locator("#solutionForm").get_by_role("button", name="Create Solution").click()
            report["create_status"] = created.value.status
            assert created.value.ok, created.value.text()
            expect(page.locator(f'#swSolution option[value="{solution_id}"]')).to_have_count(1)
            page.wait_for_function("() => !window.solutionWorkbench.inert")
            report["selection_after_create"] = page.locator("#swSolution").input_value()
            capture("03-created-selection")

            stage = "create-workflow"
            page.locator("#swSolution").select_option(solution_id)
            page.locator("#swNew").click()
            expect(page.locator("#wfSolution")).to_have_value(solution_id)
            page.locator("#wfTitle").fill("Browser echo")
            page.locator("#wfId").fill(solution_id)
            page.locator("#wfKind").select_option("skill")
            page.locator("#wfAdd").click()
            panel = page.locator("#wfProperties")
            panel.get_by_label("Target", exact=True).select_option("echo")
            panel.get_by_label("Input JSON", exact=True).fill('{"text":"UI publication works"}')
            panel.get_by_label("Input JSON", exact=True).press("Tab")
            capture("04-workflow")
            page.locator("#wfRelease").click()
            expect(page.locator("#swSolution")).to_have_value(solution_id)
            expect(page.locator("#swResolve")).to_be_enabled()

            stage = "resolve-build"
            page.locator("#swResolve").click()
            expect(page.locator("#swStatus")).to_have_text("Dependencies resolved")
            page.locator("#swConfirm").check()
            capture("05-review-dependencies")
            page.locator("#swBuild").click()
            expect(page.locator("#swResult h3")).to_have_text(
                f"{solution_id}@1.0.0 · valid", timeout=30000,
            )
            report["candidate_id"] = page.locator("#swResult > p").inner_text()
            capture("06-built-not-published")

            stage = "publish"
            page.locator("#swResult").get_by_role(
                "button", name="Publish (development)",
            ).click()
            expect(page.locator("#swStatus")).to_have_text("Published", timeout=60000)
            report["generation"] = page.locator("#swResult > p").inner_text()
            capture("07-published")

            stage = "refresh-persistence"
            page.reload()
            page.wait_for_function("() => typeof state !== 'undefined' && state.status !== null")
            page.locator('[data-view="solutions"]').click()
            page.wait_for_function("() => !window.solutionWorkbench.inert")
            page.locator("#swSolution").select_option(solution_id)
            row = page.locator("#swHistoryList tbody tr").filter(has_text="1.0.0")
            expect(row.locator("td").nth(1)).to_have_text("active")
            capture("08-active-after-reload")
            assert not report["errors"], report["errors"]
            report["status"] = "published"
        except Exception as error:
            report.update(status="failed", failed_stage=stage, failure=str(error))
            page.screenshot(path=str(args.output / "failure.png"), full_page=True)
            raise
        finally:
            context.tracing.stop(path=str(args.output / "trace.zip"))
            context.close()
            browser.close()
            (args.output / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8",
            )
            print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()