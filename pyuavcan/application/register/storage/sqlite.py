# Copyright (C) 2021  UAVCAN Consortium  <uavcan.org>
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel@uavcan.org>

from __future__ import annotations
import typing
from pathlib import Path
import logging
import sqlite3
import pyuavcan
from uavcan.register import Value_1_0 as Value
from . import Entry, StorageError, Storage


_TIMEOUT = 0.5
_LOCATION_VOLATILE = ":memory:"


# noinspection SqlNoDataSourceInspection,SqlResolve
class SQLiteStorage(Storage):
    """
    Register storage backend implementation based on SQLite.
    Supports either persistent on-disk single-file storage or volatile in-memory storage.
    """

    def __init__(self, location: typing.Union[str, Path] = ""):
        """
        :param location: Either a path to the database file, or None. If None, the data will be stored in memory.
        """
        self._loc = str(location or _LOCATION_VOLATILE).strip()
        self._db = sqlite3.connect(self._loc, timeout=_TIMEOUT)
        self._execute(
            r"""
            create table if not exists `register` (
                `name`      varchar(255) not null unique primary key,
                `mutable`   boolean not null,
                `value`     blob not null,
                `ts`        time not null default current_timestamp
            )
            """,
            commit=True,
        )
        _logger.debug("%r: Initialized with registers: %r", self, self.get_names())

    @property
    def location(self) -> str:
        return self._loc

    @property
    def persistent(self) -> bool:
        return self._loc.lower() != _LOCATION_VOLATILE

    def count(self) -> int:
        return self._execute(r"select count(*) from register").fetchone()[0]

    def get_names(self) -> typing.List[str]:
        return [x for x, in self._execute(r"select name from register order by name").fetchall()]

    def get_name_at_index(self, index: int) -> typing.Optional[str]:
        try:
            # TODO: this is super inefficient, make a request instead.
            return self.get_names()[index]
        except IndexError:
            return None

    def get(self, name: str) -> typing.Optional[Entry]:
        res = self._execute(r"select mutable, value from register where name = ?", name).fetchone()
        if res is None:
            _logger.debug("%r: Get %r -> (nothing)", self, name)
            return None
        mutable, value = res
        assert isinstance(value, bytes)
        obj = pyuavcan.dsdl.deserialize(Value, [memoryview(value)])
        if obj is None:  # pragma: no cover
            _logger.warning("%r: Value of %r is not a valid serialization of %s: %r", self, name, Value, value)
        e = Entry(value=obj, mutable=bool(mutable))
        _logger.debug("%r: Get %r -> %r", self, name, e)
        return e

    def set(self, name: str, e: Entry) -> None:
        _logger.debug("%r: Set %r <- %r", self, name, e)
        serialized = b"".join(pyuavcan.dsdl.serialize(e.value))
        # language=SQLite
        self._execute(
            r"insert or replace into register (name, mutable, value) values (?, ?, ?)",
            name,
            e.mutable,
            serialized,
            commit=True,
        )

    def delete(self, names: typing.Sequence[str]) -> None:
        _logger.debug("%r: Delete %r", self, names)
        try:
            self._db.executemany(r"delete from register where name = ?", ((x,) for x in names))
            self._db.commit()
        except sqlite3.OperationalError as ex:
            raise StorageError(f"Could not delete {len(names)} registers: {ex}")

    def close(self) -> None:
        _logger.debug("%r: Closing", self)
        self._db.close()

    def _execute(self, statement: str, *params: typing.Any, commit: bool = False) -> sqlite3.Cursor:
        try:
            cur = self._db.execute(statement, params)
            if commit:
                self._db.commit()
            return cur
        except sqlite3.OperationalError as ex:
            raise StorageError(f"Database transaction has failed: {ex}") from ex


_logger = logging.getLogger(__name__)


def _unittest_memory() -> None:
    from uavcan.primitive import String_1_0 as String, Unstructured_1_0 as Unstructured

    st = SQLiteStorage()
    print(st)
    assert not st.get_names()
    assert not st.get_name_at_index(0)
    assert None is st.get("foo")
    assert st.count() == 0
    st.delete(["foo"])

    st.set("foo", Entry(Value(string=String("Hello world!")), mutable=False))
    e = st.get("foo")
    assert e
    assert e.value.string
    assert e.value.string.value.tobytes().decode() == "Hello world!"
    assert not e.mutable
    assert st.count() == 1

    # Override the same register.
    st.set("foo", Entry(Value(unstructured=Unstructured([1, 2, 3])), mutable=True))
    e = st.get("foo")
    assert e
    assert e.value.unstructured
    assert e.value.unstructured.value.tobytes() == b"\x01\x02\x03"
    assert e.mutable
    assert st.count() == 1

    assert ["foo"] == st.get_names()
    assert "foo" == st.get_name_at_index(0)
    assert None is st.get_name_at_index(1)
    st.delete(["baz"])
    assert ["foo"] == st.get_names()
    st.delete(["foo", "baz"])
    assert [] == st.get_names()
    assert st.count() == 0

    st.close()


def _unittest_file() -> None:
    import tempfile
    from uavcan.primitive import Unstructured_1_0 as Unstructured

    # First, populate the database with registers.
    db_file = tempfile.mktemp(".db")
    print("DB file:", db_file)
    st = SQLiteStorage(db_file)
    print(st)
    st.set("mutable", Entry(Value(unstructured=Unstructured([1, 2, 3])), mutable=True))
    st.set("immutable", Entry(Value(unstructured=Unstructured([4, 5, 6])), mutable=False))
    assert st.count() == 2
    st.close()

    # Then re-open it in writeable mode and ensure correctness.
    st = SQLiteStorage(db_file)
    print(st)
    assert st.count() == 2
    e = st.get("mutable")
    assert e
    assert e.value.unstructured
    assert e.value.unstructured.value.tobytes() == b"\x01\x02\x03"
    assert e.mutable

    e = st.get("immutable")
    assert e
    assert e.value.unstructured
    assert e.value.unstructured.value.tobytes() == b"\x04\x05\x06"
    assert not e.mutable
    st.close()