"""UI regression for the annotated 729px controls and selection motion.

Run against an isolated Energyhub server with ENERGYHUB_BROWSER_URL configured.
Requires Playwright/Chromium. No calculation is submitted by this script.
"""
import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
BASE=os.environ.get('ENERGYHUB_BROWSER_URL','http://127.0.0.1:20228').rstrip('/')

async def box(locator):
    return await locator.bounding_box()

async def aligned(indicator, target):
    first,second=await box(indicator),await box(target)
    return all(abs(first[key]-second[key]) < 1 for key in ['x','y','width','height'])

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
        context=await browser.new_context(viewport={'width':729,'height':977},reduced_motion='no-preference')
        page=await context.new_page()
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        await page.goto(BASE+'/#new',wait_until='networkidle')
        await expect(page.locator('[data-upload="ref"]')).to_be_visible()
        assert float(await page.locator('#memoryPool').get_attribute('max')) > 8
        assert (await box(page.locator('#newTaskLink .plus svg')))['width']==14
        assert (await box(page.locator('.stepper button svg').first))['width']==12
        card=page.locator('.resource-control').first
        label=card.locator('label').first
        minus=card.locator('.stepper button').first
        plus=card.locator('.stepper button').last
        cb,lb,mb,pb=await box(card),await box(label),await box(minus),await box(plus)
        assert abs(lb['x']-(cb['x']+16))<1
        assert abs(mb['x']-lb['x'])<1
        assert abs(pb['x']+pb['width']-(cb['x']+cb['width']-16))<1
        family=page.locator('.segments').filter(has=page.locator('input[name="basis_family"]'))
        target=family.locator('input[value="cc"] + span')
        indicator=family.locator('.selection-indicator')
        await target.click()
        await page.wait_for_timeout(70)
        moving=await box(indicator);goal=await box(target)
        assert abs(moving['x']-goal['x']) > 1, (moving,goal)
        await page.wait_for_timeout(450)
        assert await aligned(indicator,target)
        for field,value in [('basis','3zeta'),('method','CCSDT'),('basis','CBS'),('cbs_pair','45'),('task_threads_choice','4')]:
            radio=page.locator(f'input[name="{field}"][value="{value}"]')
            await radio.locator('..').click()
            await page.wait_for_timeout(450)
            group=radio.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " option-grid ") or contains(concat(" ", normalize-space(@class), " "), " segments ")][1]')
            selected=radio.locator('..') if field in ['method','basis'] else radio.locator('xpath=following-sibling::span[1]')
            assert await aligned(group.locator(':scope > .selection-indicator'),selected), field
        # Rapid reversals must leave conditional controls correctly expanded/collapsed.
        for value in ['3zeta','CBS','4zeta','CBS']:
            await page.locator(f'label:has(input[name="basis"][value="{value}"])').click()
            await page.wait_for_timeout(45)
        await page.wait_for_timeout(450)
        await expect(page.locator('#cbsInfo')).to_be_visible()
        await page.locator('#referenceFile').set_input_files(str(ROOT/'examples/H2.ref'))
        await expect(page.locator('#referenceFileLabel')).to_contain_text('H2.ref')
        await page.locator('[data-remove="ref"]').click()
        await expect(page.locator('[data-upload="ref"]')).to_be_visible()
        await expect(page.locator('#referenceFileLabel')).to_contain_text('Drop a .ref')
        await page.locator('#openResourceSettings').click()
        await expect(page.locator('#settingsDialog')).to_be_visible()
        await page.locator('#cancelSettings').click()
        for width in [320,390,729,1440]:
            await page.set_viewport_size({'width':width,'height':977})
            await page.wait_for_timeout(150)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),width
        await page.set_viewport_size({'width':729,'height':977})
        await page.wait_for_timeout(150)
        await page.screenshot(path=str(ROOT/'.tmp/interactions-729.png'),full_page=True)
        assert not errors,errors
        await context.close()
        reduced=await browser.new_context(viewport={'width':729,'height':977},reduced_motion='reduce')
        page=await reduced.new_page()
        await page.goto(BASE+'/#new',wait_until='networkidle')
        await page.locator('label:has(input[name="basis_family"][value="cc"])').click()
        await page.wait_for_timeout(50)
        durations=await page.locator('.selection-indicator').first.evaluate('(element)=>getComputedStyle(element).transitionDuration')
        assert all(float(value.strip().rstrip('s'))==0 for value in durations.split(',')),durations
        await browser.close()
        print(json.dumps({'status':'passed','checks':['memory above 8GB','729px card alignment','correct plus/minus icons','always visible reference uploader','moving segment and card selectors','rapid selection reversal','reduced motion','320/390/729/1440px layout'],'browser_errors':errors}))

if __name__=='__main__':
    asyncio.run(main())
