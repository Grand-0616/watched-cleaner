#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py —— 看完即清 WebUI v2 后端 (Flask)
=========================================
启动：python3 app.py   (默认 http://0.0.0.0:5000)
提供：
  GET  /                前端页面
  GET  /api/status      下载器连接状态
  GET  /api/watched     自动读取飞牛影视已看 -> 匹配下载器种子 -> 候选删除清单
  POST /api/delete      删除选中的种子任务(含数据文件)

安全：
  - config.json 的 web.token 可设置访问口令；留空则不启用。
  - 前端删除前二次确认；后端删除只接受显式的种子引用列表。
依赖：flask, requests   (pip3 install flask requests)
"""

import os
import hmac
import threading

from flask import Flask, request, jsonify, render_template

from cleaner import Cleaner, load_config, save_downloaders, ensure_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 首次启动自动生成 config/config.json（模板来自 config.example.json），
# 所以部署时不需要手动 cp，更新代码也不会覆盖已有配置。
CONFIG_PATH = ensure_config()

app = Flask(__name__, template_folder="templates", static_folder="static")

_config = load_config(CONFIG_PATH)
_cleaner = Cleaner(_config)
TOKEN = _config.get("web", {}).get("token", "")

# 删除改为后台线程执行(删大文件耗时可能达数分钟)，锁防止并发重复删除
_delete_lock = threading.Lock()


def _require_token():
    if not TOKEN:
        return True
    provided = request.headers.get("X-Token", "")
    return hmac.compare_digest(provided, TOKEN)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    if not _require_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    fnnas_cfg = _config.get("fnnas", {})
    try:
        wc = len(_cleaner.get_watched())
        watched_ok = True
        watched_err = ""
    except Exception as e:
        wc = 0
        watched_ok = False
        watched_err = str(e)
    return jsonify({
        "ok": _cleaner.ok,
        "errors": _cleaner.errors,
        "downloader_count": len(_cleaner.clients),
        "total_torrents": len(_cleaner.all_torrents()),
        "fnnas": {
            "db_path": fnnas_cfg.get("db_path", ""),
            "username": fnnas_cfg.get("username", ""),
            "ok": watched_ok,
            "watched_count": wc,
            "error": watched_err,
        },
    })


@app.route("/api/watched")
def watched():
    if not _require_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        result = _cleaner.preview_watched()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/delete", methods=["POST"])
def delete():
    if not _require_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    refs = data.get("refs", [])
    if not isinstance(refs, list) or not refs:
        return jsonify({"ok": False, "error": "缺少要删除的种子引用"}), 400
    try:
        if not _delete_lock.acquire(blocking=False):
            return jsonify({"ok": False, "error": "已有删除任务进行中，请稍后再试"}), 429
        refs_copy = [dict(r) for r in refs]
        def _work():
            try:
                _cleaner.delete(refs_copy)
            finally:
                _delete_lock.release()
        threading.Thread(target=_work, daemon=True).start()
        return jsonify({"ok": True,
                        "message": "已提交删除，正在后台执行(删除种子并移除本地数据)。请稍候刷新页面确认结果。"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/delete_unmatched", methods=["POST"])
def delete_unmatched():
    """无对应种子的已看作品：安全删除已看文件本身(不删目录，不影响同目录其他资源)。"""
    if not _require_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    files = data.get("files", [])
    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "error": "缺少要删除的文件"}), 400
    try:
        if not _delete_lock.acquire(blocking=False):
            return jsonify({"ok": False, "error": "已有删除任务进行中，请稍后再试"}), 429
        files_copy = [str(f) for f in files]
        def _work():
            try:
                _cleaner.delete_files(files_copy)
            finally:
                _delete_lock.release()
        threading.Thread(target=_work, daemon=True).start()
        return jsonify({"ok": True, "message": "已提交删除已看文件，后台执行中。请稍候刷新确认。"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/delete_unmatched_dir", methods=["POST"])
def delete_unmatched_dir():
    """强确认后删除整个作品目录(仅当目录确为单一作品、无其他未看资源时使用)。"""
    if not _require_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    dirs = data.get("dirs", [])
    if not isinstance(dirs, list) or not dirs:
        return jsonify({"ok": False, "error": "缺少要删除的目录"}), 400
    try:
        if not _delete_lock.acquire(blocking=False):
            return jsonify({"ok": False, "error": "已有删除任务进行中，请稍后再试"}), 429
        dirs_copy = [str(d) for d in dirs]
        def _work():
            try:
                _cleaner.delete_dirs(dirs_copy)
            finally:
                _delete_lock.release()
        threading.Thread(target=_work, daemon=True).start()
        return jsonify({"ok": True, "message": "已提交删除整个目录，后台执行中。请稍候刷新确认。"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/downloaders", methods=["GET", "POST"])
def downloaders_api():
    """下载器自定义选择：GET 返回配置与连接状态；POST 保存并热重载(无需重启)。"""
    if not _require_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if request.method == "GET":
        return jsonify({"ok": True, "downloaders": _cleaner.downloaders,
                        "errors": _cleaner.errors,
                        "connected": {c.kind: True for c in _cleaner.clients}})
    data = request.get_json(silent=True) or {}
    dl = data.get("downloaders")
    if not isinstance(dl, list):
        return jsonify({"ok": False, "error": "downloaders 必须是数组"}), 400
    try:
        save_downloaders(dl)
        _cleaner.reload()
        return jsonify({"ok": True, "downloaders": _cleaner.downloaders,
                        "errors": _cleaner.errors,
                        "connected": {c.kind: True for c in _cleaner.clients}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    host = _config.get("web", {}).get("host", "0.0.0.0")
    port = _config.get("web", {}).get("port", 5000)
    print(f"看完即清 WebUI v2 已启动: http://{host}:{port}")
    if TOKEN:
        print("已启用访问口令保护。")
    app.run(host=host, port=port, debug=False)
