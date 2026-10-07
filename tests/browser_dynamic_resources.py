"""Browser regression for live resource polling and explicit user overrides.

Run against an isolated server at ENERGYHUB_BROWSER_URL (default :20228).
Only resource responses are simulated; no calculation is submitted.
"""
import asyncio,json,os,time
from pathlib import Path
from playwright.async_api import async_playwright,expect

BASE=os.environ.get('ENERGYHUB_BROWSER_URL','http://127.0.0.1:20228').rstrip('/')
ROOT=Path(__file__).resolve().parents[1]

async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
  page=await browser.new_page(viewport={'width':729,'height':977},reduced_motion='reduce')
  errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
  config=dict(resource_mode='auto',pool_size=8,thread_pool_size=8,cpu_count=8,memory_pool_mb=40960,
              memory_total_mb=65536,memory_capacity_mb=65536,memory_available_mb=53248,
              recommended_memory_pool_mb=40960,memory_used_mb=0,slots_used=0,thread_used=0,
              resource_refresh_seconds=2,max_upload_bytes=64*1024**2)
  writes=[]
  async def resource_route(route):
   if route.request.method=='PUT':
    payload=route.request.post_data_json;writes.append(payload)
    if payload['resource_mode']=='auto':
     assert payload=={'resource_mode':'auto'},payload
     config.update(resource_mode='auto',pool_size=config['cpu_count'],thread_pool_size=config['cpu_count'],memory_pool_mb=config['recommended_memory_pool_mb'])
    else:
     config.update(payload)
   config['resources_checked_at']=time.time()
   await route.fulfill(json=config)
  await page.route('**/api/energyhub/config*',resource_route)
  await page.goto(BASE+'/#new',wait_until='networkidle')
  await expect(page.locator('#memoryPool')).to_have_value('40')
  await expect(page.locator('#resourceLimitSummary')).to_contain_text('Auto')
  config.update(memory_pool_mb=16384,memory_available_mb=20480,recommended_memory_pool_mb=16384)
  await expect(page.locator('#memoryPool')).to_have_value('16',timeout=9000)
  await expect(page.locator('#resourceLimitSummary')).to_contain_text('20 GB RAM available')
  await page.locator('#memoryPool').fill('8')
  await page.locator('#taskName').click()
  config.update(memory_pool_mb=32768,memory_available_mb=40960,recommended_memory_pool_mb=32768)
  await expect(page.locator('#memoryPool')).to_have_attribute('max','32',timeout=9000)
  await expect(page.locator('#memoryPool')).to_have_value('8')
  await page.locator('#useServerResources').click()
  await expect(page.locator('#memoryPool')).to_have_value('32')
  await page.locator('#referenceFile').set_input_files(str(ROOT/'examples/H2.ref'))
  await page.locator('#openResourceSettings').click()
  await expect(page.locator('#autoResources')).to_be_checked()
  await expect(page.locator('#globalMemory')).to_be_disabled()
  await page.locator('#autoResources').uncheck()
  await expect(page.locator('#globalMemory')).to_be_enabled()
  await page.locator('#globalMemory').fill('6144')
  await page.locator('#saveSettings').click()
  await expect(page.locator('#resourceLimitSummary')).to_contain_text('Manual')
  await expect(page.locator('#memoryPool')).to_have_value('6')
  assert writes[-1]['memory_pool_mb']==6144
  await page.locator('#openResourceSettings').click()
  await page.locator('#autoResources').check()
  await page.locator('#saveSettings').click()
  await expect(page.locator('#resourceLimitSummary')).to_contain_text('Auto')
  await expect(page.locator('#memoryPool')).to_have_value('32')
  await expect(page.locator('#referenceFileLabel')).to_contain_text('H2.ref')
  assert not errors,errors
  await browser.close()
  print(json.dumps({'status':'passed','checks':['live RAM polling','automatic default updates','preserved user task edits','server-value reset','auto/manual switching','no stale numeric limits sent in auto mode','uploaded reference preserved'],'browser_errors':errors}))

if __name__=='__main__':
 asyncio.run(main())
