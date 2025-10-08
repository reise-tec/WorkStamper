# 1. ベースとなる環境を選択 (Python 3.11のスリム版)
FROM python:3.11-slim

# 2. コンテナ内の作業場所を作成
WORKDIR /app

# 3. 必要なライブラリの一覧をコピー
COPY requirements.txt .

# 4. ライブラリをインストール
RUN pip install --no-cache-dir -r requirements.txt

# 5. アプリの全コードを作業場所にコピー
COPY . .

# 6. アプリが通信に使うポート番号を指定
EXPOSE 8080

# 7. コンテナが起動したときに実行するコマンド
CMD ["gunicorn", "app:flask_app", "--bind", "0.0.0.0:8080", "--workers", "1"]