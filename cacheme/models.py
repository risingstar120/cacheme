from __future__ import annotations

import datetime
from typing import ClassVar, List, Optional, Sequence, Type, cast

from typing_extensions import Any

from cacheme.data import get_storage_by_name
from cacheme.interfaces import Cachable, DoorKeeper, Metrics, Serializer, Storage

_nodes: List[Type[Cachable]] = []


def get_nodes():
    return _nodes


class MetaNode(type):
    def __new__(cls, name, bases, dct):
        new = super().__new__(cls, name, bases, dct)
        internal = getattr(new.Meta, "internal", False)
        if internal == False:
            _nodes.append(cast(Type[Cachable], cls))
            new.Meta.metrics = Metrics()
        return new

    class Meta:
        metrics: ClassVar[Metrics]


class Node(metaclass=MetaNode):
    def key(self) -> str:
        raise NotImplementedError()

    def full_key(self) -> str:
        return f"cacheme:{self.key()}:{self.Meta.version}"

    async def load(self):
        raise NotImplementedError()

    @classmethod
    async def load_all(cls, nodes: Sequence[Cachable]) -> Any:
        data = []
        for node in nodes:
            v = await node.load()
            data.append((node, v))
        return data

    def get_version(self) -> str:
        return self.Meta.version

    def get_stroage(self) -> Storage:
        return get_storage_by_name(self.Meta.storage)

    def get_ttl(self) -> Optional[datetime.timedelta]:
        return self.Meta.ttl

    def get_local_cache(self) -> Optional[Storage]:
        if self.Meta.local_cache is None:
            return None
        return get_storage_by_name(self.Meta.local_cache)

    def get_seriaizer(self) -> Optional[Serializer]:
        return self.Meta.serializer

    def get_doorkeeper(self) -> Optional[DoorKeeper]:
        return self.Meta.doorkeeper

    @classmethod
    def get_metrics(cls) -> Metrics:
        return cls.Meta.metrics

    class Meta:
        version: ClassVar[str] = ""
        storage: ClassVar[str] = ""
        ttl: ClassVar[Optional[datetime.timedelta]] = None
        local_cache: ClassVar[Optional[str]] = None
        serializer: ClassVar[Optional[Serializer]] = None
        doorkeeper: ClassVar[Optional[DoorKeeper]] = None
        metrics: ClassVar[Metrics]
