@echo off
setlocal
pushd "%~dp0" || exit /b 1
echo Testing local networking and two NEW Edge profiles. Allow up to 3 minutes.
echo Existing browser profiles and sign-ins will not be modified.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "scripts\diagnose_edge_connection.py"
) else (
    py -3 "scripts\diagnose_edge_connection.py"
)
echo.
if errorlevel 1 (
    echo Diagnostic failed to run. Please share the error above.
) else (
    echo Diagnostic complete. Two reports open in Notepad.
    echo Please share BOTH reports. PASS on this computer is not proof for another Citrix session.
)
popd
pause
