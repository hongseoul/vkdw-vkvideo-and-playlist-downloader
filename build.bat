@echo off
setlocal
cd /d "%~dp0"
rem Builds dist\VKDW: VKDW.exe with its _internal folder. It runs without Python.
rem Messages are in English: cmd shows UTF-8 text in a batch file as garbage.
if not exist ".venv\Scripts\python.exe" (
  echo Run run.bat first: it creates the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements-build.txt
if errorlevel 1 (
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\vkdw.spec
if errorlevel 1 (
  pause
  exit /b 1
)
echo Done: dist\VKDW\VKDW.exe
