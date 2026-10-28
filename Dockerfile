# 看完即清 v2 —— 容器镜像
FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码
COPY app.py cleaner.py ./
# 只拷贝模板作为兜底：正常运行时 docker-compose 会把宿主机上你自己的
# config.json 挂载到 /app/config.json 覆盖掉它，所以重建镜像不会动你的配置。
COPY config.example.json ./config.json
COPY templates ./templates

EXPOSE 5000

# 容器内无交互终端：下载器/影视密码请填在 config.json；
# 飞牛影视数据库通过 docker-compose 只读挂载到 /fnnas-db。
CMD ["python", "app.py"]
