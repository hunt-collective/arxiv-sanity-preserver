"""
Database support functions.
The idea is that none of the individual scripts deal directly with the file system.
Any of the file system I/O and the associated settings are in this single file.
"""

import os
import threading
import sqlite3, zlib, pickle, tempfile
from sqlitedict import SqliteDict
from contextlib import contextmanager

# -----------------------------------------------------------------------------
# global configuration

DATA_DIR = 'data'

# if set, all state lives in this Postgres database (e.g. Neon) instead of files in DATA_DIR.
# this is what makes the server work on Vercel, where the filesystem does not persist.
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# -----------------------------------------------------------------------------
# utilities for safe writing of a pickle file

# Context managers for atomic writes courtesy of
# http://stackoverflow.com/questions/2333872/atomic-writing-to-file-with-python
@contextmanager
def _tempfile(*args, **kws):
    """ Context for temporary file.
    Will find a free temporary filename upon entering
    and will try to delete the file on leaving
    Parameters
    ----------
    suffix : string
        optional file suffix
    """

    fd, name = tempfile.mkstemp(*args, **kws)
    os.close(fd)
    try:
        yield name
    finally:
        try:
            os.remove(name)
        except OSError as e:
            if e.errno == 2:
                pass
            else:
                raise e


@contextmanager
def open_atomic(filepath, *args, **kwargs):
    """ Open temporary file object that atomically moves to destination upon
    exiting.
    Allows reading and writing to and from the same filename.
    Parameters
    ----------
    filepath : string
        the file path to be opened
    fsync : bool
        whether to force write the file to disk
    kwargs : mixed
        Any valid keyword arguments for :code:`open`
    """
    fsync = kwargs.pop('fsync', False)

    with _tempfile(dir=os.path.dirname(filepath)) as tmppath:
        with open(tmppath, *args, **kwargs) as f:
            yield f
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.rename(tmppath, filepath)

def safe_pickle_dump(obj, fname):
    """
    prevents a case where one process could be writing a pickle file
    while another process is reading it, causing a crash. the solution
    is to write the pickle file to a temporary file and then move it.
    """
    with open_atomic(fname, 'wb') as f:
        pickle.dump(obj, f, -1) # -1 specifies highest binary protocol

# -----------------------------------------------------------------------------

class CompressedSqliteDict(SqliteDict):
    """ overrides the encode/decode methods to use zlib, so we get compressed storage """

    def __init__(self, *args, **kwargs):

        def encode(obj):
            return sqlite3.Binary(zlib.compress(pickle.dumps(obj, pickle.HIGHEST_PROTOCOL)))

        def decode(obj):
            return pickle.loads(zlib.decompress(bytes(obj)))

        super().__init__(*args, **kwargs, encode=encode, decode=decode)

# -----------------------------------------------------------------------------
# Postgres backend: a dict-like table with the same interface as SqliteDict

_pg_conn = None
_pg_lock = threading.Lock()
_pg_tables = set() # tables we already made sure exist, in this process

def _pg(reconnect=False):
    """ a single shared autocommit connection per process, reopened if it dropped """
    global _pg_conn
    import psycopg # only needed when DATABASE_URL is set
    with _pg_lock:
        if reconnect or _pg_conn is None or _pg_conn.closed or _pg_conn.broken:
            _pg_conn = psycopg.connect(DATABASE_URL, autocommit=True)
    return _pg_conn

# values are stored as jsonb (queryable from SQL) or as zlib-compressed pickles (for e.g. sets)
def _jsonb(v):
    from psycopg.types.json import Jsonb
    return Jsonb(v)

_json_codec = (_jsonb, lambda v: v) # psycopg decodes jsonb to python objects itself
_pickle_codec = (lambda v: zlib.compress(pickle.dumps(v, pickle.HIGHEST_PROTOCOL)), lambda v: pickle.loads(zlib.decompress(v)))

class PgDict:

    def __init__(self, tablename, codec):
        self.table = tablename
        self.encode, self.decode = codec
        if tablename not in _pg_tables:
            vtype = 'jsonb' if codec is _json_codec else 'bytea'
            self._exec(f'CREATE TABLE IF NOT EXISTS {self.table} (key text PRIMARY KEY, value {vtype} NOT NULL)')
            _pg_tables.add(tablename)

    def _exec(self, sql, args=()):
        import psycopg
        try:
            return _pg().execute(sql, args)
        except psycopg.OperationalError:
            # e.g. the database closed our idle connection (Neon does when it scales to zero)
            return _pg(reconnect=True).execute(sql, args)

    def __getitem__(self, key):
        row = self._exec(f'SELECT value FROM {self.table} WHERE key = %s', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return self.decode(row[0])

    def __setitem__(self, key, value):
        self._exec(f'INSERT INTO {self.table} (key, value) VALUES (%s, %s) '
                   f'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (key, self.encode(value)))

    def __delitem__(self, key):
        if self._exec(f'DELETE FROM {self.table} WHERE key = %s', (key,)).rowcount == 0:
            raise KeyError(key)

    def __contains__(self, key):
        return self._exec(f'SELECT 1 FROM {self.table} WHERE key = %s', (key,)).fetchone() is not None

    def __len__(self):
        return self._exec(f'SELECT count(*) FROM {self.table}').fetchone()[0]

    def __iter__(self):
        return iter(self.keys())

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return [r[0] for r in self._exec(f'SELECT key FROM {self.table} ORDER BY key')]

    def items(self):
        return [(k, self.decode(v)) for k, v in self._exec(f'SELECT key, value FROM {self.table} ORDER BY key')]

    def values(self):
        return [v for k, v in self.items()]

    def close(self):
        pass # the connection is shared and stays open across requests

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

# -----------------------------------------------------------------------------
"""
some docs to self:
flag='c': default mode, open for read/write, and creating the db/table if necessary
flag='r': open for read-only
"""

# stores info about papers, and also their lighter-weight metadata
PAPERS_DB_FILE = os.path.join(DATA_DIR, 'papers.db')
# stores account-relevant info, like which tags exist for which papers
DICT_DB_FILE = os.path.join(DATA_DIR, 'dict.db')

def get_papers_db(flag='r', autocommit=True):
    assert flag in ['r', 'c']
    if DATABASE_URL:
        return PgDict('papers', _json_codec)
    pdb = CompressedSqliteDict(PAPERS_DB_FILE, tablename='papers', flag=flag, autocommit=autocommit)
    return pdb

def get_metas_db(flag='r', autocommit=True):
    assert flag in ['r', 'c']
    if DATABASE_URL:
        return PgDict('metas', _json_codec)
    mdb = SqliteDict(PAPERS_DB_FILE, tablename='metas', flag=flag, autocommit=autocommit)
    return mdb

def get_tags_db(flag='r', autocommit=True):
    assert flag in ['r', 'c']
    if DATABASE_URL:
        return PgDict('tags', _pickle_codec)
    tdb = CompressedSqliteDict(DICT_DB_FILE, tablename='tags', flag=flag, autocommit=autocommit)
    return tdb

def get_last_active_db(flag='r', autocommit=True):
    assert flag in ['r', 'c']
    if DATABASE_URL:
        return PgDict('last_active', _json_codec)
    ladb = SqliteDict(DICT_DB_FILE, tablename='last_active', flag=flag, autocommit=autocommit)
    return ladb

def get_email_db(flag='r', autocommit=True):
    assert flag in ['r', 'c']
    if DATABASE_URL:
        return PgDict('email', _json_codec)
    edb = SqliteDict(DICT_DB_FILE, tablename='email', flag=flag, autocommit=autocommit)
    return edb

# -----------------------------------------------------------------------------
"""
our "feature store" is currently just a pickle file, may want to consider hdf5 in the future
"""

# stores tfidf features a bunch of other metadata
FEATURES_FILE = os.path.join(DATA_DIR, 'features.p')

def save_features(features):
    """ takes the features dict and save it to disk in a simple pickle file """
    if DATABASE_URL:
        PgDict('blobs', _pickle_codec)['features'] = features
        return
    safe_pickle_dump(features, FEATURES_FILE)

def load_features():
    """ loads the features dict from disk """
    if DATABASE_URL:
        return PgDict('blobs', _pickle_codec)['features']
    with open(FEATURES_FILE, 'rb') as f:
        features = pickle.load(f)
    return features
