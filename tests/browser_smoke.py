"""Real Chromium workflow test against an isolated, running Energyhub service.

Install playwright in your test environment and run `playwright install chromium`.
Start Energyhub with a dedicated --data-dir, >=2 slots, >=2 CPU threads, >=2048MB.
Set ENERGYHUB_BROWSER_URL (default http://127.0.0.1:20228), then run this file.
The script creates real H2 jobs and writes downloads/screenshots under .tmp/.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import tarfile
import time
from playwright.async_api import async_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.tmp/browser-validation'
BASE=os.environ.get('ENERGYHUB_BROWSER_URL','http://127.0.0.1:20228').rstrip('/')


def geometry_archive(count=12):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for index in range(count):
            content=f'2\n0 1\nH 0 0 0\nH 0 0 {0.74 + index * 0.01}\n'.encode()
            member=tarfile.TarInfo(f'H2_{index}.xyz');member.size=len(content)
            archive.addfile(member,io.BytesIO(content))
    return stream.getvalue()


async def main():
    OUT.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=await browser.new_context(viewport={'width':1440,'height':1080},accept_downloads=True,reduced_motion='reduce')
        page=await context.new_page()
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.goto(BASE,wait_until='networkidle')
        await expect(page.locator('#engineLabel')).to_contain_text('connected')
        await expect(page.locator('#submitTask')).to_be_enabled()
        await page.screenshot(path=str(OUT/'new-task.png'),full_page=True)
        await page.locator('#submitTask').click()
        await expect(page.locator('#formError')).to_contain_text('Choose both')
        await page.locator('label:has(input[name="method"][value="CCSDT(Q)"])').click()
        await expect(page.locator('#methodNotice')).to_contain_text('closed-shell')
        await page.locator('label:has(input[name="basis"][value="CBS"])').click()
        await page.locator('label:has(input[name="basis_family"][value="aug"])').click()
        await page.locator('label:has(input[name="cbs_pair"][value="45"])').click()
        await expect(page.locator('#fullLabel')).to_contain_text('aug-cc-pV[Q5]Z')
        await page.locator('label:has(input[name="method"][value="CCSD(T)"])').click()
        await page.locator('label:has(input[name="basis"][value="3zeta"])').click()
        await page.locator('label:has(input[name="basis_family"][value="cc"])').click()
        await page.locator('#loadSample').click()
        await expect(page.locator('#archiveFileLabel')).to_contain_text('H2.tgz')
        title='Browser H₂ validation '+str(int(time.time()))
        await page.locator('#taskName').fill(title)
        await page.locator('#threadBudget').fill('2')
        await page.locator('#taskThreads').fill('1')
        await page.locator('#memoryPool').fill('1024')
        await expect(page.locator('#allocationSummary')).to_contain_text('2 at a time')
        await page.locator('#submitTask').click()
        await expect(page).to_have_url(__import__('re').compile(r'.*#task/[a-f0-9]{32}$'))
        identifier=page.url.rsplit('/',1)[-1]
        await page.wait_for_function("['Completed','Failed','Cancelled'].includes(document.querySelector('#jobContent .status-badge')?.textContent.trim())",timeout=120000)
        await expect(page.locator('#jobContent .status-badge')).to_have_text('Completed')
        await expect(page.locator('#jobTitle')).to_have_text(title)
        await expect(page.locator('#jobContent')).to_contain_text('Molecular energies')
        await expect(page.locator('#jobContent')).to_contain_text('Reference values',timeout=15000)
        await page.screenshot(path=str(OUT/'completed-task.png'),full_page=True)
        for selector,name in [('#downloadReference','completed.ref'),('#downloadReport','report.json'),('#downloadCSV','energies.csv')]:
            async with page.expect_download() as download:
                await page.locator(selector).click()
            await (await download.value).save_as(OUT/name)
        report=json.loads((OUT/'report.json').read_text())
        values={row['name']:row['energy_hartree'] for row in report['molecularEnergies']}
        output=float((OUT/'completed.ref').read_text().split()[-2])
        assert abs(output-(values['H2_stretched']-values['H2'])*627.509)<1e-8
        assert 'cc-pV?Z' not in (OUT/'energies.csv').read_text()
        assert 'cc-pVTZ' in (OUT/'energies.csv').read_text()
        await page.reload(wait_until='networkidle')
        await expect(page.locator('#jobTitle')).to_have_text(title)
        await page.locator('#taskSearch').fill('no-such-energyhub-task')
        await expect(page.locator('#taskList')).to_contain_text('No matching tasks')
        await page.locator('#taskSearch').fill('')
        await page.locator('a[data-nav="results"]').click()
        await expect(page.locator('#resultsContent')).to_contain_text(title)
        await page.locator('#openSettings').click()
        await expect(page.locator('#settingsDialog')).to_be_visible()
        current_memory=await page.locator('#globalMemory').input_value()
        await page.locator('#globalMemory').fill(current_memory)
        await page.locator('#saveSettings').click()
        await expect(page.locator('#settingsDialog')).not_to_be_visible()
        await page.locator('#newTaskLink').click()
        await page.locator('#threadBudget').fill('1')
        await page.locator('#memoryPool').fill('512')
        await page.locator('#taskName').fill('Browser cancellation test')
        await page.locator('#archiveFile').set_input_files({'name':'cancel.tgz','mimeType':'application/gzip','buffer':geometry_archive()})
        await page.locator('#referenceFile').set_input_files({'name':'cancel.ref','mimeType':'text/plain','buffer':b''})
        await page.locator('#submitTask').click()
        await expect(page.locator('#cancelTask')).to_be_visible()
        await page.locator('#cancelTask').click()
        await expect(page.locator('#jobContent .status-badge')).to_have_text('Cancelled',timeout=30000)
        await expect(page.locator('#downloadReference')).to_have_count(0)
        for width in [390,320]:
            await page.set_viewport_size({'width':width,'height':900})
            for route,view in [('new','newView'),('results','resultsView'),('guide','guideView')]:
                await page.locator(f'a[data-nav="{route}"]').click()
                await expect(page.locator('#'+view)).to_be_visible()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'), (width,route)
            await page.screenshot(path=str(OUT/f'mobile-{width}.png'),full_page=True)
        assert not errors,errors
        await browser.close()
        print(json.dumps({'status':'passed','completed_job':identifier,'reference_value_kcal_mol':output,
                          'checks':['real upload/compute/download','method/basis/CBS selection','reload/history/search',
                                    'JSON/CSV/reference reports','resource settings','cancel with no result','mobile 390/320px'],
                          'browser_errors':errors},ensure_ascii=False,indent=2))

if __name__=='__main__':
    asyncio.run(main())
