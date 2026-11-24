@echo off
setlocal
set "APP_JAR=%~dp0STPyramidJava.jar"
if not exist "%APP_JAR%" set "APP_JAR=%~dp0STPyramidJava-0.2.4-windows.jar"
if not exist "%APP_JAR%" (
  echo Put this launcher next to STPyramidJava.jar. 1>&2
  if "%~1"=="" pause
  exit /b 1
)
if not defined STPYRAMID_PORTABLE_RUNTIME if exist "%~dp0runtime\app\desktop_worker.py" set "STPYRAMID_PORTABLE_RUNTIME=%~dp0runtime"
set "JAVA_NAME=java.exe"
if "%~1"=="" set "JAVA_NAME=javaw.exe"
set "APP_JAVA="
if exist "%~dp0jre\bin\%JAVA_NAME%" set "APP_JAVA=%~dp0jre\bin\%JAVA_NAME%"
if not defined APP_JAVA if defined JAVA_HOME if exist "%JAVA_HOME%\bin\%JAVA_NAME%" set "APP_JAVA=%JAVA_HOME%\bin\%JAVA_NAME%"
if not defined APP_JAVA (
  where %JAVA_NAME% >nul 2>nul
  if not errorlevel 1 set "APP_JAVA=%JAVA_NAME%"
)
if not defined APP_JAVA (
  echo Java 17 or newer is required. Use the bundled runtime, set JAVA_HOME or add Java to PATH. 1>&2
  if "%~1"=="" pause
  exit /b 1
)
if "%~1"=="" goto desktop
"%APP_JAVA%" -jar "%APP_JAR%" %*
exit /b %errorlevel%

:desktop
start "" "%APP_JAVA%" -jar "%APP_JAR%"
exit /b %errorlevel%
