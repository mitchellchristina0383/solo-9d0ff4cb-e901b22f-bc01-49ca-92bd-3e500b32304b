FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools

ENV LAYOUT_PATH=/data/layout \
    PORT=8000
EXPOSE 8000

# 首次启动时若数据卷为空则生成样例 OCI layout，然后启动发布页。
CMD python tools/make_sample_layout.py "$LAYOUT_PATH" --if-empty \
    && exec python -m app.server --layout "$LAYOUT_PATH" --host 0.0.0.0 --port "$PORT"
