@echo off
setlocal
cd /d "%~dp0"

set "SEARCH_PYTHON=%LOCALAPPDATA%\PCFullTextSearch\runtime\Scripts\python.exe"

if not exist "%SEARCH_PYTHON%" (
    call "%~dp0setup_search.cmd"
    if errorlevel 1 exit /b 1
)

if "%~1"=="" (
    if not exist "%~dp0config.json" (
        if not exist "%~dp0config.example.json" (
            echo config.example.json was not found.
            pause
            exit /b 1
        )
        copy /y "%~dp0config.example.json" "%~dp0config.json" >nul
        echo Created config.json. The default search target is your Documents folder.
        echo Change it from the Management screen if needed.
    )
    "%SEARCH_PYTHON%" -X utf8 "%~dp0app.py" serve --open-browser
) else (
    "%SEARCH_PYTHON%" -X utf8 "%~dp0app.py" --config "%~1" serve --open-browser
)
if errorlevel 1 (
    echo.
    echo The search app could not start.
    echo The message above shows whether another configuration is already using the port.
    pause
    exit /b 1
)
