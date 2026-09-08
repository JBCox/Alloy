@echo off
rem Collaborative Model Builder window without the chat app.
rem Usage: scripts\model-builder.bat [workflow_dir] [screenplay.json]   (the second argument binds scripted mock seats)
pushd "%~dp0.."
python -m gui.builder_view %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
