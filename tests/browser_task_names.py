"""Check optional task names in the browser without submitting calculations."""
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = os.environ.get('ENERGYHUB_BROWSER_URL', 'http://127.0.0.1:20228').rstrip('/')
ROOT = Path(__file__).resolve().parents[1]


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        names, errors = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))

        async def intercept(route):
            if route.request.method != 'POST':
                await route.continue_()
                return
            body = route.request.post_data_buffer
            names.append(body.split(b'name="name"\r\n\r\n', 1)[1].split(b'\r\n', 1)[0].decode())
            await route.fulfill(status=400, json={'error': 'Test intercepted submission; no calculation started.'})

        await page.route('**/api/energyhub/jobs', intercept)
        await page.goto(BASE + '/#new', wait_until='networkidle')
        await page.locator('#archiveFile').set_input_files(str(ROOT / 'examples/H2.tgz'))
        await page.locator('#referenceFile').set_input_files(str(ROOT / 'examples/H2.ref'))
        for value in ('', '   ', 'Custom reference'):
            await page.locator('#taskName').fill(value)
            expected = value.strip() or await page.locator('#taskName').get_attribute('placeholder')
            async with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/jobs')):
                await page.locator('#submitTask').click()
            await expect(page.locator('#submitTask')).to_be_enabled()
            assert names[-1] == expected, (names[-1], expected)
            await expect(page.locator('#referenceFileLabel')).to_contain_text('H2.ref')
        assert not errors, errors
        await browser.close()
        print('Passed: empty, whitespace and custom task names; uploads preserved; no calculations submitted.')


if __name__ == '__main__':
    asyncio.run(main())
