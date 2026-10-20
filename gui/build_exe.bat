@echo off
REM Builds dist\ZotifyGUI.exe (single file, no console window).
REM Run from the repo root in a Python environment that has this fork,
REM pywebview and pyinstaller installed:
REM   pip install . pywebview pyinstaller
REM FFmpeg is not bundled: the app finds it on PATH or from a winget install.

pyinstaller --noconfirm --clean --onefile --windowed ^
  --name ZotifyGUI ^
  --icon gui\icon.ico ^
  --add-data "gui\ui;ui" ^
  --paths gui ^
  --collect-all zotify ^
  --collect-all librespot ^
  --collect-all music_tag ^
  --collect-all webview ^
  --hidden-import clr ^
  gui\zotify_gui.py
