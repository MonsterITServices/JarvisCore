import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_MAX_DOCUMENT_CHARS = 500_000
_CHUNK_SIZE = 1400
_CHUNK_OVERLAP = 180
_STOP_WORDS = {"about", "after", "again", "also", "and", "are", "can", "does", "for", "from", "have", "how", "into", "its", "more", "not", "that", "the", "their", "this", "what", "when", "where", "which", "with", "you", "your"}


class KnowledgeStoreError(Exception):
    pass


def database_path():
    override = os.environ.get("JARVIS_KNOWLEDGE_DB")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "JarvisCore" / "knowledge.sqlite3"
    return Path.home() / ".local" / "share" / "jarviscore" / "knowledge.sqlite3"


def _connect():
    path = database_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        _initialize(connection)
        return connection
    except (OSError, sqlite3.Error) as error:
        raise KnowledgeStoreError("The local knowledge database could not be opened.") from error


def _initialize(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            document_id INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            document_type TEXT NOT NULL,
            content_chars INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            title,
            source,
            content,
            tokenize = 'unicode61'
        );
        """
    )
    connection.commit()


def _chunks(text):
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + _CHUNK_SIZE // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


def index_document(source, title, content, document_type="document"):
    if not isinstance(content, str) or not content.strip():
        raise KnowledgeStoreError("No readable text was found in that source.")
    content = content.replace("\x00", " ").strip()
    if len(content) > _MAX_DOCUMENT_CHARS:
        content = content[:_MAX_DOCUMENT_CHARS]
    title = (title or "Untitled source").strip()[:300]
    source = (source or "local source").strip()[:2000]
    document_type = (document_type or "document").strip()[:40]
    fingerprint = hashlib.sha256(f"{source}\0{content}".encode("utf-8")).hexdigest()
    pieces = _chunks(content)
    if not pieces:
        raise KnowledgeStoreError("No readable text was found in that source.")

    connection = _connect()
    try:
        existing = connection.execute(
            "SELECT document_id FROM documents WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if existing:
            return {"document_id": existing["document_id"], "duplicate": True, "chunks": len(pieces)}

        cursor = connection.execute(
            "INSERT INTO documents (fingerprint, title, source, document_type, content_chars, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fingerprint, title, source, document_type, len(content), datetime.now(timezone.utc).isoformat()),
        )
        document_id = cursor.lastrowid
        for chunk_index, piece in enumerate(pieces):
            chunk_cursor = connection.execute(
                "INSERT INTO chunks (document_id, chunk_index, content) VALUES (?, ?, ?)",
                (document_id, chunk_index, piece),
            )
            connection.execute(
                "INSERT INTO knowledge_fts (rowid, title, source, content) VALUES (?, ?, ?, ?)",
                (chunk_cursor.lastrowid, title, source, piece),
            )
        connection.commit()
        return {"document_id": document_id, "duplicate": False, "chunks": len(pieces)}
    except sqlite3.Error as error:
        connection.rollback()
        raise KnowledgeStoreError("The source could not be added to the local library.") from error
    finally:
        connection.close()


def search_knowledge(query, limit=5):
    terms = []
    for term in re.findall(r"[\w-]{2,}", query.lower(), flags=re.UNICODE):
        if term not in _STOP_WORDS and term not in terms:
            terms.append(term)
        if len(terms) >= 12:
            break
    if not terms:
        return []
    match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT d.document_id, d.title, d.source, d.document_type, c.content "
            "FROM knowledge_fts f "
            "JOIN chunks c ON c.chunk_id = f.rowid "
            "JOIN documents d ON d.document_id = c.document_id "
            "WHERE knowledge_fts MATCH ? "
            "ORDER BY bm25(knowledge_fts) LIMIT ?",
            (match_query, max(1, min(limit, 8))),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as error:
        raise KnowledgeStoreError("The local knowledge library could not be searched.") from error
    finally:
        connection.close()


def list_documents():
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT document_id, title, source, document_type, content_chars, added_at "
            "FROM documents ORDER BY added_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as error:
        raise KnowledgeStoreError("The local knowledge library could not be listed.") from error
    finally:
        connection.close()


def delete_document(document_id):
    connection = _connect()
    try:
        chunk_ids = connection.execute(
            "SELECT chunk_id FROM chunks WHERE document_id = ?", (document_id,)
        ).fetchall()
        for row in chunk_ids:
            connection.execute("DELETE FROM knowledge_fts WHERE rowid = ?", (row["chunk_id"],))
        cursor = connection.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        connection.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as error:
        connection.rollback()
        raise KnowledgeStoreError("The source could not be removed from the local library.") from error
    finally:
        connection.close()
