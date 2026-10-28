# 看完即清 v2 · 飞牛影视已看自动清理（WebUI + Docker）

> 用途：自动读取**飞牛影视（fnOS / Jellyfin 内核）的「已看完」历史**，在 Transmission / qBittorrent  
> 里匹配到对应种子，打开网页勾选即可删除——**不用手动输关键词**，避免匹配异常影响其他种子做种。
>
> 你的环境已适配：飞牛影视直挂资源文件夹、一份数据挂多站 tracker（一个种子多个 tracker）。

---

## 1. 原理（数据流）

```
飞牛影视已看历史                下载器种子
trimmedia.db                     Transmission / qBittorrent
item_user_play(watched=1)  →    种子 + 每个文件的路径
item_media.path(宿主机路径)  ───────┬─────────── 按【文件路径】精确匹配
                                  ▼
              WebUI 展示「你看过且仍在保种的种子」，勾选 → 删除任务+本地文件
                              （停止全部站点保种，磁盘释放，影视条目同步消失）
```

- **已看来源**：飞牛影视数据库 `trimmedia.db` 中 `item_user_play` 表 `watched=1` 的记录，  
  关联 `item_media.path` 得到你已看完的媒体文件路径（宿主机路径，如 `/vol1/1000/Media/电视剧/…/xxx.mkv`）。
- **匹配方式**：把已看文件路径与下载器种子文件路径精确比对。  
  路径前缀差异（宿主机 `/vol1/1000/Media` vs 容器 `/downloads`）通过 `fnnas.path_map` 消除；  
  即使不配映射，也会回退到「作品目录名 + 文件名」的精确组合，不会误删相似/无关种子。
- **删除粒度**：勾选某个种子即删除**整个种子任务 + 本地数据文件**。只列出「其中确有你看过的文件」的种子，  
  并显示已看集数/总集数、「全部看完/部分」标记，由你确认后删除。

---

## 2. 目录结构

```
watched-cleaner/
├── app.py                  Flask 后端（/api/watched 已看清单、/api/downloaders 下载器管理）
├── cleaner.py              核心：飞牛影视已看读取 + 下载器客户端 + 文件路径匹配
├── templates/index.html    WebUI 前端（自动加载清单、勾选删除、下载器设置）
├── config.json             配置模板（fnnas 数据库/用户/路径映射 + 默认下载器地址）
├── downloaders.json        下载器配置(WebUI 可写，优先于 config.json)
├── Dockerfile
├── docker-compose.yml      （含飞牛影视数据库只读挂载 + downloaders.json 可写挂载）
├── requirements.txt
├── .gitignore
└── README.md               本文件
```

---

## 3. 部署步骤（在你的 NAS 上）

### 3.1 把项目放上 NAS

把 `watched_cleaner_webui_v2/` 整个目录放到 NAS 的一个目录（例如 `/vol1/docker/watched-cleaner`）。

### 3.2 首次：确认 `downloaders.json`

目录里已带一个空 `downloaders.json`（`{"downloaders": []}`）。为空时工具自动使用 `config.json`  
里的默认下载器；之后你在 WebUI 里增删改下载器会写到这个文件。  
若 docker compose 启动报「downloaders.json 不存在」，先在该目录执行 `touch downloaders.json`。

### 3.2 编辑 `config.json`（关键！）

```jsonc
{
  "fnnas": {
    "db_path": "/fnnas-db/trimmedia.db",   // 容器内路径，对应下方挂载；一般不用改
    "username": "<飞牛影视登录账号>",       // 必填：你自己的飞牛影视用户名
    "path_map": [                          // 宿主机资源根 → 下载器容器内根（用于路径精确匹配）
      { "host_root": "/vol1/1000/Media", "container_root": "/downloads" }
    ]
  },
  "downloaders": [
    { "type": "transmission", "url": "http://127.0.0.1:9091", "user": "", "pass": "" },
    { "type": "qbittorrent",  "url": "http://127.0.0.1:8080", "user": "", "pass": "" }
  ],
  "web": { "host": "0.0.0.0", "port": 5000, "token": "" }
}
```

- **`<飞牛影视登录账号>`**：填你自己的飞牛影视用户名（用于在 `user` 表定位已看记录）。  
  留空时会回退到数据库里第一个管理员用户，但建议显式填写。
- **下载器地址**：本部署使用 `network_mode: host`，容器与宿主机共享网络，WebUI 直接监听宿主机 5000 端口，  
  因此下载器地址写 `http://127.0.0.1:端口`，qB 会命中它的 **localhost 免认证**（`bypass_local_auth`），  
  账号、密码、API 密钥都留空即可连上。  
  若改回默认 bridge 网络，则要写宿主机局域网 IP（如 `192.168.1.100`），不能写 `127.0.0.1`。
- **qBittorrent 三种认证方式**（按优先级）：① 提供 `api_key` → 用 `X-API-Key` 头（qB 4.6+ WebUI 生成的密钥，如 `qbt_xxx`）；② 提供账号密码 → 走 `/api/v2/auth/login`；③ 都留空 → 假定 qB 已开 localhost 免认证，以 `127.0.0.1` 访问直接放行（**本部署采用这种**）。
- **下载器在 WebUI 里改**：启动后点页面右上「下载器设置」，即可在网页里添加/编辑/启用/删除  
  Transmission 或 qBittorrent（地址、账号、密码、API密钥），保存后自动重连，**不用改文件、不用重启容器**。  
  `config.json` 里的 downloaders 只是首次默认；WebUI 保存后以 `downloaders.json` 为准。
- **端口**：Transmission 默认 `9091`，qBittorrent 默认 `8080`。若你的映射端口不同，改成实际端口。
- **`fnnas.path_map`**：`host_root` 填飞牛影视已看路径的根（你的资源根，通常是 `/vol1/1000/Media`），  
  `container_root` 填下载器容器内下载根（Transmission 通常是 `/downloads`，qB 看你挂载）。  
  如果下载器返回的路径前缀与宿主机一致（host 网络直挂），可留空数组 `[]`，匹配会自动回退到目录+文件名。
- **`web.token`**：建议设一个访问口令（访问网页时要输入），防止局域网内别人乱删。留空则不校验。

### 3.3 启动

```bash
cd /vol1/docker/watched-cleaner
docker compose up -d --build
```

### 3.4 打开使用

浏览器访问 `http://<NAS宿主机IP>:5000`。

- 若设了 `web.token`，先输入访问口令。
- 页面自动读取飞牛影视已看，列出「你看过且仍在保种」的种子候选。
- 勾选（默认勾选「已全部看完」项），点「删除选中（含本地文件）」→ 二次确认 → 删除。
- 建议删除后到各 PT 站确认种子已停止做种（一份数据多站保种，删除即全部停止）。

> 首次建议先**不勾选**、仅查看清单核对匹配结果；确认无误后再删除。

---

## 4. 数据库挂载说明

`docker-compose.yml` 已把飞牛影视数据库目录只读挂进容器：

```yaml
- /usr/local/apps/@appdata/trim.media/database:/fnnas-db:ro
```

- 容器内 `fnnas.db_path` 固定为 `/fnnas-db/trimmedia.db`。
- **只读挂载**：工具绝不会写数据库，只查询 `watched=1` 的记录。
- 若你的飞牛影视数据库路径不同（用 `find / -name trimmedia.db` 确认），改 compose 的左侧路径即可。

---

## 5. 已看判断与安全

- **已看**：飞牛影视里被标记为看完（`item_user_play.watched=1`）的记录。只看了一半不会列为本工具候选。
- **不误删**：
  - 只对「文件名 + 作品目录名」都完全一致的种子文件命中；相似剧名、不同集、同名不同作品都不会误匹配。
  - 非视频文件（字幕、样品等）不参与匹配。
  - 删除前前端二次确认，且「已全部看完 / 部分看完」有明确标识。
- **删除即释放**：删除种子任务 + 本地数据文件，磁盘空间释放，飞牛影视媒体库对应条目同步消失，所有站点保种停止。

---

## 6. 隐私与提交前检查

本仓库不包含任何真实账号、密码、内网 IP 或私有路径，`config.json` 是可提交的配置模板。  
在你把它部署到自己的环境后，下列文件会**开始包含私有信息**，提交到 Git 前请逐项确认：

| 文件                      | 可能包含                                             | 处理方式                         |
| ----------------------- | ------------------------------------------------ | ---------------------------- |
| `config.json`           | `fnnas.username`（影视账号）、`web.token`（访问口令）、下载器账号密码 | 只提交占位版本；本地改动不要提交             |
| `downloaders.json`      | 在 WebUI 里填写的下载器地址、账号、密码、qB API 密钥（**明文**）        | 提交前恢复为 `{"downloaders": []}` |
| `docker-compose.yml`    | 你自己的卷路径、盘符                                       | 改成本机路径后建议不提交，或保留通用示例         |
| `*.db` / `trimmedia.db` | 完整观影历史                                           | 已被 `.gitignore` 排除           |

`.gitignore` 已排除 `__pycache__/`、`*.pyc`、`*.db`、`.env`、日志与编辑器目录。

自查命令（在项目根目录执行）：

```bash
# 查找非空的账号/口令/密码字段，以及任何 IPv4 地址
grep -rniE '(username|token|pass|password|api_key)"?\s*:\s*"[^"]+"|[0-9]{1,3}(\.[0-9]{1,3}){3}' .
```

输出为空即可放心推送。

---

## 7. 本地自测（可选，在电脑上跑）

```bash
pip install flask requests
python app.py     # 直接用本地 config.json 启动，浏览器打开 http://127.0.0.1:5000
```

数据库路径不存在时接口会返回明确报错，不影响前端页面调试。
