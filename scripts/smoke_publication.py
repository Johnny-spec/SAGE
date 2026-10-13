"""Verify publication handoffs without mutating server resources."""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--live-demo", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for width, height in ((1440, 1000), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error, *, errors=errors: errors.append(str(error)))
            page.goto(args.url + "/admin")
            expect(page.locator("#connectionState")).not_to_have_text("Disconnected")
            page.wait_for_function("() => state.status !== null")
            page.wait_for_function(
                "() => document.querySelector('#swStatus').textContent !== 'Working...'"
            )
            if args.live_demo:
                for refresh in (False, True):
                    if refresh:
                        page.reload()
                        page.wait_for_function("() => state.status !== null")
                    page.locator('[data-view="solutions"]').click()
                    page.wait_for_function("() => !window.solutionWorkbench.inert")
                    expect(page.locator('#swSolution option[value="flow"]')).to_have_count(1)
                    page.locator("#swSolution").select_option("flow")
                    expect(page.locator("#swHistoryList tbody tr")).to_have_count(2)
                    current = page.locator("#swHistoryList tbody tr").filter(has_text="1.1.0")
                    expect(current.locator("td").nth(1)).to_have_text("active")
                    previous = page.locator("#swHistoryList tbody tr").filter(has_text="1.0.0")
                    expect(previous.locator("td").nth(1)).to_have_text("inactive")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.screenshot(path=str(args.output / f"release-{width}.png"), full_page=True)
                current.get_by_role("button").click()
                expect(page.locator('[data-view-panel="catalog"]')).to_be_visible()
                page.screenshot(path=str(args.output / f"catalog-{width}.png"), full_page=True)
                page.locator('[data-view="mcp"]').click()
                page.locator("#mcpList .resource-item").click()
                expect(page.locator("#mcpTools")).to_contain_text("Native tool versions")
                expect(page.locator("#mcpTools .status-badge")).to_have_text("active")
            page.evaluate("""() => {
              const definition = {agent_id: 'ui-echo', name: 'UI Echo', version: '1.0.0'};
              state.agents = [{draft_id:'ui-agent', definition, status:'valid'}];
              state.skills = [{package_id:'ui-skill', name:'UI Calc', version:'1.0.0',
                status:'valid', source:'mcp', execution_type:'mcp', executable:true,
                validation:{issues:[], content_digest:'sha256:ui'}}];
              state.tools = [{registration_id:'ui-tool', tool_id:'tool:mcp:ui:total',
                status:'draft', definition:{version:'1.0.0', binding:{server_id:'ui-server',
                server_alias:'UI', tool_name:'total'}}}];
              state.selectedMcp = {server_id:'ui-server', tools:[]};
              renderAgents(); renderSkills(); renderMcpTools(); renderCatalog();
              navigate('agents');
            }""")
            page.locator("#agentList").get_by_role("button", name="Prepare publication").click()
            expect(page.locator('#candidateAgents input[value="ui-agent"]')).to_be_checked()
            expect(page.locator("#candidateTitle")).to_have_value("UI Echo 1.0.0")
            page.evaluate("renderCatalog()")
            expect(page.locator('#candidateAgents input[value="ui-agent"]')).to_be_checked()
            page.evaluate("navigate('skills')")
            page.locator("#skillPackageList").get_by_role(
                "button",
                name="Prepare publication",
            ).click()
            expect(page.locator('#candidateSkills input[value="ui-skill"]')).to_be_checked()
            expect(page.locator('#candidateAgents input[value="ui-agent"]')).to_be_checked()
            page.evaluate("navigate('mcp')")
            page.locator("#mcpTools").get_by_role("button", name="Prepare publication").click()
            expect(page.locator('#candidateTools input[value="ui-tool"]')).to_be_checked()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            page.screenshot(path=str(args.output / f"publication-{width}.png"), full_page=True)
            page.evaluate("""() => {
              const definition = state.agents[0].definition;
              state.agents[0].status = 'published';
              activeGeneration().agent_definitions['ui-echo'] = {...definition};
              renderAgents();
            }""")
            expect(page.locator("#agentList .status-badge")).to_have_text("active")
            page.evaluate("""() => {
              activeGeneration().agent_definitions['ui-echo'].version = '2.0.0';
              renderAgents();
              state.candidates = [{candidate_id:'ui-candidate', title:'UI candidate',
                status:'valid', base_generation_id:'ui-base', validation:{status:'valid'}}];
              environment = 'production'; renderCandidateTable();
            }""")
            expect(page.locator("#agentList .status-badge")).to_have_text("published (historical)")
            expect(
                page.locator("#candidateList").get_by_role(
                    "button",
                    name="Publish",
                    exact=True,
                )
            ).to_have_count(0)
            expect(
                page.locator("#candidateList").get_by_role(
                    "button",
                    name="Submit",
                    exact=True,
                )
            ).to_have_count(1)
            assert not errors, errors
            page.close()
        browser.close()
    print("Desktop/mobile publication handoffs, selections, states and production gate passed")


if __name__ == "__main__":
    main()
