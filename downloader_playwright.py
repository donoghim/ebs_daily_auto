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
GCP_SA_KEY = os.environ.get('GCP_SA_KEY')
GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID')

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
    # Try common login pages or detect login form
    # If credentials not provided, skip
    if not (EBS_USERNAME and EBS_PASSWORD):
        logger.warning('No EBS credentials provided; cannot login via Playwright.')
        return

    # Try visiting a known login URL first
    login_urls = [
        'https://user.ebs.co.kr/login.do',
        'https://user.ebs.co.kr/login',
        'https://member.ebs.co.kr/login',
    ]
    for lu in login_urls:
        try:
            page.goto(lu, wait_until='domcontentloaded', timeout=15000)
        except Exception:
            continue

        # try common selectors
        username_selectors = ['input[name="userId"]', 'input[name="username"]', 'input[type="text"]', 'input[id*=id]']
        password_selectors = ['input[name="password"]', 'input[type="password"]', 'input[id*=pw]']

        uname = None
        pwd = None
        for s in username_selectors:
            try:
                if page.query_selector(s):
                    uname = s
                    break
            except Exception:
                continue
        for s in password_selectors:
            try:
                if page.query_selector(s):
                    pwd = s
                    break
            except Exception:
                continue

        if uname and pwd:
            try:
                page.fill(uname, EBS_USERNAME)
                page.fill(pwd, EBS_PASSWORD)
                # try pressing Enter
                page.keyboard.press('Enter')
                page.wait_for_timeout(3000)
                logger.info('Submitted login form on %s', lu)
                # Recover to AUSCHOOL after successful attempt
                try:
                    page.goto(AUSCHOOL_URL, wait_until='domcontentloaded', timeout=15000)
                except Exception:
                    pass
                return
            except Exception:
                continue

    # Fallback: navigate to AUSCHOOL and look for login modal/button
    try:
        page.goto(AUSCHOOL_URL, wait_until='domcontentloaded', timeout=15000)
        # if there is a button/link with '로그인'
        btn = page.query_selector('text=로그인')
        if btn:
            btn.click()
            page.wait_for_timeout(2000)
            # attempt generic fills
            for s in ['input[type="text"]', 'input[type="email"]', 'input[name*=id]']:
                el = page.query_selector(s)
                if el:
                    el.fill(EBS_USERNAME)
                    break
            for s in ['input[type="password"]']:
                el = page.query_selector(s)
                if el:
                    el.fill(EBS_PASSWORD)
                    break
            page.keyboard.press('Enter')
            page.wait_for_timeout(3000)
            logger.info('Attempted modal login')
    except Exception:
        logger.debug('Fallback login attempt failed')
    
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

    logger.info('Using AUSCHOOL_URL: %s', AUSCHOOL_URL)

    def on_response(response):
        try:
            req = response.request
            if '.m4a' in req.url or 'ebs' in req.url:
                if '.m4a' in req.url and response.status in (200, 206):
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
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on('response', on_response)

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
                        if ah and '.m4a' in ah and not any(x['url']==requests.compat.urljoin(page.url, ah) for x in links):
                            links.append({'url': requests.compat.urljoin(page.url, ah), 'referer': page.url})
                    except Exception:
                        pass
                for src in page.query_selector_all('audio source, source'):
                    try:
                        s = src.get_attribute('src')
                        if s and '.m4a' in s and not any(x['url']==requests.compat.urljoin(page.url, s) for x in links):
                            links.append({'url': requests.compat.urljoin(page.url, s), 'referer': page.url})
                    except Exception:
                        pass
                # scripts may contain URLs
                for sc in page.query_selector_all('script'):
                    try:
                        txt = sc.inner_text()
                        if '.m4a' in txt:
                            for m in re.findall(r'https?://[^"\'\s]+\.m4a[^"\s]*', txt):
                                if not any(x['url']==m for x in links):
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

            downloaded = list(filtered_audio)
            if downloaded:
                logger.info('Using %d audio files for today (%s)', len(downloaded), today_str)

            for item in to_download:
                if downloaded:
                    break
                url = item['url']
                referer = item.get('referer') or 'https://5dang.ebs.co.kr/'
                fname = url.split('/')[-1].split('?')[0]
                dest = tdpath / fname
                try:
                    logger.info('Downloading %s via Playwright request', url)
                    # use Playwright's request API (shares browser context/cookies)
                    ua = page.evaluate('() => navigator.userAgent')
                    headers = {
                        'Referer': referer,
                        'User-Agent': ua,
                        'Range': 'bytes=0-',
                        'Accept-Encoding': 'identity;q=1, *;q=0',
                    }
                    resp = page.request.get(url, headers=headers, timeout=60000)
                    if resp.status not in (200, 206):
                        raise Exception(f'{resp.status} {resp.status_text}')
                    content = resp.body()
                    with open(dest, 'wb') as f:
                        f.write(content)
                    downloaded.append(dest)
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

            if not GCP_SA_KEY or not GDRIVE_FOLDER_ID:
                logger.error('GCP_SA_KEY or GDRIVE_FOLDER_ID not set; skipping upload');
                return

            try:
                credentials_json = json.loads(GCP_SA_KEY)
            except Exception as e:
                logger.error('Invalid GCP_SA_KEY JSON: %s', e)
                sys.exit(5)

            for fpath in downloaded:
                try:
                    upload_to_drive(fpath, GDRIVE_FOLDER_ID, credentials_json)
                except Exception as e:
                    logger.error('Upload failed %s: %s', fpath.name, e)

        context.close()
        browser.close()


if __name__ == '__main__':
    main()
