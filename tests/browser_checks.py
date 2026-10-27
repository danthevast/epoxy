"""Real Chromium checks against an isolated temporary database. No external requests."""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server
from app import create_app, TYPES, TIMES

def main():
    root=Path(__file__).resolve().parents[1]
    output=root/'artifacts'
    output.mkdir(exist_ok=True)
    checks=0
    with tempfile.TemporaryDirectory() as temp:
        app=create_app(dict(TESTING=True,SECRET_KEY='browser-test-key-'*4,
            LEAD_KEY=Fernet.generate_key(),ADMIN_PASSWORD_HASH=generate_password_hash('browser-test-only'),
            DATABASE=str(Path(temp)/'leads.sqlite3'),MIN_FORM_SECONDS=0,PRODUCTION=False,
            SESSION_COOKIE_SECURE=False))
        server=make_server('127.0.0.1',0,app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                executable=os.getenv('CHROMIUM_PATH') or shutil.which('chromium')
                browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
                errors=[]
                for width,height in [(1440,1050),(768,1024),(390,844),(320,740)]:
                    context=browser.new_context(viewport={'width':width,'height':height},is_mobile=width<700,has_touch=width<700)
                    page=context.new_page()
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    for variant in ('a','b'):
                        response=page.goto(base+'/?v='+variant,wait_until='networkidle')
                        assert response.status==200
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,variant,'horizontal overflow')
                        assert page.locator('h1').is_visible()
                        page.locator('a[href="#quote"]').first.click()
                        assert page.locator('#project_type').is_visible()
                        assert page.locator('button[type=submit]').bounding_box()['height']>=44
                        assert page.locator('#share_consent').is_checked() is False
                        checks+=1
                        if width==1440:
                            page.evaluate('window.scrollTo(0,0)')
                            page.screenshot(path=str(output/f'version-{variant}-desktop.png'),full_page=True)
                            page.screenshot(path=str(output/f'version-{variant}-top.png'))
                        if width==390:
                            page.evaluate('window.scrollTo(0,0)')
                            page.screenshot(path=str(output/f'version-{variant}-mobile.png'),full_page=True)
                    if width==390:
                        page.locator('#project_type').select_option(TYPES[0])
                        page.locator('#sqft').fill('2500')
                        page.locator('#zip').fill('63103')
                        page.locator('#timeframe').select_option(TIMES[1])
                        page.locator('#name').fill('Browser Test')
                        page.locator('#email').fill('browser@example.test')
                        page.locator('#details').fill('Synthetic test, not a real lead.')
                        # Client-side contact requirement and unchecked sharing consent.
                        page.locator('button[type=submit]').click()
                        assert '/thanks' not in page.url
                        page.locator('#share_consent').check()
                        page.locator('button[type=submit]').click()
                        page.wait_for_url('**/thanks')
                        assert page.get_by_text('Thanks for',exact=False).is_visible()
                        checks+=1
                        page.screenshot(path=str(output/'confirmation-mobile.png'),full_page=True)
                        page.goto(base+'/admin')
                        assert page.url.endswith('/admin/login')
                        page.locator('#password').fill('browser-test-only')
                        page.get_by_role('button',name='Sign in').click()
                        page.wait_for_url('**/admin')
                        assert page.get_by_text('Browser Test',exact=True).is_visible()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(output/'admin-mobile.png'),full_page=True)
                        page.get_by_role('button',name='Mark reviewed').click()
                        assert page.get_by_text('reviewed',exact=True).is_visible()
                        checks+=1
                    context.close()
                # A desktop dashboard screenshot with synthetic data only.
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.goto(base+'/admin/login')
                page.locator('#password').fill('browser-test-only')
                page.get_by_role('button',name='Sign in').click()
                page.wait_for_url('**/admin')
                page.screenshot(path=str(output/'admin-desktop.png'),full_page=True)
                assert not errors,errors
                browser.close()
                checks+=1
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    print(f'{checks} browser checks passed: both variations at four widths, mobile submission, private dashboard, no JavaScript errors.')
if __name__=='__main__':
    main()
