@echo off
rem Collaborative Model Builder CLI (python -m builder), on the same settings file as the app's Model Builder view.
if not defined ALLOY_BUILDER_CONFIG set "ALLOY_BUILDER_CONFIG=%~dp0sessions\builder.json"
pushd "%~dp0"
python -m builder %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
