@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Mode comes from config\runtime.json: mock, simulation, or production.
rem Activate once so the shared launcher can handle shutdown directly.
set "launcher_conda="
set "launcher_exit="
if defined CONDA_EXE for %%I in ("%CONDA_EXE%") do if exist "%%~dpI..\condabin\conda.bat" set "launcher_conda=%%~dpI..\condabin\conda.bat"
if defined launcher_conda goto conda_found
for /f "delims=" %%I in ('where conda.bat 2^>nul') do (
    set "launcher_conda=%%I"
    goto conda_found
)
for %%I in (
    "%USERPROFILE%\miniconda3\condabin\conda.bat"
    "%USERPROFILE%\anaconda3\condabin\conda.bat"
    "%USERPROFILE%\miniforge3\condabin\conda.bat"
    "%LOCALAPPDATA%\miniconda3\condabin\conda.bat"
    "%LOCALAPPDATA%\anaconda3\condabin\conda.bat"
    "%LOCALAPPDATA%\miniforge3\condabin\conda.bat"
    "%LOCALAPPDATA%\Programs\Miniforge3\condabin\conda.bat"
    "%ProgramData%\miniconda3\condabin\conda.bat"
    "%ProgramData%\anaconda3\condabin\conda.bat"
    "%ProgramData%\miniforge3\condabin\conda.bat"
) do if exist "%%~I" (
    set "launcher_conda=%%~I"
    goto conda_found
)
echo Conda was not found. Run this file from Anaconda / Miniforge Prompt,
echo or set CONDA_EXE to the full path of your conda.exe.
goto failed

:conda_found
call "%launcher_conda%" activate tss
if errorlevel 1 goto failed
pushd "%~dp0"
if errorlevel 1 goto failed
python -X utf8 -u "%~dp0src\service_launcher.py" %*
set "launcher_exit=%ERRORLEVEL%"
popd
if not "%launcher_exit%"=="0" goto failed
exit /b 0

:failed
if not defined launcher_exit set "launcher_exit=1"
echo Launcher stopped with an error. Review the message above.
pause
exit /b %launcher_exit%
