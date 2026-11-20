@echo off
rem Builds naming_lab.dll (32-bit) and installs the mod into
rem build\port-trace\mods\naming-lab, beside the tracing build the lab runs on
rem (port\build-port-trace.cmd). Only the tracing build has a plugin loader
rem (port\hal\lab_plugins.cpp); the shipped build\port never loads a plugin.
setlocal
rem Visual Studio through vswhere, any edition: port\tools\vs_env.cmd sets up
rem MSVC x86, cmake and NINJA_EXE, the same as the port's own build scripts.
call "%~dp0..\..\tools\vs_env.cmd"
if errorlevel 1 exit /b 1
set "ROOT=%~dp0..\..\.."
cmake -S "%~dp0." -B "%ROOT%\build\naming_lab" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_MAKE_PROGRAM="%NINJA_EXE%" || exit /b 1
"%NINJA_EXE%" -C "%ROOT%\build\naming_lab" || exit /b 1
if not exist "%ROOT%\build\port-trace\walk_window.exe" (
  echo build\port-trace\walk_window.exe is missing: run port\build-port-trace.cmd first.
  exit /b 1
)
if not exist "%ROOT%\build\port-trace\mods\naming-lab\plugin" mkdir "%ROOT%\build\port-trace\mods\naming-lab\plugin"
copy /y "%~dp0mod.json" "%ROOT%\build\port-trace\mods\naming-lab\" || exit /b 1
copy /y "%ROOT%\build\naming_lab\naming_lab.dll" "%ROOT%\build\port-trace\mods\naming-lab\plugin\" || exit /b 1
echo installed build\port-trace\mods\naming-lab
