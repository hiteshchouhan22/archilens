"""
Screenshot the ArchiLens comment on a public pull request, as GitHub renders it
(used for docs/images/). Needs Playwright and an installed Chrome:

    pip install playwright
    python scripts/pr_comment_screenshot.py https://github.com/OWNER/REPO/pull/N full.png top.png

full.png: the whole comment, with GitHub's comment header.
top.png:  from the comment header down to the "Changes" table heading.
"""

import sys

from playwright.sync_api import sync_playwright

url, full_path, top_path = sys.argv[1:4]
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    # Wide enough that GitHub gives the comment room for the whole change diagram.
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="light")
    page.goto(url, wait_until="networkidle", timeout=90_000)
    body = page.locator(".markdown-body").filter(has_text="ArchiLens: Architecture Diff").first
    body.scroll_into_view_if_needed()
    # The Mermaid diagram renders inside an iframe; wait for its SVG.
    page.frame_locator(".markdown-body iframe").first.locator("svg").first.wait_for(timeout=60_000)
    page.wait_for_timeout(1500)

    # GitHub's sticky PR header floats over whatever is scrolled under it: hide every
    # sticky or fixed element. Inside the diagram iframe, hide the zoom and pan buttons.
    page.evaluate(
        """() => { for (const el of document.querySelectorAll('body *')) {
            const position = getComputedStyle(el).position;
            if (position === 'sticky' || position === 'fixed') el.style.setProperty('display', 'none', 'important');
        } }"""
    )
    for frame_obj in page.frames:
        if frame_obj != page.main_frame and frame_obj.locator("svg").count():
            frame_obj.add_style_tag(content="button, [role=button] { display: none !important; }")
    page.wait_for_timeout(500)

    # The comment's frame (header "github-actions commented" + body), if the page has one.
    frame = body
    for xpath in (
        "xpath=ancestor::div[contains(@class,'timeline-comment-group')][1]",
        "xpath=ancestor::div[contains(@class,'timeline-comment')][1]",
        "xpath=ancestor::div[contains(@class,'react-issue-comment')][1]",
    ):
        candidate = body.locator(xpath)
        if candidate.count():
            frame = candidate.first
            break
    frame.screenshot(path=full_path)

    box = page.evaluate(
        """([frameEl, bodyEl]) => {
            const f = frameEl.getBoundingClientRect();
            const changes = [...bodyEl.querySelectorAll('h3')].find(h => h.textContent.trim() === 'Changes');
            const bottom = changes ? changes.getBoundingClientRect().top - 8 : f.bottom;
            return {x: f.left + scrollX, y: f.top + scrollY, width: f.width, height: bottom - f.top};
        }""",
        [frame.element_handle(), body.element_handle()],
    )
    page.screenshot(path=top_path, clip=box, full_page=True)
    print("full:", frame.bounding_box(), "top:", box)
    browser.close()
