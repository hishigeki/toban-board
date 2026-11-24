"""開発用: TOBAN_DEMO=1 python -m toban  (メモリ上のデモデータで起動)"""
import os

from .app import create_app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
