# EBS Auto Download -> Google Drive

자동으로 EBS 오디오(아침 방송)를 다운로드해 Google Drive에 업로드하는 도구입니다.

주요 파일
- `downloader.py`: 메인 스크립트(다운로드 + Drive 업로드)
- `requirements.txt`: 파이썬 의존성
- `.github/workflows/download.yml`: GitHub Actions 워크플로(아래에 설명)

환경 변수 / GitHub Secrets
- `EBS_USERNAME`, `EBS_PASSWORD` : EBS 로그인 정보 (Secrets)
- `GCP_SA_KEY` : 서비스 계정 JSON 전체 내용을 그대로 저장한 값 (Secrets)
- `GDRIVE_FOLDER_ID` : 업로드 대상 Google Drive 폴더 ID (Secrets)
- `AUSCHOOL_URL` : (선택) 다운로드 대상 페이지 URL

간단 설치
```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
```

로컬 실행
```bash
export EBS_USERNAME=you
export EBS_PASSWORD=pass
export GCP_SA_KEY='{"type":...}'
export GDRIVE_FOLDER_ID=folderid
python downloader.py
```

GitHub Actions
- 워크플로는 `.github/workflows/download.yml`에 정의되어 있으며, 기본적으로 매일 KST 08:10(UTC 23:10 전날)에 실행됩니다.
- 실행 시 스크립트 내부에서 KST 기준으로 요일을 검사하여 월요일~토요일에만 동작합니다.
- GitHub 리포지토리에 파일을 올린 뒤, 반드시 `Settings > Secrets and variables > Actions`에서 아래 값을 등록해야 합니다.

GitHub Secrets 등록 방법
1. GitHub 리포지토리에서 `Settings` 클릭
2. 왼쪽 메뉴에서 `Secrets and variables > Actions` 선택
3. `New repository secret` 클릭 후 다음 값을 등록
   - `GCP_SA_KEY` : 서비스 계정 JSON 파일 전체 내용
     - 작은따옴표로 감싸지 말고, JSON 원본 그대로 붙여넣습니다.
   - `GDRIVE_FOLDER_ID` : Google Drive 폴더 ID
   - `EBS_USERNAME` : EBS 로그인 아이디
   - `EBS_PASSWORD` : EBS 로그인 비밀번호
   - (선택) `AUSCHOOL_URL` : 다운로드 페이지 URL을 변경하려면 입력

GitHub에 업로드하기
1. 변경한 파일을 커밋
```bash
git add .
git commit -m "Add GitHub Actions and Drive upload setup"
git push origin main
```
2. GitHub에서 리포지토리로 이동한 뒤 `Actions` 탭에서 워크플로가 정상적으로 등록되었는지 확인합니다.
3. `Actions`에서 `EBS Auto Download` 워크플로를 선택하고 `Run workflow` 버튼을 눌러 수동 실행할 수 있습니다.

수동 실행 확인
- 워크플로가 실행되면 로그에서 `downloader_playwright.py`가 정상 실행되었는지 확인합니다.
- 실행이 완료되면 Google Drive 폴더에 `20260718_070000...`, `20260718_072000...`, `20260718_074000...` 형태의 m4a 파일이 업로드되어야 합니다.

노트
- EBS 로그인 페이지 구조가 변경되었거나 특수한 JS 기반 인증이 필요한 경우, `downloader.py`의 `get_session_login` 함수를 실제 로그인 흐름에 맞추어 조정해야 합니다.
- 서비스 계정으로 Google Drive 업로드를 사용하려면 업로드 대상 폴더를 서비스 계정 이메일로 공유해야 합니다.

대체: gcloud CLI로 서비스 계정/키를 만들려면 (로컬에 gcloud 설치되어 있고 프로젝트가 선택된 경우):
```bash
gcloud iam service-accounts create ebs-downloader --display-name="EBS Downloader"
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ebs-downloader@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/drive.file"
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=ebs-downloader@$PROJECT_ID.iam.gserviceaccount.com
```

마지막으로 `sa-key.json` 내용을 `GCP_SA_KEY` Secrets로 업로드합니다.

테스트 실행
- 로컬에서 먼저 테스트하려면 `GCP_SA_KEY` 환경변수에 JSON 텍스트를 설정하고 `GDRIVE_FOLDER_ID`를 설정한 후 스크립트를 실행하세요.
```bash
export GCP_SA_KEY='{"type":... }'
export GDRIVE_FOLDER_ID="<your-folder-id>"
export EBS_USERNAME="your-ebs-id"
export EBS_PASSWORD="your-ebs-pass"
python downloader_playwright.py
```

