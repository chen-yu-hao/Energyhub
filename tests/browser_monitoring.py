"""Verify process display and log polling across task rerenders without calculation."""
import asyncio
import os
import time
from playwright.async_api import async_playwright, expect

BASE = os.environ.get('ENERGYHUB_BROWSER_URL', 'http://127.0.0.1:20228').rstrip('/')

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page(viewport={'width':951, 'height':956})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        identifier = 'a'*32
        job = dict(task_id=identifier, name='Monitoring fixture', state='running', method='CCSD(T)',
                   basis='CBS', basis_family='cc', cbs_pair='34', pool_size=2, task_threads=1,
                   memory_mb=512, memory_pool_mb=1024, created_at=1, started_at=1,
                   progress={'total':2, 'completed':0, 'workers':[
                       dict(name='H2', state='running', pid=12345, stage='SCF', basis='cc-pvtz', started_at=time.time()-5, threads=1, memory_mb=512),
                       dict(name='H', state='queued', pid=None, stage='Waiting', basis='CBS', threads=1, memory_mb=512)]})
        log = {'text':'[H2] SCF started', 'truncated':False}
        async def route(request):
            if '/log?' in request.request.url:
                await request.fulfill(json=log)
            elif request.request.url.endswith('/report'):
                await request.fulfill(json={'molecularEnergies':[], 'reference_rows':[]})
            else:
                await request.fulfill(json=job)
        await page.route('**/api/energyhub/jobs/'+identifier+'**', route)
        await page.goto(BASE+'/#task/'+identifier, wait_until='networkidle')
        await expect(page.locator('#jobContent')).to_contain_text('12345')
        await expect(page.locator('#jobContent')).to_contain_text('SCF')
        elapsed = page.locator('[data-worker-start]').first
        initial_elapsed = await elapsed.inner_text()
        await expect(elapsed).not_to_have_text(initial_elapsed, timeout=4000)
        await page.locator('#showTaskLog').click()
        await expect(page.locator('#taskLog')).to_contain_text('SCF started')
        job['progress']['workers'][0].update(stage='CCSD', basis='cc-pvqz')
        log['text']='[H2] CCSD cycle 2 <not HTML>'
        await expect(page.locator('#jobContent')).to_contain_text('cc-pvqz', timeout=12000)
        await expect(page.locator('#taskLog')).to_contain_text('CCSD cycle 2 <not HTML>', timeout=12000)
        job.update(state='completed', finished_at=5)
        for worker in job['progress']['workers']:
            worker.update(state='completed', stage='Completed', finished_at=5)
        log['text']='Reference file completed'
        await expect(page.locator('#jobContent .status-badge')).to_have_text('Completed', timeout=12000)
        await expect(page.locator('#taskLog')).to_contain_text('Reference file completed', timeout=12000)
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Page overflows on mobile'
        assert not errors, errors
        await browser.close()
        print('Passed: per-structure PID/stage/basis, automatic logs, rerender retention, final log, mobile layout.')

if __name__ == '__main__':
    asyncio.run(main())
