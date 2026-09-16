@echo off
setlocal
cd /d "%~dp0"
set "BASE=intelligent-data-discovery-html-only-2026-09-15.zip"
set "EXPECTED=59895244a6888785e56b60a3757f865cd63b47b37d83d128f00e12c4d40a2602"
if exist "%BASE%" goto existing
for %%P in (01 02 03) do if not exist "%BASE%.part%%P" (
  echo Missing: %BASE%.part%%P
  goto failure
)
if exist "%BASE%.partial" (
  echo A partial file exists. Inspect or move it aside before retrying.
  goto failure
)
echo Combining 3 parts, about 4 GB...
copy /b "%BASE%.part01"+"%BASE%.part02"+"%BASE%.part03" "%BASE%.partial"
if errorlevel 1 goto failure
echo Verifying SHA256...
powershell -NoProfile -Command "if ((Get-FileHash -LiteralPath ($env:BASE + '.partial') -Algorithm SHA256).Hash -ne $env:EXPECTED) { exit 1 }"
if errorlevel 1 (
  echo Checksum failed. Download the parts again; no ZIP was published.
  goto failure
)
move "%BASE%.partial" "%BASE%" >nul
if errorlevel 1 goto failure
goto success
:existing
powershell -NoProfile -Command "if ((Get-FileHash -LiteralPath $env:BASE -Algorithm SHA256).Hash -ne $env:EXPECTED) { exit 1 }"
if errorlevel 1 (
  echo Existing ZIP checksum differs; move it aside first.
  goto failure
)
:success
echo Verified. Extract the ZIP, then open index.html in Chrome or Edge.
pause
exit /b 0
:failure
echo Not completed. Original downloaded parts have been kept.
pause
exit /b 1
