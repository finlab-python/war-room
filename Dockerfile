# 公開站：資料庫打包進映像，唯讀；不接 AI。ADD 會解開 tar 並保留 ClickHouse 的相對連結
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY war_room ./war_room
RUN uv pip install --system --no-cache ".[data]"
ADD data/ch.tar /app/data/
ENV WAR_ROOM_DB=/app/data/ch
CMD ["war-room"]
