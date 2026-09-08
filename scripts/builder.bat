@echo off
rem Collaborative Model Builder CLI. Runs from Alloy's own folder so config.yaml and the builder package are found.
rem Usage: scripts\builder.bat <verb> ...      (try: scripts\builder.bat --help)
pushd "%~dp0.."
python -m builder %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
