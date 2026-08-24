@echo off
echo ============================================
echo   PongDu  ( gui.py )  --^>  exe build
echo ============================================
echo.

echo [0/5] Checking Python...
py --version
if errorlevel 1 (
    echo.
    echo [!] Python not found. Install it first:
    echo       winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)
echo.

echo [1/5] Installing PyQt5...
py -m pip install PyQt5
if errorlevel 1 goto :pipfail
echo.

echo [2/5] Installing aiohttp ^(latest wheel for this Python^)...
REM chzzkpy 2.2.0 pins aiohttp==3.12.13, which has no prebuilt wheel for
REM Python 3.13+. Installing aiohttp unpinned first grabs a wheel that
REM matches the current interpreter, avoiding a source build that would
REM need MSVC. chzzkpy is then installed with --no-deps so its exact pin
REM does not drag 3.12.13 back in.
py -m pip install aiohttp
if errorlevel 1 goto :pipfail
echo.

echo [3/5] Installing chzzkpy 2.2.0 and its runtime deps...
py -m pip install --no-deps chzzkpy==2.2.0
if errorlevel 1 goto :pipfail
REM ahttp-client is pinned: chzzkpy 2.2.0 targets the 1.x API and 2.0.0
REM is a breaking release. The rest float to whatever has wheels.
py -m pip install "ahttp-client==1.0.4" pydantic
if errorlevel 1 goto :pipfail
echo.

echo [4/5] Installing / updating PyInstaller...
py -m pip install --upgrade pyinstaller
if errorlevel 1 goto :pipfail
echo.

echo [4.5/5] Verifying imports...
py -c "import chzzkpy; from chzzkpy import Client, UserPermission; from chzzkpy.authorization import AccessToken; from chzzkpy.client import UserClient; import PyQt5; print('imports OK')"
if errorlevel 1 (
    echo.
    echo [!] Import check failed. The installed package versions are not
    echo     compatible with each other. Copy the error above and ask about it.
    pause
    exit /b 1
)
echo.

echo [5/5] Building exe... ^(may take a few minutes^)
py -m PyInstaller --onefile --noconsole --name PongDu --icon=pongdu.ico --add-data "pongdu.ico;." --add-data "connection.wav;." --add-data "opt_conf;opt_conf" --collect-all chzzkpy --collect-all ahttp_client gui.py
if errorlevel 1 (
    echo.
    echo [!] Build failed. Copy the red error above and ask about it.
    pause
    exit /b 1
)

echo.
echo [6/6] Generating version.json for the release...
REM Reads VERSION from gui.py, hashes dist\PongDu.exe, and writes dist\version.json.
REM Upload BOTH dist\PongDu.exe and dist\version.json as assets on the GitHub release,
REM and tag the release with the same version string - the launcher's auto-updater
REM fetches version.json from /releases/latest/download/ and verifies the sha256.
py make_version.py
if errorlevel 1 (
    echo.
    echo [!] Could not generate version.json. The exe is still fine - you can
    echo     write dist\version.json by hand if you need to publish a release.
)

echo.
echo ============================================
echo   DONE!  Run  dist\PongDu.exe
echo   ^(share that single file - others just double-click^)
echo.
echo   To publish an update, create a GitHub release
echo   tagged with the VERSION above and attach BOTH:
echo       dist\PongDu.exe
echo       dist\version.json     ^(edit "notes" first^)
echo ============================================
pause
exit /b 0

:pipfail
echo.
echo [!] pip failed. See the error above.
pause
exit /b 1
