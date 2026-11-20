import json
import sqlite3
from contextlib import closing


SCHEMA = """
CREATE TABLE IF NOT EXISTS node (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    settings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device (
    position INTEGER PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS login_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    last_step INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, data):
        self.path = data / "yhl-ui.sqlite3"
        self.legacy = data / "state.json"
        self.legacy_otp = data / "otp.json"

    @property
    def initialized(self):
        return self.path.is_file() or self.legacy.is_file()

    def connect(self, *, readonly=False):
        if readonly:
            return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        self.path.touch(mode=0o600, exist_ok=True)
        return sqlite3.connect(self.path)

    def load(self):
        if not self.path.is_file():
            return json.loads(self.legacy.read_text(encoding="utf-8"))
        with closing(self.connect(readonly=True)) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT settings FROM node WHERE id = 1").fetchone()
            if row is None:
                return json.loads(self.legacy.read_text(encoding="utf-8"))
            state = json.loads(row[0])
            state["devices"] = [
                {"id": key, "name": name, "token": token}
                for key, name, token in db.execute("SELECT id, name, token FROM device ORDER BY position")
            ]
            return state

    def legacy_step(self):
        return json.loads(self.legacy_otp.read_text(encoding="utf-8")) if self.legacy_otp.is_file() else 0

    def last_step(self):
        if not self.path.is_file():
            return self.legacy_step()
        with closing(self.connect(readonly=True)) as db:
            row = db.execute("SELECT last_step FROM login_state WHERE id = 1").fetchone()
        return row[0] if row else self.legacy_step()

    def save(self, state):
        settings = {key: value for key, value in state.items() if key != "devices"}
        try:
            with closing(self.connect()) as db:
                db.executescript(SCHEMA)
                with db:
                    db.execute("INSERT OR REPLACE INTO node VALUES (1, ?)", (json.dumps(settings),))
                    db.execute("INSERT OR IGNORE INTO login_state VALUES (1, ?)", (self.legacy_step(),))
                    db.execute("DELETE FROM device")
                    db.executemany("INSERT INTO device VALUES (?, ?, ?, ?)", [
                        (index, entry["id"], entry["name"], entry["token"])
                        for index, entry in enumerate(state["devices"])
                    ])
        except sqlite3.Error as error:
            raise RuntimeError("无法保存 yhl-ui 数据。") from error

    def save_step(self, step):
        try:
            with closing(self.connect()) as db, db:
                db.execute("UPDATE login_state SET last_step = ? WHERE id = 1", (step,))
        except sqlite3.Error as error:
            raise RuntimeError("无法保存验证器登录状态。") from error
