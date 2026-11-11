import csv,io,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'portal'))
from server import start
from playwright.sync_api import sync_playwright
server,base=start()
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox']);context=browser.new_context(accept_downloads=True);page=context.new_page()
        page.goto(base);page.get_by_role('button',name='进入演练账号').click();page.wait_for_function("!document.getElementById('export').disabled")
        page.select_option('#org','b');page.wait_for_function("document.getElementById('selected').dataset.org==='b'")
        page.get_by_role('button',name='生成导出').click();link=page.locator('#downloads a');link.wait_for()
        with page.expect_download() as event:link.click()
        download=event.value;rows=list(csv.DictReader(io.StringIO(Path(download.path()).read_text(encoding='utf-8-sig'))));assert rows[0]['org']=='b' and rows[0]['export_id']+'.csv'==download.suggested_filename
        other=browser.new_context();response=other.request.get(base+'/export/'+rows[0]['export_id']);assert response.status==404
        page.get_by_role('button',name='结束会话').click();page.wait_for_function("document.getElementById('status').textContent==='登录已失效'")
        other.close();context.close();browser.close()
finally:server.shutdown();server.server_close()
print('门户实际浏览器：联动、下载标识、会话隔离和失效检查通过')
