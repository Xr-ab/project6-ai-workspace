# Phase 11a：后端一个镜像喂四个服务（app / worker / mcp-server / init-data）。
# 不写 CMD/ENTRYPOINT：各服务跑什么由 docker-compose.yml 的 command 给（spec §2.1）。
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖先装、代码后拷：改一行业码不该重下 ~90 个包（层缓存，Step 6 有针）。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 非 root。uid/gid 写死 10001：命名卷的属主要靠它，写死才不会每次重建换号。
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home app

# 两个数据目录必须在 COPY 之前建好并 chown：
# compose 的命名卷首次挂载时把**镜像里这个路径的内容与属主**搬进卷。
# 属主若不是 app，容器里以 uid 10001 跑的进程就写不进自己的上传目录。
RUN mkdir -p /app/data/uploads /app/data/model_cache && chown -R app:app /app/data

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

USER app
