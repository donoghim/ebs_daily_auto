@echo off
REM Local runner for downloader_playwright.py (headful debugging)
SETLOCAL
REM Use default AUSCHOOL_URL or override with your own
SET DEBUG_PLAYWRIGHT=1
SET DEBUG_SLOWMO=150
REM Example: test a single replay page (change as needed)
SET AUSCHOOL_URL=https://5dang.ebs.co.kr/auschool/sub/language?clsfnSystId1=47140032%%3E47140033
REM Prevent uploads during local debugging
SET SKIP_UPLOAD=1

REM Prompt for EBS credentials (leave blank to skip)
SET /P EBS_USERNAME=EBS Username (leave blank to skip): 
IF "%EBS_USERNAME%"=="" (
    ECHO No credentials provided - login will be skipped
    SET EBS_PASSWORD=
) ELSE (
    SET /P EBS_PASSWORD=EBS Password: 
    ECHO.
    ECHO Starting with credentials for user: %EBS_USERNAME%
)

REM Activate venv if present
IF EXIST .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

python downloader_playwright.py
ENDLOCAL
