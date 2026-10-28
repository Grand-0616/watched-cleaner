# 看完即清 v2 —— 容器镜像
FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码
COPY app.py cleaner.py ./
COPY templates ./templates

# 只拷贝模板：首次启动时程序会自动用它在 /app/config/ 下生成真实配置。
# docker-compose 会把宿主机 ./config 目录挂载到 /app/config，
# 所以配置存在宿主机上，重建镜像、更新代码都不会覆盖它。
COPY config.example.json downloaders.example.json ./

EXPOSE 5000

# 容器内无交互终端：下载器/影视密码请填在 config/config.json；
# 飞牛影视数据库通过 docker-compose 只读挂载到 /fnnas-db。
CMD ["python", "app.py"]
