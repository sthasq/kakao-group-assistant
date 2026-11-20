"""Bounded local room memory. No participant names or API credentials are stored."""
import os
import sqlite3
from datetime import timedelta


class ChatMemory:
    def __init__(self, path):
        if str(path) != ":memory:":
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, speaker TEXT NOT NULL,
            stamp TEXT NOT NULL, text TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS messages_stamp ON messages(stamp)")
        self.db.commit()

    def ingest(self, messages, appended, at):
        """Persist only appended rows, or safely merge a baseline after reconnect."""
        if appended is None:
            existing = {row[0] for row in self.db.execute("SELECT fingerprint FROM messages")}
            # Preserve repeated identical messages within a first-time snapshot.
            incoming = [m for m in messages if m["_key"] not in existing]
        else:
            incoming = messages[-appended:] if appended else []
        added = []
        with self.db:
            for message in incoming:
                cursor = self.db.execute(
                    "INSERT INTO messages(fingerprint,speaker,stamp,text) VALUES (?,?,?,?)",
                    (message["_key"], message["speaker"], message["time"], message["text"]),
                )
                added.append((cursor.lastrowid, message["speaker"], message["time"]))
            self.db.execute("DELETE FROM messages WHERE stamp < ?", ((at - timedelta(hours=24)).isoformat(timespec="minutes"),))
            self.db.execute("DELETE FROM messages WHERE id NOT IN (SELECT id FROM messages ORDER BY id DESC LIMIT 2000)")
        return added

    def context(self, at, minutes, limit):
        rows = self.db.execute(
            """SELECT id,speaker,stamp,text FROM messages
               WHERE stamp >= ? AND stamp <= ?
               ORDER BY stamp DESC,id DESC LIMIT ?""",
            ((at - timedelta(minutes=minutes)).isoformat(timespec="minutes"),
             at.isoformat(timespec="minutes"), limit),
        ).fetchall()
        return [{"id": row[0], "speaker": row[1], "time": row[2], "text": row[3]} for row in reversed(rows)]

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def close(self):
        self.db.close()
