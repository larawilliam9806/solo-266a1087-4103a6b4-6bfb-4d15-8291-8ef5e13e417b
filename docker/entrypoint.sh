#!/bin/sh
# 单 worker + 多线程：SQLite 写事务（BEGIN IMMEDIATE）天然串行化，
# 单进程避免跨进程锁竞争，线程内由 WAL + busy_timeout 保证一致性。
set -e

mkdir -p "$(dirname "$DATABASE_PATH")"

# 启动时先把旧库修复跑完（逐批提交，可安全中断/续跑），
# 这样对外服务时汇总与详情已经可开放；仍保留按请求续跑作为兜底。
python - <<'PY'
import logging

from app import create_app
from app import services

logging.basicConfig(level=logging.INFO)
app = create_app()
with app.app_context():
    services.run_upgrade()
    logging.info("旧库数据修复检查完成")
PY

exec gunicorn --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 8 \
    --timeout 300 \
    --graceful-timeout 30 \
    --access-logfile - \
    --error-logfile - \
    "app:create_app()"
