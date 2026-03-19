@echo off
cd /d "%~dp0"
echo === 市場・競合分析 AI アシスタント ===
echo.

REM .envファイルがなければ案内
if not exist ".env" (
  echo [エラー] .envファイルが見つかりません。
  echo .env.exampleをコピーして.envを作成し、APIキーを設定してください。
  pause
  exit /b 1
)

REM 仮想環境の作成とパッケージインストール
if not exist "venv" (
  echo 仮想環境を作成中...
  python -m venv venv
  call venv\Scripts\activate.bat
  echo パッケージをインストール中...
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate.bat
)

echo.
echo アプリを起動しています...
echo ブラウザで http://localhost:5000 を開いてください
echo.
python app.py
pause
