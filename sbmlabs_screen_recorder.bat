@echo off
rem SBM Labs Screen Recorder -- double-click to open the recorder (no console window)
python -c "import customtkinter" 2>nul || (
    echo Installing the UI toolkit ^(first run only^)...
    python -m pip install -q customtkinter
)
start "" pythonw "%~dp0recorder.py"
