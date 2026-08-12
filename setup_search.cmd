@echo off
setlocal
cd /d "%~dp0"

set "SEARCH_HOME=%LOCALAPPDATA%\PCFullTextSearch"
set "SEARCH_VENV=%SEARCH_HOME%\runtime"
set "SEARCH_PYTHON=%SEARCH_VENV%\Scripts\python.exe"

echo PC full-text search setup
echo Runtime: %SEARCH_VENV%

if not exist "%SEARCH_PYTHON%" (
    where py >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Python 3.11 was not found.
        echo Install Python 3.11, then run this file again.
        pause
        exit /b 1
    )
    echo Creating the private Python environment...
    py -3.11 -m venv "%SEARCH_VENV%"
    if errorlevel 1 (
        echo Failed to create the Python environment.
        pause
        exit /b 1
    )
)

echo Installing required modules...
"%SEARCH_PYTHON%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo Module installation failed. Check the internet connection and retry.
    pause
    exit /b 1
)

echo.
echo Setup completed. Run start_search.cmd to open the search screen.
exit /b 0
