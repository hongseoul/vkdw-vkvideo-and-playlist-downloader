@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Создаю окружение Python...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Не найден Python 3. Установите его с https://www.python.org/downloads/
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import PySide6, qfluentwidgets, certifi, PIL" 1>nul 2>nul
if errorlevel 1 (
  echo Устанавливаю библиотеки...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
rem python.exe, not pythonw.exe: VPN split-tunnel rules on this machine match python.exe,
rem and VK refuses many videos to the foreign address pythonw.exe would get.
rem The app gets a console of its own and hides it at start (vkdw/console.py);
rem if it fails, the console comes back with the error.
set VKDW_HIDE_CONSOLE=1
start "VKDW" ".venv\Scripts\python.exe" -m vkdw
