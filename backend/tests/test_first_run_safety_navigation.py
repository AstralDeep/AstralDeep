
from html.parser import HTMLParser

import pytest

from orchestrator.projection_surfaces import llm
from tests.chrome.test_surface_llm import make_orch


class Buttons(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.controls = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.controls.append(dict(attrs))


@pytest.mark.asyncio
async def test_regular_provider_settings_do_not_add_a_first_run_navigation_control():
    orch = make_orch()
    html = await llm.render(orch, "owner", ["user"], {})
    components = await llm.components(orch, "owner", ["user"], {})
    assert all(item.get("data-action") != "chrome_open" for item in Buttons(html).controls)
    assert all(item.get("action") != "chrome_open" for item in components)
