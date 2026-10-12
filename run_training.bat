@echo off
cd /d D:\golden-trinity
echo Running LSTM v4 training...
echo Output will appear below. Errors will be saved to training_error.txt
echo.
venv\Scripts\python.exe train_lstm_v4.py 2> training_error.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo *** PYTHON CRASHED — error saved to training_error.txt ***
    echo.
    type training_error.txt
) else (
    echo.
    echo Training finished successfully.
)
pause
