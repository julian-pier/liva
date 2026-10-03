@echo off
setlocal
set "PI_BASE_URL=http://192.0.2.1:5000"
set "CURL_EXE=%SystemRoot%\System32\curl.exe"
set "TIMEOUT_EXE=%SystemRoot%\System32\timeout.exe"
set "URL=%PI_BASE_URL%/api/pc-status/offline"

for /L %%I in (1,1,10) do (
    "%CURL_EXE%" --fail --silent --show-error --max-time 4 --connect-timeout 2 --retry 0 --request POST "%URL%" >nul 2>&1 && exit /b 0
    "%TIMEOUT_EXE%" /t 2 /nobreak >nul
)

endlocal
exit /b 0
