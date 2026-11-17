@echo off
setlocal
set "APP_JAR=%~dp0STPyramidJava-0.2.4-windows.jar"
if not exist "%APP_JAR%" (
  echo Put this launcher next to STPyramidJava-0.2.4-windows.jar.
  pause
  exit /b 1
)
if not defined STPYRAMID_PORTABLE_RUNTIME if exist "%~dp0runtime\app\desktop_worker.py" set "STPYRAMID_PORTABLE_RUNTIME=%~dp0runtime"
if exist "%~dp0jre\bin\javaw.exe" (
  start "" "%~dp0jre\bin\javaw.exe" -jar "%APP_JAR%"
  exit /b
)
if defined JAVA_HOME if exist "%JAVA_HOME%\bin\javaw.exe" (
  start "" "%JAVA_HOME%\bin\javaw.exe" -jar "%APP_JAR%"
  exit /b
)
where javaw.exe >nul 2>nul
if not errorlevel 1 (
  start "" javaw.exe -jar "%APP_JAR%"
  exit /b
)
echo Java 17 or newer is required. Set JAVA_HOME or add Java to PATH.
pause
exit /b 1
