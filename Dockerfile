# 看完即清 v2 —— 容器镜像
FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝代码与配置
COPY app.py cleaner.py ./
COPY config.json ./
COPY templates ./templates

EXPOSE 5000

# 容器内无交互终端：下载器/影视密码请填在 config.json；
# 飞牛影视数据库通过 docker-compose 只读挂载到 /fnnas-db。
CMD ["python", "app.py"]
