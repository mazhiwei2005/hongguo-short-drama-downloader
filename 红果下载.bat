@echo off
setlocal enabledelayedexpansion
title 红果短剧下载器

cd /d "%~dp0"
rem ===== 保存位置选择(首次弹窗, 之后回车沿用) =====
set "SAVE_CFG=%~dp0下载位置.config"
set "SAVE_DIR="
if exist "%SAVE_CFG%" set /p SAVE_DIR=<"%SAVE_CFG%"
if defined SAVE_DIR (
    echo [位置] 当前保存位置: %SAVE_DIR%
    set /p CHANGE=  [回车]用这个位置 [输入新路径]换位置: 
    if not "!CHANGE!"=="" set "SAVE_DIR=!CHANGE!"
)
if not defined SAVE_DIR (
    echo [位置] 首次使用, 请选择下载保存位置...
    for /f "delims=" %%i in ('powershell.exe -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object System.Windows.Forms.FolderBrowserDialog; $f.Description='选择红果短剧下载保存位置'; if($f.ShowDialog() -eq 'OK'){ $f.SelectedPath }; [System.Windows.Forms.Application]::Exit()"') do set "SAVE_DIR=%%i"
    if not defined SAVE_DIR set "SAVE_DIR=%~dp0downloads"
)
> "%SAVE_CFG%" echo %SAVE_DIR%
echo [位置] 已保存: %SAVE_DIR%
echo.


rem 优先用 python (anaconda 等), 找不到再退回 py -3
where python >nul 2>nul
if %errorlevel%==0 (
    set PY=python
) else (
    set PY=py -3
)

rem ===== 拖拽模式: 把链接拖到这个bat上直接下载 =====
if not "%~1"=="" (
    echo [OK] 检测到拖入内容, 自动识别下载...
    %PY% hgapp_dl.py %* --out "%SAVE_DIR%"
    echo.
    pause
    exit /b
)

:MENU
cls
echo.
echo  ============================================
echo    红果短剧 全能下载器（App通道版）
echo    突破网页3集限制，全集1080P
echo  ============================================
echo    用法：把链接直接拖到这个bat上也能下载！
echo  ============================================
echo    [1] 粘贴链接下载（可多个，空格/逗号分隔）
echo    [2] 按名称搜索选剧下载
echo    [3] 输入sid下载（可多个，逗号分隔依次下）
echo    [4] 选集下载（sid + 起止集数）
echo    [5] 补下失败集（输入sid）
echo    [6] 测试单集（输入sid + 集数）
echo    [Q] 退出
echo  ============================================
set /p CHOICE=  请选择：

if "%CHOICE%"=="1" (
    set /p INPUT=  粘贴链接（多个用空格或逗号分隔）：
    %PY% hgapp_dl.py --url "!INPUT!" --out "%SAVE_DIR%"
    goto :DONE
)
if "%CHOICE%"=="2" (
    set /p KW=  输入剧名关键词：
    %PY% hgapp_dl.py --name "!KW!" --out "%SAVE_DIR%"
    goto :DONE
)
if "%CHOICE%"=="3" (
    set /p SID=  输入series_id，多个用逗号分隔：
    %PY% hgapp_dl.py --sid "!SID!" --out "%SAVE_DIR%"
    goto :DONE
)
if "%CHOICE%"=="4" (
    set /p SID=  输入series_id：
    set /p S=  起始集（回车默认1）：
    set /p E=  结束集（回车默认全集）：
    if "!S!"=="" set S=1
    if "!E!"=="" set E=99999
    %PY% hgapp_dl.py --sid !SID! --start !S! --end !E! --out "%SAVE_DIR%"
    goto :DONE
)
if "%CHOICE%"=="5" (
    set /p SID=  输入series_id：
    %PY% hgapp_dl.py --sid !SID! --retry --out "%SAVE_DIR%"
    goto :DONE
)
if "%CHOICE%"=="6" (
    set /p SID=  输入series_id：
    set /p N=  测试第几集（回车默认1）：
    if "!N!"=="" set N=1
    %PY% hgapp_dl.py --sid !SID! --test --start !N! --out "%SAVE_DIR%"
    goto :DONE
)
if /i "%CHOICE%"=="Q" exit /b

goto :MENU

:DONE
echo.
echo  完成！按任意键返回菜单...
pause >nul
goto :MENU
