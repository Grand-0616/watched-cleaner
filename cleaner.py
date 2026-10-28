#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleaner.py —— 看完即清 v2 核心逻辑
===================================
v2 与 v1 的关键变化：
  1. 数据源：不再手动输关键词，改为【自动读取飞牛影视(fnOS/Jellyfin 内核)
     的已看历史】，从 trimmedia.db 的 item_user_play(watched=1) 读出你已看完的
     媒体文件路径。
  2. 匹配：用【文件路径】精确匹配下载器(Transmission/qBittorrent)里的种子文件，
     而不是作品名前缀模糊匹配。路径前缀(宿主机 /vol1/... vs 容器 /downloads/...)
     差异通过 config.path_map 消除；未配置时回退到 (作品目录名, 文件名) 精确组合，
     避免误删其他种子影响做种。
  3. 删除：删除整个种子任务 + 本地数据文件；一份数据挂多站 tracker 的种子，
     删任务即同时停止所有站点做种、释放磁盘、飞牛影视媒体库条目同步消失。
"""

import os
import re
import json
import shutil
import sqlite3
import urllib.parse

import requests

VIDEO_EXTS = {".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".webm"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 运行期配置目录：docker-compose 把它挂载出来，不在 git 里，
# 所以 git pull 更新代码、重建镜像都不会覆盖你已部署好的配置。
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
# WebUI 可写的下载器配置(独立于只读的 config.json)，便于在网页里增删改下载器
DL_OVERRIDE = os.path.join(CONFIG_DIR, "downloaders.json")
# 首次启动写入 config/config.json 的默认配置（全部占位，账号/口令留空）
DEFAULT_CONFIG = {
    "fnnas": {
        "db_path": "/fnnas-db/trimmedia.db",
        "username": "",
        "path_map": [
            {"host_root": "/vol1/1000/Media", "container_root": "/downloads"}
        ],
    },
    "downloaders": [
        {"type": "transmission", "url": "http://127.0.0.1:9091", "user": "", "pass": ""},
        {"type": "qbittorrent", "url": "http://127.0.0.1:8080", "user": "", "pass": ""},
    ],
    "web": {"host": "0.0.0.0", "port": 5000, "token": ""},
}


def ensure_config():
    """首次启动自动准备 config/ 下的配置文件，免去手动 cp。

    - config/ 不存在 -> 创建
    - config/config.json 不存在 -> 写入内置默认配置 DEFAULT_CONFIG
    - config/downloaders.json 不存在 -> 写入 {"downloaders": []}
    - 兼容旧布局：根目录若还留着 config.json / downloaders.json，自动迁移进来

    返回实际使用的 config.json 路径。
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)

    # 旧布局迁移(旧版本把配置放在项目根目录)
    for old_name, new_path in (
        ("config.json", CONFIG_PATH),
        ("downloaders.json", DL_OVERRIDE),
    ):
        old_path = os.path.join(BASE_DIR, old_name)
        if os.path.isfile(old_path) and not os.path.exists(new_path):
            try:
                shutil.move(old_path, new_path)
                print(f"[config] 已把旧配置 {old_name} 迁移到 config/{old_name}")
            except Exception as e:
                print(f"[config] 迁移 {old_name} 失败: {e}")

    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[config] 已生成默认配置 {CONFIG_PATH}")

    if not os.path.exists(DL_OVERRIDE):
        with open(DL_OVERRIDE, "w", encoding="utf-8") as f:
            json.dump({"downloaders": []}, f, ensure_ascii=False, indent=2)
        print(f"[config] 已生成 {DL_OVERRIDE}")

    return CONFIG_PATH


# 允许用环境变量覆盖配置，方便在 docker-compose.yml 里直接写，
# 不用进容器改文件。留空或未设置时以 config/config.json 为准。
ENV_OVERRIDES = {
    "FNNAS_USERNAME": ("fnnas", "username", str),
    "FNNAS_DB_PATH": ("fnnas", "db_path", str),
    "WEB_PORT": ("web", "port", int),
    "WEB_TOKEN": ("web", "token", str),
}


def apply_env_overrides(cfg):
    """用环境变量覆盖配置项（优先级高于 config/config.json）。"""
    for env_key, (section, key, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is None or raw.strip() == "":
            continue
        try:
            cfg.setdefault(section, {})[key] = cast(raw.strip())
            print(f"[config] 环境变量 {env_key} 覆盖 {section}.{key}")
        except (TypeError, ValueError) as e:
            print(f"[config] 环境变量 {env_key} 无效({raw!r})，已忽略: {e}")
    return cfg


# 规范化种子名 -> 作品身份(主名+季)，用于聚合同作品的不同分辨率/版本
_SEASON_RE = re.compile(r"\.?S(\d{1,2})(?:\s*E\d{1,3})?\b", re.I)
_TOKEN_RE = re.compile(
    r"\.(?:2160p|4k|1080p|720p|480p|UHD|HDR10|HDR|DV|DolbyVision|SDR|HDRVivid|"
    r"WEB|DL|WEB-DL|WEBRip|BLURAY|BluRay|REMUX|HDTV|DVDRip|HDRip|BDRip|AMZN|NETFLIX|iTunes|TX|"
    r"H265|H264|HEVC|AVC|x265|x264|10bit|8bit|Hi10P|"
    r"DDP|DDP5|DDP7|AC3|AAC|TrueHD|DTS|DTSHD|EAC3|FLAC|Atmos|"
    r"60FPS|HFR|MultiAudio|2Audios|DualAudio|Dual?Audio|Complete|CEE|CHS|CHT|"
    r"WS|FIXED|REPACK|PROPER|UNCUT|EXTENDED|IMAX|1080|2160|"
    r"ADWeb|HHWEB|AilMWeb|UBWEB|QHstudIo|RUNE)\b",
    flags=re.I)


def work_key(name: str) -> str:
    """提取作品身份键：保留中文/英文主名 + 季号，去除分辨率/编码/来源/发布组/年份等。"""
    s = (name or "").strip()
    season = ""
    m = _SEASON_RE.search(s)
    if m:
        season = "s" + m.group(1)
    s = re.sub(r"\.?S\d{1,3}(?:\s*E\d{1,3})?", "", s, flags=re.I)  # 移除 SxxEyy 片段
    s = re.sub(r"-[A-Za-z0-9]{2,}", ".", s, flags=re.I)  # 任意位置的发布组/站点 token -> 点
    s = _TOKEN_RE.sub(".", s)          # 分辨率/编码/来源/音轨 token -> 点
    s = re.sub(r"\.\d{4}\b", "", s)    # 年份
    s = re.sub(r"\.\d+\b", "", s)      # 残留数字片段(如 5.1、60FPS 等)
    s = re.sub(r"\.+", ".", s).strip(".")
    key = re.sub(r"[^0-9a-z\u4e00-\u9fa5.]", "", s.lower())   # 仅保留中英文+数字+点
    key = re.sub(r"\.+", ".", key).strip(".")
    if season:
        key = (key + "." + season).strip(".")
    return key


def is_video(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in VIDEO_EXTS


def load_downloaders(config):
    """下载器配置：优先 read downloaders.json(WebUI 可写)，否则用 config.json 默认。"""
    dl = [dict(d) for d in config.get("downloaders", [])]
    if os.path.exists(DL_OVERRIDE):
        try:
            with open(DL_OVERRIDE, encoding="utf-8") as f:
                data = json.load(f)
            override = data.get("downloaders")
            if isinstance(override, list) and override:
                dl = [dict(d) for d in override]
        except Exception:
            pass
    return dl


def save_downloaders(dl):
    """把下载器列表写入 config/downloaders.json(WebUI 调用)。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(DL_OVERRIDE, "w", encoding="utf-8") as f:
        json.dump({"downloaders": dl}, f, ensure_ascii=False, indent=2)


def fmt_size(b):
    b = b or 0
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}PB"


# ============================================================
# 飞牛影视已看历史读取 (SQLite 只读)
# ============================================================
class FnnasWatched:
    """
    读取 /usr/local/apps/@appdata/trim.media/database/trimmedia.db
    已看 = item_user_play.watched=1，关联 item_media.path 得到媒体文件路径。
    """
    def __init__(self, cfg):
        self.db_path = cfg.get("db_path", "")     # 容器内路径(挂载后)，如 /fnnas-db/trimmedia.db
        self.username = cfg.get("username", "")   # 飞牛影视登录账号，请在 config.json 的 fnnas.username 填写
        self.path_map = cfg.get("path_map", [])   # [{host_root, container_root}]

    def _connect(self):
        if not self.db_path:
            raise RuntimeError("未配置 fnnas.db_path(飞牛影视数据库容器内路径)")
        if not os.path.exists(self.db_path):
            raise RuntimeError(f"飞牛影视数据库不存在: {self.db_path}")
        # 只读挂载 + WAL 库：immutable=1 绕过 -shm/-wal 协调，直接只读主库(适用于只读 bind mount)
        return sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)

    def _user_guid(self, conn):
        row = None
        if self.username:
            cur = conn.execute("select guid from user where username=?", (self.username,))
            row = cur.fetchone()
        if not row:
            # 允许回退到任一非模板、非删除用户
            cur = conn.execute(
                "select guid from user where username not like '%_deleted_%' "
                "and username!='user-template' and is_admin=1 limit 1")
            row = cur.fetchone()
        if not row:
            raise RuntimeError(
                f"找不到影视用户: {self.username or '(未配置 fnnas.username)'}")
        return row[0]

    def get_watched(self):
        """返回已看文件记录列表 [{title, type, path, time(ms)}]，按观看时间倒序。"""
        conn = self._connect()
        try:
            uid = self._user_guid(conn)
            rows = conn.execute(
                "select i.title, i.type, m.path, p.create_time "
                "from item_user_play p "
                "left join item i on p.item_guid=i.guid "
                "left join item_media m on p.media_guid=m.guid "
                "where p.watched=1 and p.user_guid=? "
                "order by p.create_time desc", (uid,)
            ).fetchall()
            out = []
            for title, typ, path, ct in rows:
                if not path:
                    continue
                out.append({"title": title or "", "type": typ or "",
                            "path": path or "", "time": ct or 0})
            return out
        finally:
            conn.close()


# ============================================================
# 路径匹配
# ============================================================
def _norm_slash(p: str) -> str:
    return (p or "").replace("\\", "/")


def _strip_root(p: str, roots) -> str:
    """尝试去掉第一个匹配的根前缀，返回相对路径；剥不掉则原样返回。"""
    p = _norm_slash(p).rstrip("/")
    best = p
    for r in roots:
        r = _norm_slash(r).rstrip("/")
        if not r:
            continue
        if p == r:
            return ""
        if p.startswith(r + "/"):
            cand = p[len(r) + 1:]
            if len(cand) < len(best):
                best = cand
    return best


def watched_match(watched_path: str, dl_file: str, known_roots) -> bool:
    """判断下载器文件是否命中某已看文件。双保险：
      1) 去除公共根前缀后的相对路径完全相等(需配置 path_map 才精确到根)。
      2) 回退：作品目录名 + 文件名 组合相等(不依赖根前缀，天然精确)。
    """
    if not watched_path or not dl_file:
        return False
    wp, df = _norm_slash(watched_path), _norm_slash(dl_file)
    if not is_video(df) and not is_video(wp):
        # 至少一方是视频才参与匹配
        if not (is_video(df) or is_video(wp)):
            return False
    # 1) 相对路径精确
    if _strip_root(wp, known_roots) == _strip_root(df, known_roots) and _strip_root(wp, known_roots):
        return True
    # 2) 作品目录名 + 文件名
    w_dir = os.path.basename(os.path.dirname(wp))
    d_dir = os.path.basename(os.path.dirname(df))
    if os.path.basename(wp) == os.path.basename(df) and w_dir == d_dir:
        return True
    return False


# ============================================================
# 下载器客户端 (返回种子及其文件路径列表)
# ============================================================
class TransmissionClient:
    kind = "transmission"

    def __init__(self, cfg, default_path_map=None):
        self.url = cfg["url"].rstrip("/") + "/transmission/rpc"
        user = cfg.get("user", "")
        self.auth = (user, cfg.get("pass", "")) if user else None
        # 每台下载器的独立路径映射(挂载不同目录)；无则用全局默认
        self.path_map = cfg.get("path_map") or default_path_map or []
        self.known_roots = []
        for _m in self.path_map:
            if _m.get("host_root"):
                self.known_roots.append(_m["host_root"])
            if _m.get("container_root"):
                self.known_roots.append(_m["container_root"])
        self.session = requests.Session()
        self.session_id = self._get_session_id()
        self.get_torrents()

    def _get_session_id(self):
        try:
            r = self.session.get(self.url, auth=self.auth, timeout=10)
            return r.headers.get("X-Transmission-Session-Id", "")
        except Exception:
            return ""

    def _rpc(self, method, args=None, timeout=30):
        payload = {"method": method, "arguments": args or {}}
        headers = {}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        r = self.session.post(self.url, json=payload, auth=self.auth,
                              headers=headers, timeout=timeout)
        if r.status_code == 409:
            self.session_id = r.headers.get("X-Transmission-Session-Id", "")
            headers["X-Transmission-Session-Id"] = self.session_id
            r = self.session.post(self.url, json=payload, auth=self.auth,
                                  headers=headers, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Transmission RPC HTTP {r.status_code}")
        data = r.json()
        if data.get("result") != "success":
            raise RuntimeError(f"Transmission RPC 失败: {data.get('result')}")
        return data.get("arguments", {})

    def get_torrents(self):
        args = self._rpc("torrent-get", {"fields": ["id", "name", "downloadDir",
                                                    "totalSize", "hashString", "files", "trackers"]})
        out = []
        for t in args.get("torrents", []):
            dl = t.get("downloadDir", "")
            files = []
            for f in t.get("files", []):
                fn = f.get("name", "")
                files.append(os.path.join(dl, fn) if not os.path.isabs(fn) else fn)
            # tracker 站点域名(去重排序)，用于按 PT 站确认保种状态
            # TR trackers 字段是 announce(tracker URL)，解析 hostname 作为站点标签
            _trackers = set()
            for _tr in t.get("trackers", []):
                _a = _tr.get("announce", "") or ""
                if _a.startswith(("http://", "https://")):
                    _h = urllib.parse.urlparse(_a).hostname
                    if _h:
                        _trackers.add(_h)
            trackers = sorted(_trackers)
            out.append({
                "downloader": self.kind,
                "id": str(t["id"]),
                "name": t.get("name", ""),
                "dir": dl,
                "size": t.get("totalSize", 0),
                "files": files,
                "trackers": trackers,
            })
        return out

    def delete(self, ids, delete_files=True):
        # TR 的 torrent-remove ids 必须是 int/数组；字符串会被当无效 hash 而静默成功但不删
        _ids = [int(x) for x in ids if str(x).lstrip("-").isdigit()]
        if not _ids:
            raise RuntimeError("Transmission 删除：无有效种子 id")
        # 删大文件时 TR RPC 会同步等待本地数据删除完成，可能超 30s，给足超时
        self._rpc("torrent-remove", {"ids": _ids, "delete-local-data": delete_files}, timeout=600)


class QBClient:
    kind = "qbittorrent"

    def __init__(self, cfg, default_path_map=None):
        self.url = cfg["url"].rstrip("/")
        self.user = cfg.get("user", "") or ""
        self.password = cfg.get("pass", "")
        self.api_key = cfg.get("api_key", "").strip()   # qB 4.6+ 可选 API 密钥(X-API-Key)
        # 每台下载器的独立路径映射
        self.path_map = cfg.get("path_map") or default_path_map or []
        self.known_roots = []
        for _m in self.path_map:
            if _m.get("host_root"):
                self.known_roots.append(_m["host_root"])
            if _m.get("container_root"):
                self.known_roots.append(_m["container_root"])
        self.session = requests.Session()
        self._login()

    def _login(self):
        if self.api_key:
            # qB WebUI 若启用 API 密钥，用 X-API-Key 头直接访问(无需 auth/login)
            self.session.headers["X-API-Key"] = self.api_key
            r = self.session.get(self.url + "/api/v2/app/version", timeout=10)
            if r.status_code == 200 and r.text.strip():
                return
            raise RuntimeError("qBittorrent API 密钥无效，请检查")
        if self.user:
            r = self.session.post(self.url + "/api/v2/auth/login",
                                  data={"username": self.user, "password": self.password},
                                  timeout=10)
            if r.text.strip() != "Ok.":
                raise RuntimeError("qBittorrent 登录失败，请检查 URL/账号/密码")
        # 无密钥且无账号：假定 qB 开启了 localhost 免认证(bypass_local_auth)，
        # 以 http://127.0.0.1:端口 访问时 qB 直接放行，无需任何认证。

    def get_torrents(self):
        r = self.session.get(self.url + "/api/v2/torrents/info",
                             params={"filter": "all"}, timeout=20)
        r.raise_for_status()
        out = []
        for t in r.json():
            h = t.get("hash")
            files = []
            trackers = []
            try:
                fr = self.session.get(self.url + "/api/v2/torrents/files",
                                      params={"hash": h}, timeout=20)
                fr.raise_for_status()
                save = t.get("save_path", "")
                for f in fr.json():
                    fn = f.get("name", "")
                    files.append(os.path.join(save, fn) if not os.path.isabs(fn) else fn)
                # 该种子关联的 tracker 站点(去重)
                tr_r = self.session.get(self.url + "/api/v2/torrents/trackers",
                                        params={"hash": h}, timeout=20)
                tr_r.raise_for_status()
                for tr in tr_r.json():
                    u = tr.get("url", "") or ""
                    host = ""
                    if u.startswith(("http://", "https://")):
                        host = urllib.parse.urlparse(u).hostname or ""
                    elif u and u != "** [DHT] **" and u != "** [PeX] **" and u != "** [LSD] **":
                        host = u.split("/")[0].split(":")[0]
                    if host and host not in trackers:
                        trackers.append(host)
            except Exception:
                pass
            out.append({
                "downloader": self.kind,
                "id": h,
                "name": t.get("name", ""),
                "dir": t.get("save_path", ""),
                "size": t.get("size", 0),
                "files": files,
                "trackers": sorted(trackers),
            })
        return out

    def delete(self, ids, delete_files=True):
        r = self.session.post(self.url + "/api/v2/torrents/delete",
                              data={"hashes": "|".join(ids),
                                    "deleteFiles": "true" if delete_files else "false"},
                              timeout=600)
        r.raise_for_status()


FACTORY = {"transmission": TransmissionClient, "qbittorrent": QBClient}


# ============================================================
# 管理器
# ============================================================
class Cleaner:
    def __init__(self, config):
        self.config = config
        self.fnnas_cfg = config.get("fnnas", {})
        # 全局默认路径映射(每台下载器可用自己 path_map 覆盖)
        self.path_map = self.fnnas_cfg.get("path_map", [])
        self.downloaders = load_downloaders(config)
        self.clients = []
        self.errors = []
        self._build_clients()

    def _build_clients(self):
        """按当前下载器配置重建连接；只连 enabled 项。"""
        self.clients = []
        self.errors = []
        for cfg in self.downloaders:
            if not cfg.get("enabled", True):
                continue
            try:
                self.clients.append(FACTORY[cfg["type"]](cfg, default_path_map=self.path_map))
            except Exception as e:
                self.errors.append(f"{cfg.get('url')}: {e}")

    def reload(self):
        """WebUI 修改下载器后重建连接(无需重启容器)。"""
        self.downloaders = load_downloaders(self.config)
        self._build_clients()

    @property
    def ok(self):
        return len(self.clients) > 0

    def all_torrents(self):
        out = []
        for c in self.clients:
            try:
                out.extend(c.get_torrents())
            except Exception:
                continue
        return out

    def get_watched(self):
        """读取飞牛影视已看文件列表。"""
        return FnnasWatched(self.fnnas_cfg).get_watched()

    def preview_watched(self):
        """
        核心：读取已看 -> 匹配下载器种子 -> 生成候选删除清单。
        返回结构：
          {
            watched_count,           已看文件数
            works: [{
                downloader, id, name, dir, size, size_text,
                matched_count,         命中的已看视频文件数
                total_video,           种子内视频文件总数
                all_watched,           是否种子内视频全部已看
                matched_files,         命中的文件名列表(前若干)
                watched_titles,        命中的已看条目名列表
            }]
          }
        """
        watched = self.get_watched()
        # 索引：(文件名, 父目录名) -> 已看记录列表，先粗筛再精确匹配，避免 O(N*M) 全比对
        idx = {}
        for w in watched:
            p = w.get("path")
            if not p:
                continue
            k = (os.path.basename(p), os.path.basename(os.path.dirname(p)))
            idx.setdefault(k, []).append(w)
        def _to_host(path, path_map):
            """把下载器容器内路径转成宿主机路径(用于跨下载器聚合与已看匹配)。"""
            if not path:
                return path
            p = path.replace("\\", "/")
            for _m in path_map or []:
                _cr = (_m.get("container_root") or "").rstrip("/")
                _hr = (_m.get("host_root") or "").rstrip("/")
                if _cr and (p == _cr or p.startswith(_cr + "/")):
                    return _hr + p[len(_cr):]
            return path

        # 所有 path_map 的根(供剥根匹配)
        host_roots = []
        for _c in self.clients:
            for _m in _c.path_map:
                if _m.get("host_root"):
                    host_roots.append(_m["host_root"])
                if _m.get("container_root"):
                    host_roots.append(_m["container_root"])

        # 按"宿主文件路径"聚合：同一文件跨下载器/多 tracker 做种 -> 合并为一条
        seeded_host = set()
        groups = {}
        order = []
        for c in self.clients:
            try:
                torrents = c.get_torrents()
            except Exception:
                continue
            for t in torrents:
                for f in t.get("files", []):
                    seeded_host.add(_to_host(f, c.path_map))
                vids_host = []
                for f in t.get("files", []):
                    if is_video(f):
                        vids_host.append(_to_host(f, c.path_map))
                if not vids_host:
                    continue
                key = tuple(sorted(set(vids_host)))
                g = groups.get(key)
                if g is None:
                    g = {
                        "name": t["name"],
                        "size": t["size"],
                        "refs": [],
                        "trackers": set(),
                        "dirs": set(),
                        "downloaders": set(),
                        "vids_host": key,
                    }
                    groups[key] = g
                    order.append(key)
                g["refs"].append({"downloader": c.kind, "url": c.url, "id": t["id"], "size": t["size"]})
                g["trackers"].update(t.get("trackers", []))
                g["dirs"].add(t["dir"])
                g["downloaders"].add(c.kind)

        works = []
        for key in order:
            g = groups[key]
            matched = []
            titles = set()
            for hf in g["vids_host"]:
                kb = (os.path.basename(hf), os.path.basename(os.path.dirname(hf)))
                for w in idx.get(kb, []):
                    if watched_match(w["path"], hf, host_roots):
                        matched.append(hf)
                        titles.add(w.get("title", ""))
                        break
            if not matched:
                continue
            total_video = len(g["vids_host"])
            works.append({
                "downloader": sorted(g["downloaders"])[0] if g["downloaders"] else "",
                "all_downloaders": sorted(g["downloaders"]),
                "client": g["refs"][0]["url"] if g["refs"] else "",
                "id": g["refs"][0]["id"] if g["refs"] else "",
                "name": g["name"],
                "dir": sorted(g["dirs"])[0] if g["dirs"] else "",
                "dirs": sorted(g["dirs"]),
                "size": g["size"],
                "size_text": fmt_size(g["size"]),
                "matched_count": len(matched),
                "total_video": total_video,
                "all_watched": total_video > 0 and len(matched) == total_video,
                "matched_files": matched[:8],
                "trackers": sorted(g["trackers"]),
                "refs": g["refs"],
                "watched_titles": sorted(t for t in titles if t)[:6],
            })
        total = sum(w["size"] for w in works)
        works = sorted(works, key=lambda w: (not w["all_watched"], -w["size"]))
        # 同作品不同版本(不同文件路径/分辨率/来源) -> 可勾选一并删除
        for w in works:
            w["_key"] = work_key(w["name"])
        by_key = {}
        for w in works:
            by_key.setdefault(w["_key"], []).append(w)
        for w in works:
            w["siblings"] = [
                {"name": o["name"], "size_text": o["size_text"], "all_watched": o["all_watched"],
                 "trackers": o["trackers"], "downloaders": o["all_downloaders"],
                 "dirs": o["dirs"], "refs": o["refs"]}
                for o in by_key.get(w["_key"], []) if o is not w
            ][:20]
            w.pop("_key", None)
        # watched but not seeded in any configured downloader
        unmatched = []
        um_map = {}
        for w in watched:
            p = w.get("path")
            if not p or p in seeded_host:
                continue
            key = os.path.dirname(p)
            e = um_map.get(key)
            if e is None:
                e = {"dir": key, "titles": [], "files": [], "count": 0}
                um_map[key] = e
                unmatched.append(e)
            e["files"].append(p)
            e["count"] += 1
            t = (w.get("title") or "").strip()
            if t and t not in e["titles"]:
                e["titles"].append(t)
        for e in unmatched:
            e["titles"] = e["titles"][:6]
        unmatched.sort(key=lambda e: -e["count"])
        return {
            "watched_count": len(watched),
            "work_count": len(works),
            "works": works,
            "total_size": total,
            "total_size_text": fmt_size(total),
            "unmatched": unmatched,
        }

    def delete(self, refs):
        # 按 (下载器类型, 客户端url) 精确路由，支持同一类型多实例(如两个 Transmission 9091/9092)
        client_map = {(c.kind, c.url): c for c in self.clients}
        grouped = {}
        for ref in refs:
            key = (ref.get("downloader"), ref.get("url", ""))
            grouped.setdefault(key, []).append(ref["id"])
        deleted = []
        for key, ids in grouped.items():
            client = client_map.get(key)
            if not client:
                continue
            client.delete(ids, delete_files=True)
            deleted.append({"downloader": key[0], "url": key[1], "count": len(ids)})
        return deleted

    def _roots(self):
        roots = []
        for c in self.clients:
            for m in c.path_map:
                hr = m.get("host_root")
                if hr and hr not in roots:
                    roots.append(hr)
        return roots

    def delete_files(self, files):
        """删除已看文件本身(安全，不删目录，不影响同目录其他资源)。"""
        roots = self._roots()
        results = []
        for f in files or []:
            f = (f or "").strip()
            if not f or not f.startswith("/"):
                results.append({"path": f, "ok": False, "error": "必须是绝对路径"})
                continue
            if not any(f == r or f.startswith(r + "/") for r in roots):
                results.append({"path": f, "ok": False, "error": "路径不在数据目录下，已拒绝"})
                continue
            if not os.path.isfile(f):
                results.append({"path": f, "ok": False, "error": "文件不存在"})
                continue
            try:
                os.remove(f)
                results.append({"path": f, "ok": True})
            except Exception as e:
                results.append({"path": f, "ok": False, "error": str(e)})
        return results

    def delete_dirs(self, dirs):
        """删除整个作品目录(强确认后使用)。仅当目录确为单一作品时安全。"""
        roots = self._roots()
        results = []
        for d in dirs or []:
            d = (d or "").strip().rstrip("/")
            if not d:
                results.append({"dir": d, "ok": False, "error": "空路径"})
                continue
            if not d.startswith("/"):
                results.append({"dir": d, "ok": False, "error": "必须是绝对路径"})
                continue
            if not any(d == r or d.startswith(r + "/") for r in roots):
                results.append({"dir": d, "ok": False, "error": "路径不在数据目录下，已拒绝"})
                continue
            if not os.path.exists(d):
                results.append({"dir": d, "ok": False, "error": "路径不存在"})
                continue
            try:
                shutil.rmtree(d)
                results.append({"dir": d, "ok": True})
            except Exception as e:
                results.append({"dir": d, "ok": False, "error": str(e)})
        return results


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
