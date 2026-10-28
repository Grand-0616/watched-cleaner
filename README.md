# 看完即清 · 飞牛影视已看自动清理（WebUI + Docker）

读取飞牛影视（fnOS / Jellyfin 内核）的「已看完」记录，在 Transmission / qBittorrent 里匹配到对应种子，
打开网页勾选就能删除——不用手动输关键词，避免误删其它做种。

## 原理

```
飞牛影视已看历史            下载器种子
trimmedia.db                Transmission / qBittorrent
item_user_play(watched=1) → 种子 + 每个文件的路径
item_media.path(宿主机路径) ──┬── 按【文件路径】精确匹配
                             ▼
          WebUI 列出「你看过且仍在保种」的种子 → 勾选删除(任务 + 本地文件)
```

- **已看**：`item_user_play` 表里 `watched=1` 的记录，关联 `item_media.path` 得到文件路径。
- **匹配**：已看文件路径与种子文件路径精确比对。宿主机路径与下载器容器内路径的差异由
  `fnnas.path_map` 消除；即使不配映射，也会回退到「作品目录名 + 文件名」的精确组合。
- **删除**：勾选即删除整个种子任务 + 本地数据文件，磁盘释放，所有站点停止保种。

## 目录结构

```
watched-cleaner/
├── app.py                  Flask 后端
├── cleaner.py              核心：已看读取 + 下载器客户端 + 路径匹配
├── templates/index.html    WebUI 前端
├── config/                 运行期配置（挂载进容器，不在 git 里）
│   ├── config.json         首次启动自动生成，账号填这里
│   └── downloaders.json    首次启动自动生成，WebUI 改下载器写这里
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 部署

### 1. 放到 NAS

```bash
cd /vol1/docker
git clone https://github.com/Grand-0616/watched-cleaner.git
cd watched-cleaner
```

### 2. 启动

```bash
docker compose up -d --build
```

首次启动会自动创建 `config/` 并生成默认配置，日志里会看到：

```
[config] 已生成默认配置 /app/config/config.json
```

不需要手动建目录或复制文件。

### 3. 填飞牛影视账号

编辑 `config/config.json` 的 `fnnas.username`，然后重启容器：

```bash
docker compose restart
```

留空也能跑：程序会自动使用数据库里的第一个管理员用户。

### 4. 打开使用

浏览器访问 `http://<NAS地址>:5000`。

- 页面自动列出「你看过且仍在保种」的种子候选，按作品聚合，显示已看集数/总集数。
- 勾选（默认勾选「已全部看完」的）→「删除选中（含本地文件）」→ 二次确认。
- 建议首次先不勾选、只看清单核对匹配结果，确认无误再删。

### 5. 更新项目

```bash
git pull && docker compose up -d --build
```

`config/` 不在 git 里，更新不会影响你已有的配置。

## docker-compose.yml 说明

```yaml
services:
  watched-cleaner:
    build: .
    image: watched-cleaner:latest
    container_name: watched-cleaner
    restart: unless-stopped
    network_mode: host
    volumes:
      # 飞牛影视数据库目录(只读)
      - /usr/local/apps/@appdata/trim.media/database:/fnnas-db:ro
      # 配置目录
      - ./config:/app/config
      # 资源数据盘(与宿主机相同路径)
      - /vol1/1000:/vol1/1000
      - /vol2/1000:/vol2/1000
    environment:
      - TZ=Asia/Shanghai
```

| 项 | 作用 | 什么时候要改 |
|---|---|---|
| `network_mode: host` | 容器与宿主机共享网络，WebUI 直接监听宿主机端口，下载器可用 `127.0.0.1` 访问 | 一般不用改 |
| `/usr/local/apps/@appdata/trim.media/database:/fnnas-db:ro` | 只读挂载飞牛影视数据库 | 数据库不在这个路径时改左边（`find / -name trimmedia.db` 确认） |
| `./config:/app/config` | 挂载配置目录，配置存在宿主机上 | 一般不用改 |
| `/vol1/1000:/vol1/1000`<br>`/vol2/1000:/vol2/1000` | 资源盘按原路径挂进容器，用于「已看但未做种」时直接删文件 | 你的资源盘不是这两个路径时，按实际路径增删 |
| `TZ=Asia/Shanghai` | 容器时区 | 一般不用改 |

> 容器名冲突时改 `container_name`；端口冲突时改 `config/config.json` 里的 `web.port`。

## 配置说明（config/config.json）

首次启动生成的默认配置：

```jsonc
{
  "fnnas": {
    "db_path": "/fnnas-db/trimmedia.db",   // 容器内路径，一般不用改
    "username": "",                        // 飞牛影视登录账号，留空则用第一个管理员
    "path_map": [                          // 宿主机资源根 → 下载器容器内根
      { "host_root": "/vol1/1000/12T", "container_root": "/downloads" }
    ]
  },
  "downloaders": [
    { "type": "transmission", "url": "http://127.0.0.1:9091", "user": "", "pass": "" },
    { "type": "qbittorrent",  "url": "http://127.0.0.1:8090", "user": "", "pass": "" }
  ],
  "web": { "host": "0.0.0.0", "port": 5000, "token": "" }
}
```

- **`path_map`**：可写多条。宿主机 `/vol1/1000/12T` 在下载器里是 `/downloads`，
  `/vol2/1000/4T` 是 `/datas`，就写两条。路径前缀一致时可留空数组 `[]`。
- **`web.token`**：访问口令，设了就要在网页里输入；留空不校验。
- **下载器不用改文件**：启动后点页面右上「下载器设置」，在网页里添加/编辑/启用/删除
  Transmission 或 qBittorrent，保存后自动重连，不用重启容器。设置写在 `config/downloaders.json`。

> 本部署用 `network_mode: host`，下载器地址填 `http://127.0.0.1:端口` 即可命中
> qB 的 localhost 免认证，账号密码留空。

## 数据库挂载

- 容器内 `fnnas.db_path` 固定为 `/fnnas-db/trimmedia.db`。
- **只读挂载**，工具绝不写数据库，只查询 `watched=1`。
- 路径不同就用 `find / -name trimmedia.db` 确认后改 compose 左侧。

## 安全说明

- 只对「文件名 + 作品目录名」都完全一致的种子文件命中，相似剧名、不同集、同名不同作品都不会误匹配。
- 字幕、样本等非视频文件不参与匹配。
- 删除前有二次确认，并标注「已全部看完 / 部分看完」。
