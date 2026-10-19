@echo off
rem NInfer-DFlash. Set NINFER_DIR and NINFER_MODEL to your build and artifact; the port and
rem model id here must match "base" and "send_model" in router.json.
setlocal
set "NINFER_DIR=C:\path\to\windows-ninfer-V100X2"
set "NINFER_EXE=%NINFER_DIR%\build-windows\apps\ninfer-serve.exe"
set "NINFER_MODEL=%NINFER_DIR%\models\your-model.ninfer"
if not exist "%NINFER_EXE%" (
    echo Edit NINFER_EXE in this file.
    exit /b 1
)
if not exist "%NINFER_MODEL%" (
    echo Edit NINFER_MODEL in this file.
    exit /b 1
)
"%NINFER_EXE%" "%NINFER_MODEL%" ^
    --host 127.0.0.1 --port 8081 --model-id coder390 ^
    --tp 2 --devices 0,1 --max-concurrency 1 ^
    --pending-timeout-ms 600000 ^
    --max-context 98304 --kv-capacity 98304 --kv-dtype int8 ^
    --prefill-chunk 256 --spec dflash --draft-tokens 7
