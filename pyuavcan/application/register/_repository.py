# Copyright (C) 2021  UAVCAN Consortium  <uavcan.org>
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel@uavcan.org>

from __future__ import annotations
from fnmatch import fnmatchcase
import typing
import logging
import pyuavcan
from .storage import Storage, Entry, Value
from ._primitives import RelaxedValue, convert


PrimitiveType = typing.TypeVar("PrimitiveType", bound=pyuavcan.dsdl.CompositeObject)


class ConflictError(ValueError):
    pass


class MissingRegisterError(KeyError):
    pass


class Repository:
    def __init__(self, backend: Storage) -> None:
        """
        :param backend: The storage backend to store data in. The persistency flag is inherited from it.

        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> Repository(SQLiteStorage()).persistent
        False
        >>> import tempfile
        >>> rs = Repository(SQLiteStorage(tempfile.mktemp(".db")))
        >>> rs.persistent
        True
        >>> rs.close()
        """
        self._storage = backend

    @property
    def persistent(self) -> bool:
        """True if the storage backend is persistent, False if it is in-memory."""
        return self._storage.persistent

    def close(self) -> None:
        """Closes the file handles related to the storage."""
        self._storage.close()

    def keys(self) -> typing.List[str]:
        """
        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> rs.create("b", Value())
        >>> rs.create("a", Value())
        >>> rs.keys()  # Sorted lexicographically.
        ['a', 'b']
        """
        return self._storage.get_names()

    def get_name_at_index(self, index: int) -> typing.Optional[str]:
        """
        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> rs.create("foo", Value())
        >>> rs.get_name_at_index(0)
        'foo'
        >>> rs.get_name_at_index(1) is None
        True
        """
        return self._storage.get_name_at_index(index)

    def get(self, name: str) -> typing.Optional[Entry]:
        """
        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> from uavcan.primitive.array import Bit_1_0
        >>> rs = Repository(SQLiteStorage())
        >>> rs.get("foo") is None                       # No such register --> None.
        True
        >>> rs.create("foo", Value(bit=Bit_1_0([True, False])))
        >>> e = rs.get("foo")
        >>> not e.value.empty                           # Detect the type by querying the union fields.
        True
        >>> not e.value.string                          # etc...
        True
        >>> e.value.bit.value[0], e.value.bit.value[1]  # The value is a standard NumPy array.
        (True, False)
        """
        return self._storage.get(name)

    def set(self, name: str, value: RelaxedValue) -> None:
        """
        Set if the register exists and the type of the value is matching or can be converted to the register's type.
        The mutability flag is ignored.

        :raises: :class:`MissingRegisterError` (subclass of :class:`KeyError`) if the register does not exist.
                 :class:`ConflictError` if the register exists but the value cannot be converted to its type.

        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> rs.set("foo", True)                      # No such register, will fail. # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        MissingRegisterError: 'foo'
        >>> from uavcan.primitive.array import Bit_1_0
        >>> rs.create("foo", Value(bit=Bit_1_0([True])))    # Create explicitly.
        >>> rs.get("foo").value.bit.value[0]                # Yup, created.
        True
        >>> rs.set("foo", False)                            # Now it can be set.
        >>> rs.get("foo").value.bit.value[0]
        False
        >>> rs.set("foo", [True, False])                    # Wrong dimensionality. # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        ConflictError: ...
        """
        e = self._storage.get(name)
        if not e:
            raise MissingRegisterError(name)
        converted = convert(e.value, value)
        if not converted:
            raise ConflictError(f"Cannot assign {e.value!r} from {value!r}")
        self._storage.set(name, Entry(converted, mutable=e.mutable))

    def create(self, name: str, value: Value, *, mutable: bool = True) -> None:
        """
        If the register exists, behaves like :meth:`set` and the flags are ignored. Otherwise it is created.
        """
        try:
            self.set(name, value)
        except MissingRegisterError:
            self._storage.set(name, Entry(value, mutable=mutable))

    def access(self, name: str, value: Value) -> Entry:
        """
        Perform the set/get transaction as defined by the RPC-service ``uavcan.register.Access``.
        No exceptions are raised. This method is intended for use with RPC-service implementations.

        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> bool(rs.access("foo", Value()).value.empty)                       # No such register.
        True
        >>> from uavcan.primitive.array import Bit_1_0
        >>> rs.create("foo", Value(bit=Bit_1_0([True])))
        >>> rs.access("foo", Value()).value.bit.value[0]                      # Read access.
        True
        >>> rs.access("foo", Value(bit=Bit_1_0([False]))).value.bit.value[0]  # Write access.
        False
        """
        e = self._storage.get(name)
        if not e:
            return Entry(Value(), mutable=False)
        converted = convert(e.value, value)
        if e.mutable and converted:
            e = Entry(converted, mutable=e.mutable)
            self._storage.set(name, e)
        return e  # No point querying the storage again, just return the local value.

    def delete(self, wildcard: str) -> None:
        """
        Remove all registers that match the specified wildcard. Matching is case-sensitive.

        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> rs.create("foo.bar", Value())
        >>> rs.create("foo.baz", Value())
        >>> rs.create("zoo.bar", Value())
        >>> rs.delete("foo.*")
        >>> rs.keys()
        ['zoo.bar']
        """
        names = [n for n in self.keys() if fnmatchcase(n, wildcard)]
        _logger.debug("Deleting %d registers matching %r: %r", len(names), wildcard, names)
        self._storage.delete(names)

    def __getitem__(self, item: str) -> Entry:
        """
        Like :meth:`get`, but if the register is missing it raises :class:`MissingRegisterError`
        (subclass of :class:`KeyError`) instead of returning None.

        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> from uavcan.primitive.array import Bit_1_0
        >>> rs = Repository(SQLiteStorage())
        >>> rs["foo"]                                           # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        MissingRegisterError: 'foo'
        >>> rs.create("foo", Value(bit=Bit_1_0([True])))
        >>> rs["foo"].value.bit.value[0]
        True
        """
        e = self.get(item)
        if e is None:
            raise MissingRegisterError(item)
        return e

    def __iter__(self) -> typing.Iterator[str]:
        """
        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> rs.create("b", Value())
        >>> rs.create("a", Value())
        >>> list(rs)
        ['a', 'b']
        """
        return iter(self.keys())

    def __len__(self) -> int:
        """
        >>> from pyuavcan.application.register.storage.sqlite import SQLiteStorage
        >>> rs = Repository(SQLiteStorage())
        >>> rs.create("b", Value())
        >>> rs.create("a", Value())
        >>> len(rs)
        2
        """
        return self._storage.count()

    def __repr__(self) -> str:
        return pyuavcan.util.repr_attributes(self, self._storage, count=len(self))


_logger = logging.getLogger(__name__)