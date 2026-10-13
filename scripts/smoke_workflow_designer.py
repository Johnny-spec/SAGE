"""Exercise workflow draft editing against an isolated development preview."""

import argparse
from pathlib import Path
from uuid import uuid4

import yaml
from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8013")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context()
        solution_id = f"flow-{uuid4().hex[:8]}"
        created = context.request.post(args.url + "/v1/admin/solutions", data={
            "solution_id": solution_id, "title": "Workflow studio", "owner": "preview",
        })
        assert created.ok, created.text()
        for width, height in ((1440, 1000), (390, 844)):
            page = context.new_page()
            page.set_viewport_size({"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error, *, errors=errors: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(args.url + "/admin")
            page.locator('[data-view="workflows"]').click()
            expect(page.locator("#wfDraft option").first).to_have_text("Select a draft")
            page.locator("#wfNew").click()
            expect(page.locator("#wfEditor")).to_be_visible()
            page.locator("#wfTitle").fill(f"Review {width}")
            page.locator("#wfId").fill(f"review-{width}")
            page.locator("#wfSolution").select_option(solution_id)
            page.locator("#wfKind").select_option("skill")
            page.locator("#wfAdd").click()
            panel = page.locator("#wfProperties")
            panel.get_by_label("Target", exact=True).select_option("echo")
            first_id = page.locator(".wf-node").first.get_attribute("data-step")
            panel.get_by_label("Input JSON", exact=True).fill('{"text":"hello"}')
            panel.get_by_label("Input JSON", exact=True).press("Tab")
            page.locator("#wfAdd").click()
            panel.get_by_label("Target", exact=True).select_option("echo")
            second_id = page.locator(".wf-node").nth(1).get_attribute("data-step")
            panel.get_by_label(first_id, exact=True).check()
            bound = panel.get_by_role("group", name="Bindings", exact=True)
            bound.get_by_role("button", name="Add binding").click()
            bound.get_by_label("Parameter", exact=True).fill("text")
            bound.get_by_label("Parameter", exact=True).press("Tab")
            bound.get_by_label("Source", exact=True).select_option("step-output")
            bound.get_by_label("Upstream node", exact=True).select_option(first_id)
            bound.get_by_label("JSON Pointer", exact=True).fill("/output/text")
            bound.get_by_label("JSON Pointer", exact=True).press("Tab")
            page.locator("#wfArrange").click()
            assert page.locator("#wfEdges > path").count() == 1
            if width > 760:
                first = page.locator(f'[data-step="{first_id}"]')
                bounds = first.bounding_box()
                page.mouse.move(bounds["x"] + 40, bounds["y"] + 40)
                page.mouse.down()
                page.mouse.move(bounds["x"] + 60, bounds["y"] + 80, steps=5)
                page.mouse.up()
            page.locator("#wfSave").click()
            expect(page.locator("#wfStatus")).to_contain_text("Saved")
            page.locator("#wfValidate").click()
            expect(page.locator("#wfValidation")).to_contain_text(
                "Structure and active targets valid",
            )
            with page.expect_download() as downloaded:
                page.locator("#wfExport").click()
            output = args.output / f"workflow-{width}.yaml"
            downloaded.value.save_as(output)
            definition = yaml.safe_load(output.read_text(encoding="utf-8"))
            assert definition["steps"][1]["dependsOn"] == [first_id]
            assert definition["steps"][1]["bindings"]["text"] == {
                "source": "step-output", "stepId": first_id,
                "path": "/output/text",
            }
            with page.expect_download() as design_download:
                page.locator("#wfPackage").click()
            design_path = args.output / f"workflow-design-{width}.json"
            design_download.value.save_as(design_path)
            page.locator("#wfTitle").fill("Changed title")
            page.locator("#wfTitle").press("Tab")
            expect(page.locator("#wfStatus")).to_contain_text("Unsaved changes")
            page.locator("#wfUndo").click()
            expect(page.locator("#wfTitle")).to_have_value(f"Review {width}")
            page.locator("#wfReload").click()
            expect(page.locator("#wfStatus")).to_contain_text("Saved")
            page.locator(f'[data-step="{second_id}"]').click()
            expect(panel.get_by_label("JSON Pointer", exact=True)).to_have_value("/output/text")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.evaluate("scrollTo(0, 0)")
            page.screenshot(path=str(args.output / f"designer-{width}.png"), full_page=True)
            assert not errors, errors
            page.locator("#wfDelete").click()
            expect(page.locator("#wfEditor")).to_be_hidden()
            page.locator("#wfFile").set_input_files(design_path)
            expect(page.locator("#wfEditor")).to_be_visible()
            page.locator("#wfSave").click()
            expect(page.locator("#wfStatus")).to_contain_text("Saved")
            page.locator(f'[data-step="{second_id}"]').click()
            expect(panel.get_by_label("JSON Pointer", exact=True)).to_have_value("/output/text")
            page.locator("#wfDelete").click()
            expect(page.locator("#wfEditor")).to_be_hidden()
            page.close()
        browser.close()
    print("PASS: desktop/mobile create, bind, save, validate, export, undo, reload and delete.")


if __name__ == "__main__":
    main()