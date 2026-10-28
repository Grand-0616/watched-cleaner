# 看完即清 v2 · 飞牛影视已看自动清理（WebUI + Docker）

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
│   ├── config.json         首次启动自动生成
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

### 2. 填飞牛影视账号

编辑 `docker-compose.yml`，把账号填进环境变量（只需填这一次）：

```yaml
    environment:
      - TZ=Asia/Shanghai
      - FNNAS_USERNAME=你的飞牛影视用户名
```

留空也能跑：程序会自动使用数据库里的第一个管理员用户。

### 3. 启动

```bash
docker compose up -d --build
```

首次启动会自动创建 `config/` 并生成默认配置，日志里会看到：

```
[config] 已生成默认配置 /app/config/config.json
```

不需要手动建目录或复制文件。

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

## 配置说明

绝大多数情况只需填 `FNNAS_USERNAME`。其它可选项：

| 位置 | 项 | 说明 |
|---|---|---|
| 环境变量 | `FNNAS_USERNAME` | 飞牛影视登录账号，留空则用第一个管理员 |
| 环境变量 | `FNNAS_DB_PATH` | 数据库路径，容器内固定 `/fnnas-db/trimmedia.db` |
| 环境变量 | `WEB_PORT` | WebUI 端口，默认 5000 |
| 环境变量 | `WEB_TOKEN` | 访问口令，设了就要在网页里输入 |
| `config/config.json` | `fnnas.path_map` | 宿主机资源根 → 下载器容器内根，可写多条 |
| `config/config.json` | `web.token` | 同上，环境变量优先 |

`path_map` 示例（宿主机 `/vol1/1000/12T` 在下载器里是 `/downloads`）：

```json
"path_map": [
  { "host_root": "/vol1/1000/12T", "container_root": "/downloads" },
  { "host_root": "/vol2/1000/4T",  "container_root": "/datas" }
]
```

**下载器不用改文件**：启动后点页面右上「下载器设置」，在网页里添加/编辑/启用/删除
Transmission 或 qBittorrent，保存后自动重连，不用重启容器。设置写在 `config/downloaders.json`。

> 本部署用 `network_mode: host`，下载器地址填 `http://127.0.0.1:端口` 即可命中
> qB 的 localhost 免认证，账号密码留空。

## 数据库挂载

```yaml
- /usr/local/apps/@appdata/trim.media/database:/fnnas-db:ro
```

- 容器内 `fnnas.db_path` 固定为 `/fnnas-db/trimmedia.db`。
- **只读挂载**，工具绝不写数据库，只查询 `watched=1`。
- 路径不同就用 `find / -name trimmedia.db` 确认后改 compose 左侧。

## 安全说明

- 只对「文件名 + 作品目录名」都完全一致的种子文件命中，相似剧名、不同集、同名不同作品都不会误匹配。
- 字幕、样本等非视频文件不参与匹配。
- 删除前有二次确认，并标注「已全部看完 / 部分看完」。
