@echo off
REM =====================================================================
REM svnWeb environment check (Windows)
REM
REM Run BEFORE init_config.py. Checks:
REM   1. Python 3.8+ and venv module
REM   2. SVN toolchain: svn / svnadmin / svnlook (required), svnserve (optional)
REM   3. svn:// port listening (custom port supported, default 3690)
REM   4. Web port 37245 free
REM   5. config.json exists; repo_root exists / writable / repo count
REM   6. Python deps (Flask / Pygments / waitress)
REM
REM Usage: check_env.bat [svn_port]     (default 3690)
REM Exit code: 0 = ready, 1 = blocking problems found
REM Note: output is English-only on purpose (cmd.exe codepage safety).
REM =====================================================================
setlocal
set "SVN_PORT=%~1"
if "%SVN_PORT%"=="" set "SVN_PORT=3690"
set "WEB_PORT=37245"
set /a FAILCOUNT=0
set /a WARNCOUNT=0
set /a OKCOUNT=0
set "APPDIR=%~dp0..\python"
set "PY="

echo ==============================================
echo  svnWeb environment check  (svn=%SVN_PORT%  web=%WEB_PORT%)
echo ==============================================
echo.

echo == 1/5 Python ==
set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if "%PYEXE%"=="" where py >nul 2>nul && set "PYEXE=py"
if "%PYEXE%"=="" (
    echo   [FAIL] Python 3 not found in PATH. Install Python 3.8+ first.
    set /a FAILCOUNT+=1
    goto after_python
)
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info>=(3,8) and sys.version_info[0]==3 else 1)" >nul 2>nul
if errorlevel 1 (
    echo   [FAIL] Python 3.8+ required. Current:
    %PYEXE% -c "import sys; print('          '+sys.version.split()[0])"
    set /a FAILCOUNT+=1
    goto after_python
)
for /f "tokens=*" %%v in ('%PYEXE% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo   [OK]   Python %PYVER% found
set /a OKCOUNT+=1
%PYEXE% -m venv --help >nul 2>nul
if errorlevel 1 (
    echo   [WARN] venv module not available. Reinstall Python with venv support.
    set /a WARNCOUNT+=1
) else (
    echo   [OK]   venv module available
    set /a OKCOUNT+=1
)
:after_python

echo.
echo == 2/5 SVN toolchain ==
set "SVNFOUND=1"
for %%t in (svn svnadmin svnlook) do (
    where %%t >nul 2>nul
    if errorlevel 1 (
        echo   [FAIL] %%t not found in PATH. Install Subversion ^(add TortoiseSVN bin to PATH^).
        set /a FAILCOUNT+=1
        set "SVNFOUND=0"
    ) else (
        echo   [OK]   %%t found
        set /a OKCOUNT+=1
    )
)
where svnserve >nul 2>nul
if errorlevel 1 (
    echo   [INFO] svnserve not found - Web UI still works via file:// access,
    echo          but svn:// checkout/commit will not be available.
) else (
    echo   [OK]   svnserve found
    set /a OKCOUNT+=1
)

echo.
echo == 3/5 Ports ==
netstat -an -p tcp | findstr /C:":%SVN_PORT% " | findstr /C:"LISTENING" >nul 2>nul
if errorlevel 1 (
    echo   [INFO] Port %SVN_PORT% ^(svn://^) not listening. If you use a CUSTOM svn port,
    echo          start svnserve with --listen-port ^<port^> and set config.json
    echo          "svn_base": "svn://<ip>:<port>"  - or leave it empty for auto-detect.
) else (
    echo   [OK]   Port %SVN_PORT% ^(svn://^) is listening
    set /a OKCOUNT+=1
)
netstat -an -p tcp | findstr /C:":%WEB_PORT% " | findstr /C:"LISTENING" >nul 2>nul
if errorlevel 1 (
    echo   [OK]   Port %WEB_PORT% ^(web^) is free
    set /a OKCOUNT+=1
) else (
    echo   [WARN] Port %WEB_PORT% is already in use - choose another waitress port
    echo          if it is not this program.
    set /a WARNCOUNT+=1
)

echo.
echo == 4/5 Config and repo root ==
if not exist "%APPDIR%\config.json" (
    echo   [INFO] config.json not found yet. After checks pass, run:
    echo          python init_config.py    ^(inside the python folder^)
    goto after_config
)
echo   [OK]   config.json found
set /a OKCOUNT+=1
if "%PYEXE%"=="" goto after_config
> "%TEMP%\svnweb_repocheck.txt" 2>nul (
    %PYEXE% -c "import json,io,sys; d=json.load(open(r'%APPDIR%\config.json',encoding='utf-8')); print(d.get('repo_root',''))"
)
set "REPO_ROOT="
set /p REPO_ROOT=<"%TEMP%\svnweb_repocheck.txt"
del "%TEMP%\svnweb_repocheck.txt" >nul 2>nul
if "%REPO_ROOT%"=="" goto after_config
echo   [INFO] repo_root = %REPO_ROOT%
if not exist "%REPO_ROOT%\" (
    echo   [WARN] repo_root does not exist - init_config.py can create it.
    set /a WARNCOUNT+=1
    goto after_config
)
echo %REPO_ROOT%>"%TEMP%\svnweb_rw.tmp"
copy /y nul "%REPO_ROOT%\.svnweb_write_test" >nul 2>nul
if exist "%REPO_ROOT%\.svnweb_write_test" (
    del "%REPO_ROOT%\.svnweb_write_test" >nul 2>nul
    echo   [OK]   repo_root is writable
    set /a OKCOUNT+=1
) else (
    echo   [FAIL] repo_root is NOT writable - creating repos will fail.
    set /a FAILCOUNT+=1
)
del "%TEMP%\svnweb_rw.tmp" >nul 2>nul
%PYEXE% -c "import os; r=r'%REPO_ROOT%'; repos=[x for x in os.listdir(r) if os.path.isdir(os.path.join(r,x,'db'))]; print('   [INFO] Found', len(repos), 'existing SVN repo(s) - they will appear on the homepage.') if repos else print('   [INFO] No repos under repo_root yet - create via web UI or svnadmin create.')"
:after_config

echo.
echo == 5/5 Python dependencies ==
if "%PYEXE%"=="" goto after_deps
set "DEPPY="
if exist "%APPDIR%\venv\Scripts\python.exe" set "DEPPY=%APPDIR%\venv\Scripts\python.exe"
if "%DEPPY%"=="" set "DEPPY=%PYEXE%"
if exist "%APPDIR%\venv\Scripts\python.exe" (
    echo   [INFO] Using venv: %APPDIR%\venv
) else (
    echo   [INFO] No venv found - testing system python. Recommended:
    echo          python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
)
%DEPPY% -c "import flask, pygments, waitress" >nul 2>nul
if errorlevel 1 (
    echo   [INFO] Dependencies not installed yet. Run inside python folder:
    echo          pip install -r requirements.txt
) else (
    echo   [OK]   Flask / Pygments / waitress installed
    set /a OKCOUNT+=1
)
:after_deps

echo.
echo == Summary ==
echo   OK: %OKCOUNT%   WARN: %WARNCOUNT%   FAIL: %FAILCOUNT%
if %FAILCOUNT% gtr 0 (
    echo   Fix the FAIL items above, then run init_config.py.
    endlocal & exit /b 1
)
echo   Environment is ready. Next: run init_config.py inside the python folder.
endlocal & exit /b 0
