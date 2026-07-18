import os
import sys
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import tempfile
import pathlib
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Config via env
AUSCHOOL_URL = os.environ.get('AUSCHOOL_URL', 'https://5dang.ebs.co.kr/auschool/sub/language?clsfnSystId1=47140032%3E47140033')
EBS_USERNAME = os.environ.get('EBS_USERNAME')
EBS_PASSWORD = os.environ.get('EBS_PASSWORD')
GCP_SA_KEY = os.environ.get('GCP_SA_KEY')
GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID')

# When running in GitHub Actions, the system time is UTC. We want to operate on KST weekdays.
KST = timezone(timedelta(hours=9))


def is_allowed_weekday_kst():
    now_kst = datetime.now(KST)
    # weekday(): Monday=0 ... Sunday=6. We want Mon-Sat (0-5)
    return now_kst.weekday() <= 5


def get_session_login(session: requests.Session) -> requests.Session:
    # Try to access page directly. If redirected to login, and credentials provided, attempt generic login.
    try:
        resp = session.get(AUSCHOOL_URL, allow_redirects=True, timeout=30)
        if resp.status_code == 200 and b'.m4a' in resp.content:
            return session
    except Exception:
        pass

    if not (EBS_USERNAME and EBS_PASSWORD):
        logger.warning('No EBS credentials provided; cannot attempt login.')
        return session

    # Generic login attempt: many sites use /login or /user/login; try common patterns.
    logger.info('Attempting generic EBS login (may need adjustment).')
    login_variants = [
        'https://user.ebs.co.kr/login.do',
        'https://user.ebs.co.kr/login',
        'https://member.ebs.co.kr/login',
    ]
    payloads = [
        {'userId': EBS_USERNAME, 'password': EBS_PASSWORD},
        {'username': EBS_USERNAME, 'password': EBS_PASSWORD},
        {'id': EBS_USERNAME, 'pw': EBS_PASSWORD},
    ]

    for url in login_variants:
        for payload in payloads:
            try:
                r = session.post(url, data=payload, timeout=20)
                logger.info('Tried login %s -> %s', url, r.status_code)
            except Exception:
                continue

    return session


def find_m4a_links(html: bytes, base_url: str):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    # direct <a href="...m4a">
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '.m4a' in href:
            links.add(requests.compat.urljoin(base_url, href))
    # <audio> tags
    for audio in soup.find_all('audio'):
        for src in audio.find_all('source'):
            if src.get('src') and '.m4a' in src.get('src'):
                links.add(requests.compat.urljoin(base_url, src.get('src')))

    return sorted(links)


def download_file(session: requests.Session, url: str, dest: pathlib.Path):
    logger.info('Downloading %s', url)
    with session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


def upload_to_drive(filepath: pathlib.Path, folder_id: str, credentials_json: dict):
    scopes = ['https://www.googleapis.com/auth/drive.file']
    credentials = service_account.Credentials.from_service_account_info(credentials_json, scopes=scopes)
    service = build('drive', 'v3', credentials=credentials, cache_discovery=False)

    file_metadata = {
        'name': filepath.name,
        'parents': [folder_id] if folder_id else None,
    }
    media = MediaFileUpload(str(filepath), mimetype='audio/m4a')
    logger.info('Uploading %s to Drive folder %s', filepath.name, folder_id)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    logger.info('Uploaded file id: %s', file.get('id'))
    return file.get('id')


def main():
    if not is_allowed_weekday_kst():
        logger.info('Today is Sunday in KST -> skipping (Mon-Sat only).')
        return

    session = requests.Session()
    session = get_session_login(session)

    resp = session.get(AUSCHOOL_URL, timeout=30)
    if resp.status_code != 200:
        logger.error('Failed to fetch auschool page: %s', resp.status_code)
        sys.exit(1)

    links = find_m4a_links(resp.content, AUSCHOOL_URL)
    if not links:
        logger.warning('No .m4a links found on page; build may require custom login flow.')
        sys.exit(2)

    # prefer latest up to 3 files
    to_download = links[:3]

    with tempfile.TemporaryDirectory() as td:
        tdpath = pathlib.Path(td)
        downloaded = []
        for url in to_download:
            fname = url.split('/')[-1].split('?')[0]
            dest = tdpath / fname
            try:
                download_file(session, url, dest)
                downloaded.append(dest)
            except Exception as e:
                logger.error('Download failed for %s: %s', url, e)

        if not downloaded:
            logger.error('No files downloaded.')
            sys.exit(3)

        if not GCP_SA_KEY or not GDRIVE_FOLDER_ID:
            logger.error('GCP_SA_KEY or GDRIVE_FOLDER_ID not set in env; cannot upload.')
            sys.exit(4)

        try:
            credentials_json = json.loads(GCP_SA_KEY)
        except Exception as e:
            logger.error('Failed to parse GCP_SA_KEY JSON: %s', e)
            sys.exit(5)

        for fpath in downloaded:
            try:
                upload_to_drive(fpath, GDRIVE_FOLDER_ID, credentials_json)
            except Exception as e:
                logger.error('Upload failed for %s: %s', fpath.name, e)


if __name__ == '__main__':
    main()
