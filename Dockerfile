FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# 国内服务器直连 files.pythonhosted.org 常超时，改用腾讯云 PyPI 镜像并放宽超时
RUN pip install --no-cache-dir -i https://mirrors.tencentyun.com/pypi/simple/ --timeout 60 -r requirements.txt

COPY apps ./apps
COPY tests ./tests

ENV PYTHONUNBUFFERED=1 \
    NOVELFORGE_HOME=/app \
    NOVELFORGE_DATA=/app/data \
    NOVELFORGE_HOST=0.0.0.0 \
    NOVELFORGE_PORT=8000

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
