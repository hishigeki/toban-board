"""当番ボード (TobanBoard) — Flask アプリ本体。"""
from __future__ import annotations

import logging
import os
import threading
from zoneinfo import ZoneInfo

try:  # Python 3.11 以上は標準。3.10 は tomli で代用
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from flask import Flask, jsonify, redirect, render_template, request

from .backends import MemoryBackend, SheetsBackend
from .store import Store, TobanError

log = logging.getLogger("toban")

DEFAULT_CONFIG = {
    "app": {
        "timezone": "Asia/Tokyo",
        "fiscal_year_start_month": 4,
        "reload_minutes": 10,
        "buildings": ["1号館", "2号館"],
        "weekdays": [0, 1, 2, 3, 4],
    },
    "sheet": {
        "spreadsheet_id": "",
        "sheet_url": "",          # 管理タブのボタンの開き先。空なら spreadsheet_id から作る
        "credentials_file": "/etc/toban/service-account.json",
        "roster_sheet": "職員名簿",
        "schedule_sheet": "当番表",
        "log_sheet": "交代ログ",
        "closed_sheet": "休校日",
    },
}


def load_config(path: str | None = None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    env_path = os.environ.get("TOBAN_CONFIG")
    if not path and env_path and not os.path.exists(env_path):
        # 環境変数で場所を指定したのに無いときは、黙って初期値で進めずに知らせる
        raise FileNotFoundError(f"設定ファイルが見つかりません: {env_path}")
    path = path or env_path or "/etc/toban/config.toml"
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                user = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(
                f"設定ファイル {path} の書き方が正しくありません: {e}\n"
                "（Windowsのパスは \"C:/folder/key.json\" のように / で区切るか、'C:\\folder\\key.json' のように一重引用符で囲みます）"
            ) from e
        for sec, vals in user.items():
            cfg.setdefault(sec, {}).update(vals)
    return cfg


def demo_backend() -> MemoryBackend:
    names = ["青木", "伊藤", "上田", "江口", "小野", "加藤", "木村", "工藤", "近藤", "佐藤",
             "鈴木", "高橋", "田中", "中村", "西田", "野村", "橋本", "藤井", "松本", "山田"]
    roster = [["氏名", "鍵当番", "ごみ当番", "館(任意)"]]
    for i, n in enumerate(names):
        roster.append([n, "○" if i % 5 != 4 else "", "○" if i % 7 != 6 else "", ""])
    return MemoryBackend(roster=roster)


def create_app(cfg: dict | None = None, backend=None, now_fn=None, start_thread=True) -> Flask:
    cfg = cfg or load_config()
    app_cfg, sh = cfg["app"], cfg["sheet"]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if backend is None:
        if os.environ.get("TOBAN_DEMO") == "1":
            backend = demo_backend()
        else:
            backend = SheetsBackend(
                sh["spreadsheet_id"], sh["credentials_file"],
                {"roster": sh["roster_sheet"], "schedule": sh["schedule_sheet"],
                 "log": sh["log_sheet"], "closed": sh["closed_sheet"]},
            )

    store = Store(
        backend, app_cfg["buildings"], ZoneInfo(app_cfg["timezone"]),
        app_cfg["fiscal_year_start_month"], tuple(app_cfg["weekdays"]), now_fn,
    )
    try:
        store.load()
        store.refresh_roster()
    except Exception as e:  # 起動時にネットが無くても画面は開く
        log.error("起動時の読み込みに失敗: %s", e)
        store.last_error = str(e)

    if os.environ.get("TOBAN_DEMO") == "1" and not store.rows:
        store.allocate()

    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config["STORE"] = store
    nb = len(store.buildings)

    @app.errorhandler(TobanError)
    def _toban_error(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(Exception)
    def _any_error(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        log.exception("予期しないエラー")
        return jsonify(error=f"サーバーエラー: {e}"), 500

    @app.get("/")
    def index():
        return redirect("/1")

    @app.get("/<int:b>")
    def page(b):
        if not 1 <= b <= nb:
            return redirect("/1")
        demo = os.environ.get("TOBAN_DEMO") == "1"
        # 管理タブの「スプレッドシートを開く」。閲覧・編集できるかはスプレッドシート側の共有設定で決まる。
        # デモは環境変数 TOBAN_SHEET_URL で、ダミーデータのシートを指定する
        sid = (sh.get("spreadsheet_id") or "").strip()
        sheet_url = (os.environ.get("TOBAN_SHEET_URL") or sh.get("sheet_url") or "").strip()
        if not sheet_url and sid:
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
        return render_template("index.html", b=b, buildings=store.buildings,
                               demo=demo, sheet_url=sheet_url)

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, last_sync=store.last_sync, error=store.last_error)

    @app.get("/api/state")
    def state():
        b = request.args.get("b", 1, type=int)
        return jsonify(store.view(b))

    @app.post("/api/swap")
    def swap():
        j = request.get_json(force=True)
        a, c = j.get("a") or {}, j.get("c") or {}
        res = store.swap(int(j.get("b", 0)), a.get("date", ""), a.get("slot", ""),
                         c.get("date", ""), c.get("slot", ""),
                         a.get("name", ""), c.get("name", ""))
        return jsonify(res)

    @app.post("/api/admin/allocate")
    def allocate():
        j = request.get_json(silent=True) or {}
        fy = j.get("fy")
        return jsonify(store.allocate(int(fy) if fy else None))

    @app.post("/api/admin/reload")
    def reload_():
        store.load()
        store.refresh_roster()
        return jsonify(ok=True, banner=store.banner())

    if start_thread:
        interval = max(1, int(app_cfg["reload_minutes"])) * 60

        def loop():
            stop = threading.Event()
            while not stop.wait(interval):
                try:
                    store.load()
                    store.refresh_roster()
                    store.retry_pending_log()
                except Exception as e:
                    store.last_error = str(e)
                    log.warning("定期読み込みに失敗(前回の内容を表示し続けます): %s", e)

        threading.Thread(target=loop, daemon=True, name="toban-reload").start()
    return app
