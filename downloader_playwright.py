import os
import sys
import json
import logging
import re
from datetime import datetime, timedelta, timezone
import tempfile
import pathlib
import requests

from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

from downloader import upload_to_drive

# Env
AUSCHOOL_URL = os.environ.get('AUSCHOOL_URL') or 'https://5dang.ebs.co.kr/auschool/sub/language?clsfnSystId1=47140032%3E47140033'
EBS_USERNAME = os.environ.get('EBS_USERNAME')
EBS_PASSWORD = os.environ.get('EBS_PASSWORD')
GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID')
DEBUG_PLAYWRIGHT = os.environ.get('DEBUG_PLAYWRIGHT')
DEBUG_SLOWMO = os.environ.get('DEBUG_SLOWMO')
SKIP_UPLOAD = os.environ.get('SKIP_UPLOAD')
DEBUG_FORCE_LOGIN = os.environ.get('DEBUG_FORCE_LOGIN')

KST = timezone(timedelta(hours=9))


def is_allowed_weekday_kst():
    now_kst = datetime.now(KST)
    return now_kst.weekday() <= 5


def extract_m4a_links_from_page(page):
    links = set()
    # anchor hrefs
    anchors = page.query_selector_all('a[href]')
    for a in anchors:
        href = a.get_attribute('href')
        if href and '.m4a' in href:
            links.add(page.url.rstrip('/') + '/' + href if href.startswith('/') else href)

    # audio/source
    sources = page.query_selector_all('audio source, source')
    for s in sources:
        src = s.get_attribute('src')
        if src and '.m4a' in src:
            links.add(src)

    return sorted(links)


def transfer_cookies_to_requests(context, session: requests.Session):
    cookies = context.cookies()
    for c in cookies:
        # requests requires domain-less cookie keys for set
        session.cookies.set(c['name'], c['value'], domain=c.get('domain'))


def trigger_audio_playback(page):
    try:
        page.evaluate('''() => {
            const audios = Array.from(document.querySelectorAll('audio'));
            if (audios.length) {
                audios.forEach(a => { a.muted = true; a.play().catch(() => {}); });
                return true;
            }
            const btn = Array.from(document.querySelectorAll('button, a')).find(el => /재생|play/i.test(el.textContent));
            if (btn) {
                btn.click();
                return true;
            }
            return false;
        }''')
    except Exception:
        pass


def attempt_login(page):
    # If credentials not provided, skip
    if not (EBS_USERNAME and EBS_PASSWORD):
        logger.warning('No EBS credentials provided; cannot login via Playwright.')
        return False

    logger.info('===== AUTOMATIC LOGIN ATTEMPT =====')
    logger.info('Username: %s', EBS_USERNAME)

    # Try visiting login URLs
    login_urls = [
        'https://5dang.ebs.co.kr/login',
    ]
    
    for lu in login_urls:
        try:
            logger.info('Navigating to: %s', lu)
            page.goto(lu, wait_until='domcontentloaded', timeout=15000)
            logger.info('Page loaded: %s', page.url)
            page.wait_for_timeout(2000)  # Give page time to render
            
            # Try to find and fill username field
            username_field = None
            for selector in ['input[name="userId"]:visible', 'input[name="username"]:visible', 'input[type="text"]:visible']:
                try:
                    elem = page.query_selector(selector)
                    if elem and elem.is_visible():
                        username_field = selector
                        logger.info('Found username field: %s', selector)
                        break
                except Exception:
                    pass
            
            if not username_field:
                logger.warning('Username field not found on %s, trying next login URL', lu)
                continue
            
            # Try to find and fill password field
            password_field = None
            for selector in ['input[name="password"]:visible', 'input[type="password"]:visible']:
                try:
                    elem = page.query_selector(selector)
                    if elem and elem.is_visible():
                        password_field = selector
                        logger.info('Found password field: %s', selector)
                        break
                except Exception:
                    pass
            
            if not password_field:
                logger.warning('Password field not found on %s, trying next login URL', lu)
                continue
            
            # Fill in credentials
            logger.info('Filling username field...')
            page.fill(username_field, EBS_USERNAME)
            page.wait_for_timeout(500)
            
            logger.info('Filling password field...')
            page.fill(password_field, EBS_PASSWORD)
            page.wait_for_timeout(500)
            
            # Try to find and click login button
            login_button = None
            button_selectors = ['button:has-text("로그인")', 'button[type="submit"]', 'input[type="submit"]']
            
            for selector in button_selectors:
                try:
                    elem = page.query_selector(selector)
                    if elem:
                        login_button = selector
                        logger.info('Found login button: %s', selector)
                        break
                except Exception:
                    pass
            
            if login_button:
                logger.info('Clicking login button...')
                page.click(login_button)
            else:
                logger.info('No login button found, pressing Enter...')
                page.keyboard.press('Enter')
            
            # Wait for login to complete
            logger.info('Waiting for login to complete...')
            page.wait_for_timeout(3000)
            
            # Check if login was successful by verifying URL changed or page loaded
            current_url = page.url
            logger.info('Current URL after login attempt: %s', current_url)
            
            # If we're still on login page, login probably failed
            if 'login' in current_url.lower():
                logger.warning('Still on login page, credentials may be incorrect. Trying next URL...')
                continue
            
            logger.info('===== LOGIN SUCCESSFUL =====')
            return True
            
        except Exception as e:
            logger.error('Error during login attempt for %s: %s', lu, e)
            continue
    
    logger.error('===== LOGIN FAILED - NO VALID LOGIN URL WORKED =====')
    return False
    
    # Final recovery: ensure we're on AUSCHOOL_URL and wait for stable state
    try:
        if AUSCHOOL_URL not in page.url:
            page.goto(AUSCHOOL_URL, wait_until='domcontentloaded', timeout=10000)
    except Exception:
        pass


def main():
    if not is_allowed_weekday_kst():
        logger.info('Today is Sunday in KST -> skipping (Mon-Sat only).')
        return

    saved_audio_files = []
    TARGET_PROD_IDS = ['132558', '187', '191']

    logger.info('Using AUSCHOOL_URL: %s', AUSCHOOL_URL)

    def on_response(response):
        try:
            req = response.request
            if '.m4a' in req.url or 'ebs' in req.url:
                if '.m4a' in req.url and 'end=180' not in req.url and response.status in (200, 206):
                    try:
                        body = response.body()
                    except Exception:
                        return
                    fname = pathlib.Path(req.url.split('?')[0]).name
                    stored_name = f'har_saved_{fname}'
                    saved_audio_files.append((stored_name, body))
                    logger.info('Captured audio response %s', stored_name)
        except Exception:
            pass

    with sync_playwright() as p:
        # support DEBUG_PLAYWRIGHT for headful debugging locally
        if DEBUG_PLAYWRIGHT and DEBUG_PLAYWRIGHT.lower() not in ('0', 'false', 'no'):
            try:
                sm = int(DEBUG_SLOWMO) if DEBUG_SLOWMO else 150
            except Exception:
                sm = 150
            browser = p.chromium.launch(headless=False, slow_mo=sm)
        else:
            browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on('response', on_response)

        # If credentials provided, perform an initial login on the EBS login page
        if EBS_USERNAME and EBS_PASSWORD:
            logger.info('===== LOGIN FLOW START (credentials provided) =====')
            login_success = attempt_login(page)
            logger.info('===== LOGIN FLOW END =====')
            if not login_success:
                logger.warning('Login failed, but continuing to AUSCHOOL_URL anyway')
            try:
                page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass
        else:
            logger.info('No credentials provided - skipping login flow, proceeding directly to AUSCHOOL_URL')

        # Go to target (try load; continue even if non-fatal errors)
        try:
            page.goto(AUSCHOOL_URL, wait_until='domcontentloaded', timeout=20000)
        except Exception:
            logger.warning('Failed to load page directly; continuing to attempt login.')

        # Find program blocks that link to replay pages
        # anchors like /auschool/sub/replay?prodId=...
        anchors = page.query_selector_all('a[href*="/auschool/sub/replay"]')
        replay_hrefs = []
        for a in anchors:
            try:
                href = a.get_attribute('href')
                if href and href not in replay_hrefs:
                    if any(f'prodId={pid}' in href for pid in TARGET_PROD_IDS):
                        replay_hrefs.append(href)
            except Exception:
                continue

        if not replay_hrefs:
            logger.info('No replay links found on page; attempting login flow')
            attempt_login(page)
            # Recover page state after login attempt
            try:
                page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                logger.debug('Page load state wait timed out after login')
            try:
                page.goto(AUSCHOOL_URL, wait_until='networkidle', timeout=20000)
            except Exception:
                logger.debug('Failed to re-navigate to AUSCHOOL_URL')
            # Safely query with exception handling
            try:
                anchors = page.query_selector_all('a[href*="/auschool/sub/replay"]')
                for a in anchors:
                    try:
                        href = a.get_attribute('href')
                        if href and href not in replay_hrefs:
                            if any(f'prodId={pid}' in href for pid in TARGET_PROD_IDS):
                                replay_hrefs.append(href)
                    except Exception:
                        continue
            except Exception as e:
                logger.debug('Failed to query replay links after login: %s', e)

        if not replay_hrefs:
            logger.error('No replay links found after attempts.')
            sys.exit(2)

        # Limit number to download (commonly 3 programs)
        replay_hrefs = replay_hrefs[:6]

        links = []
        # Visit each replay page and collect m4a links from responses and DOM
        for href in replay_hrefs:
            # make absolute URL
            if href.startswith('/'):
                target = requests.compat.urljoin(page.url, href)
            else:
                target = href
            logger.info('Visiting replay page %s', target)
            try:
                # open in same page
                page.goto(target, wait_until='networkidle', timeout=20000)
                # trigger audio playback to force browser fetch of the actual m4a resource
                try:
                    trigger_audio_playback(page)
                    page.wait_for_timeout(6000)
                    try:
                        page.wait_for_response(lambda r: '.m4a' in r.url and r.status in (200, 206), timeout=10000)
                    except Exception:
                        pass
                except Exception:
                    pass
                # collect from responses (audio files may be fetched dynamically)
                # check response objects via network events already bound in outer scope - use DOM checks too
                # audio tags
                for a in page.query_selector_all('a[href]'):
                    try:
                        ah = a.get_attribute('href')
                        if ah and '.m4a' in ah and 'end=180' not in ah and not any(x['url']==requests.compat.urljoin(page.url, ah) for x in links):
                            links.append({'url': requests.compat.urljoin(page.url, ah), 'referer': page.url})
                    except Exception:
                        pass
                for src in page.query_selector_all('audio source, source'):
                    try:
                        s = src.get_attribute('src')
                        if s and '.m4a' in s and 'end=180' not in s and not any(x['url']==requests.compat.urljoin(page.url, s) for x in links):
                            links.append({'url': requests.compat.urljoin(page.url, s), 'referer': page.url})
                    except Exception:
                        pass
                # scripts may contain URLs
                for sc in page.query_selector_all('script'):
                    try:
                        txt = sc.inner_text()
                        if '.m4a' in txt:
                            for m in re.findall(r'https?://[^"\'\s]+\.m4a[^"\s]*', txt):
                                if 'end=180' not in m and not any(x['url']==m for x in links):
                                    links.append({'url': m, 'referer': page.url})
                    except Exception:
                        pass
            except Exception as e:
                logger.error('Failed to open replay %s: %s', target, e)

        if not links:
            logger.error('No .m4a links found on any replay page.')
            sys.exit(2)

        # select up to 3 unique urls
        unique = []
        for item in links:
            if item['url'] not in [u['url'] for u in unique]:
                unique.append(item)
        to_download = unique[:3]

        # prepare requests session with cookies
        sess = requests.Session()
        transfer_cookies_to_requests(context, sess)
        # set user-agent similar to Playwright
        sess.headers.update({'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0 Safari/537.36'})

        with tempfile.TemporaryDirectory() as td:
            tdpath = pathlib.Path(td)

            # Write captured audio bodies into temporary directory
            for name, body in saved_audio_files:
                out_path = tdpath / name
                try:
                    out_path.write_bytes(body)
                except Exception as e:
                    logger.warning('Failed to write captured audio %s: %s', name, e)

            # Filter saved audio files: must be today and time 0700/0720/0740
            today_str = datetime.now(KST).strftime('%Y%m%d')
            target_times = ['070000', '072000', '074000']

            all_candidate_files = list(tdpath.glob('har_saved_*.m4a'))
            filtered_audio = []
            for fpath in all_candidate_files:
                fname = fpath.name
                if today_str in fname:
                    for ttime in target_times:
                        if ttime in fname:
                            filtered_audio.append(fpath)
                            logger.info('Selected audio file: %s', fname)
                            break
                else:
                    logger.info('Skipping audio file (not today): %s', fname)

            downloaded = []
            for fpath in filtered_audio:
                if fpath.exists() and fpath.stat().st_size > 3 * 1024 * 1024:
                    downloaded.append(fpath)
                    logger.info('Selected complete audio file: %s (%d bytes)', fpath.name, fpath.stat().st_size)
                else:
                    logger.info('Discarding incomplete/short audio file: %s', fpath.name)

            if downloaded:
                logger.info('Using %d complete audio files for today (%s)', len(downloaded), today_str)

            for item in to_download:
                if downloaded:
                    break
                url = item['url']
                referer = item.get('referer') or 'https://5dang.ebs.co.kr/'
                fname = url.split('/')[-1].split('?')[0]
                dest = tdpath / fname
                # Define headers for fallback requests
                try:
                    ua = page.evaluate('() => navigator.userAgent')
                except Exception:
                    ua = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0 Safari/537.36'
                headers = {
                    'Referer': referer,
                    'User-Agent': ua,
                    'Range': 'bytes=0-',
                    'Accept-Encoding': 'identity;q=1, *;q=0',
                }

                try:
                    logger.info('Downloading %s via Browser Native Downloader', url)
                    with page.expect_download(timeout=60000) as download_info:
                        page.evaluate('''
                            (args) => {
                                const a = document.createElement('a');
                                a.href = args.url;
                                a.download = args.fname;
                                document.body.appendChild(a);
                                a.click();
                                document.body.removeChild(a);
                            }
                        ''', {'url': url, 'fname': fname})
                    download = download_info.value
                    download.save_as(str(dest))
                    downloaded.append(dest)
                    logger.info('Browser Native Download succeeded for %s', url)
                except Exception as e:
                    logger.warning('Playwright download failed %s: %s; attempting requests fallback', url, e)
                    try:
                        resp = sess.get(url, headers=headers, timeout=60, allow_redirects=True, stream=True)
                        if resp.status_code not in (200, 206):
                            raise Exception(f'{resp.status_code} {resp.reason}')
                        with open(dest, 'wb') as f:
                            for chunk in resp.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        downloaded.append(dest)
                        logger.info('Fallback download succeeded for %s', url)
                    except Exception as e2:
                        logger.error('Download failed %s: %s', url, e2)
                        # Final fallback: attempt in-page fetch using browser context (sends cookies/credentials)
                        try:
                            logger.info('Attempting in-page fetch fallback for %s', url)
                            b64 = page.evaluate(
                                '''(args) => fetch(args.url, {method:'GET', headers: args.headers, credentials:'include'})
                                    .then(r => { if(!r.ok) throw new Error(r.status + ' ' + r.statusText); return r.arrayBuffer(); })
                                    .then(buf => {
                                        const bytes = new Uint8Array(buf);
                                        let binary = '';
                                        const chunk = 0x8000;
                                        for (let i = 0; i < bytes.length; i += chunk) {
                                            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                                        }
                                        return btoa(binary);
                                    })''',
                                {'url': url, 'headers': headers}
                            )
                            import base64
                            content = base64.b64decode(b64)
                            with open(dest, 'wb') as f:
                                f.write(content)
                            downloaded.append(dest)
                            logger.info('In-page fetch succeeded for %s', url)
                        except Exception as e3:
                            logger.error('All download attempts failed for %s: %s', url, e3)

            if not downloaded:
                logger.error('No files downloaded')
                sys.exit(3)

            if SKIP_UPLOAD and SKIP_UPLOAD.lower() not in ('0', 'false', 'no'):
                logger.info('SKIP_UPLOAD is set; skipping upload step')
                return

            GCP_CLIENT_ID = os.environ.get('GCP_CLIENT_ID')
            GCP_CLIENT_SECRET = os.environ.get('GCP_CLIENT_SECRET')
            GCP_REFRESH_TOKEN = os.environ.get('GCP_REFRESH_TOKEN')

            if not (GCP_CLIENT_ID and GCP_CLIENT_SECRET and GCP_REFRESH_TOKEN) or not GDRIVE_FOLDER_ID:
                logger.error('GCP OAuth credentials or GDRIVE_FOLDER_ID not set; skipping upload')
                return

            for fpath in downloaded:
                try:
                    upload_to_drive(fpath, GDRIVE_FOLDER_ID, GCP_CLIENT_ID, GCP_CLIENT_SECRET, GCP_REFRESH_TOKEN)
                except Exception as e:
                    logger.error('Upload failed %s: %s', fpath.name, e)

        context.close()
        browser.close()


if __name__ == '__main__':
    main()
