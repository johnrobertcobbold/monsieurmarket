# bloomberg_camoufox/setup.py
import json
import time
from pathlib import Path
from camoufox.sync_api import Camoufox

SESSION_FILE = Path(__file__).parent / 'bloomberg_session.json'

print('🦊 Bloomberg Camoufox Setup')
print('Browser will open — log in manually\n')

with Camoufox(headless=False) as browser:
    page = browser.new_page()
    
    page.goto('https://www.bloomberg.com', timeout=30000)
    
    print('👋 Please log in to Bloomberg...')
    print('   Waiting for subscriber session...\n')
    
    # wait for real subscriber login
    page.wait_for_function("""() => {
        const userData = document.cookie
            .split(';')
            .find(c => c.trim().startsWith('_user-data='));
        if (!userData) return false;
        try {
            const decoded = decodeURIComponent(
                userData.split('=').slice(1).join('=')
            );
            const parsed = JSON.parse(decoded);
            return parsed?.status === 'logged_in' &&
                   parsed?.subscriberData?.subscriptionStatus === 'active';
        } catch { return false; }
    }""", timeout=300000)
    
    print('✅ Subscriber session detected!')
    time.sleep(3)  # let cookies settle
    
    # save cookies
    cookies = page.context.cookies()
    
    # save localStorage
    local_storage = page.evaluate("""() => {
        const items = {};
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            items[key] = localStorage.getItem(key);
        }
        return items;
    }""")
    
    # sanity check
    important = ['_user-token', 'session_key', '_user-data', '_user-role']
    print('\n📋 Captured cookies:')
    for name in important:
        found = any(c['name'] == name for c in cookies)
        print(f"   {'✅' if found else '❌'} {name}")
    
    SESSION_FILE.write_text(json.dumps({
        'cookies':      cookies,
        'localStorage': local_storage,
        'savedAt':      time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }, indent=2))
    
    print(f'\n✅ Session saved to {SESSION_FILE}')
    print('   You can now run the monitor!\n')
    
    time.sleep(2)

print('Done!')