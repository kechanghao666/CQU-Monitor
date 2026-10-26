"""只读生成数据库完整性及逐表指纹，用于升级和整理前后的记录核对。"""

import hashlib
import json
from pathlib import Path
import sqlite3
import sys


def inspect(database):
    database = Path(database).resolve()
    result = {"database": str(database), "tables": {}}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        result["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        result["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
        result["active_sessions"] = connection.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL"
        ).fetchall()
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for (name,) in names:
            quoted = '"' + name.replace('"', '""') + '"'
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute("SELECT * FROM " + quoted + " ORDER BY rowid"):
                digest.update(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                count += 1
            result["tables"][name] = {"count": count, "sha256": digest.hexdigest().upper()}
    assert result["integrity"] == "ok"
    return result


if __name__ == "__main__":
    Path(sys.argv[2]).write_text(
        json.dumps(inspect(sys.argv[1]), ensure_ascii=False, indent=2), encoding="utf-8"
    )
