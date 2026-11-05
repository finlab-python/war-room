#!/bin/sh
# 部署公開站（Cloud Run）。上傳原始碼會把連結變成資料夾，所以資料庫先打成 tar。
set -e
cd "$(dirname "$0")"
COPYFILE_DISABLE=1 tar -cf data/ch.tar -C data ch  # 不要夾帶 macOS 的 ._ 檔
gcloud run deploy war-room --source . --region asia-east1 --project fdata-299302 --allow-unauthenticated \
  --memory 1Gi --cpu 1 --min-instances 1 --max-instances 3 --concurrency 40 --quiet
