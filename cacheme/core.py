import types
from asyncio import Lock
from datetime import datetime, timezone
from time import time_ns
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Optional,
    OrderedDict,
    Sequence,
    Type,
    TypeVar,
    cast,
    overload,
)


from typing_extensions import ParamSpec, Self

from cacheme.interfaces import Cachable, Memoizable, CachedData
from cacheme.models import TagNode
from cacheme.serializer import MsgPackSerializer, PickleSerializer
from cacheme.data import get_tag_storage


C = TypeVar("C")
C_co = TypeVar("C_co", covariant=True)
P = ParamSpec("P")
R = TypeVar("R")


class Locker:
    lock: Lock
    value: Any

    def __init__(self):
        self.lock = Lock()
        self.value = None


_lockers: Dict[str, Locker] = {}


# local storage(if enable) -> storage -> cache miss, load from source
async def get(node: Cachable[C_co]) -> C_co:
    storage = node.get_stroage()
    metrics = node.get_metrics()
    result = None
    local_storage = node.get_local_cache()
    if local_storage is not None:
        result = await local_storage.get(node, None)
    if result is None:
        result = await storage.get(node, node.get_seriaizer())
    # get result from cache, check tags
    if result is not None and len(node.tags()) > 0:
        tag_storage = get_tag_storage()
        valid = await tag_storage.validate_tags(
            result,
        )
        if not valid:
            await storage.remove(node)
            result = None
    if result is None:
        metrics.miss_count += 1
        locker = _lockers.setdefault(node.full_key(), Locker())
        async with locker.lock:
            if locker.value is None:
                now = time_ns()
                try:
                    loaded = await node.load()
                except Exception as e:
                    metrics.load_failure_count += 1
                    metrics.total_load_time += time_ns() - now
                    raise (e)
                locker.value = loaded
                metrics.load_success_count += 1
                metrics.total_load_time += time_ns() - now
                result = CachedData(
                    data=loaded, node=node, updated_at=datetime.now(timezone.utc)
                )
                doorkeeper = node.get_doorkeeper()
                if doorkeeper is not None:
                    exist = doorkeeper.contains(node.full_key())
                    if not exist:
                        doorkeeper.put(node.full_key())
                        return cast(C_co, result)
                await storage.set(node, loaded, node.get_ttl(), node.get_seriaizer())
                if local_storage is not None:
                    await local_storage.set(node, loaded, node.get_ttl(), None)
                _lockers.pop(node.full_key(), None)
            else:
                result = CachedData(
                    data=locker.value, node=node, updated_at=datetime.now(timezone.utc)
                )
    else:
        metrics.hit_count += 1

    return cast(C_co, result.data)


async def invalid_tag(tag: str):
    storage = get_tag_storage()
    await storage.set(TagNode(tag), None, ttl=None, serializer=TagNode.Meta.serializer)


class Wrapper(Generic[P, R]):
    def __init__(self, fn: Callable[P, Awaitable[R]], node: Type[Memoizable]):
        self.func = fn

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        node = self.key_func(*args, **kwargs)
        node = cast(Cachable, node)

        # inline load function
        async def load() -> Any:
            return await self.func(*args, **kwargs)

        node.load = load  # type: ignore
        return await get(node)

    def to_node(self, fn: Callable[P, Memoizable]) -> Self:  # type: ignore
        self.key_func = fn
        return self

    @overload
    def __get__(self, instance, owner) -> Callable[..., R]:
        ...

    @overload
    def __get__(self, instance, owner) -> Self:  # type: ignore
        ...

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return cast(Callable[..., R], types.MethodType(self, instance))


class Memoize:
    def __init__(self, node: Type[Memoizable]):
        self.node = node

    def __call__(self, fn: Callable[P, Awaitable[R]]) -> Wrapper[P, R]:
        return Wrapper(fn, self.node)


class NodeSet:
    def __init__(self, nodes: Sequence[Cachable]):
        self.hashmap: Dict[int, Cachable] = {}
        for node in nodes:
            self.hashmap[node.key_hash()] = node

    def remove(self, node: Cachable):
        self.hashmap.pop(node.key_hash(), None)

    @property
    def list(self) -> Sequence[Cachable]:
        return tuple(self.hashmap.values())

    def __len__(self):
        return len(self.hashmap)


async def get_all(nodes: Sequence[Cachable[C]]) -> Sequence[C]:
    if len(nodes) == 0:
        return tuple()
    node_cls = nodes[0].__class__
    s: OrderedDict[int, Optional[C]] = OrderedDict()
    for node in nodes:
        if node.__class__ != node_cls:
            raise Exception(
                f"node class mismatch: expect [{node_cls}], get [{node.__class__}]"
            )
        s[node.key_hash()] = None
    pending_nodes = NodeSet(nodes)
    storage = nodes[0].get_stroage()
    metrics = nodes[0].get_metrics()
    local_storage = nodes[0].get_local_cache()
    serializer = nodes[0].get_seriaizer()
    ttl = nodes[0].get_ttl()
    if local_storage is not None:
        cached = await local_storage.get_all(nodes, serializer)
        for k, v in cached:
            pending_nodes.remove(k)
            s[k.key_hash()] = cast(C, v.data)
    cached = await storage.get_all(pending_nodes.list, serializer)
    for k, v in cached:
        pending_nodes.remove(k)
        s[k.key_hash()] = cast(C, v.data)
    metrics.miss_count += len(pending_nodes)
    now = time_ns()
    try:
        ns = cast(Sequence[Cachable], pending_nodes.list)
        loaded = await node_cls.load_all(ns)
    except Exception as e:
        metrics.load_failure_count += len(pending_nodes)
        metrics.total_load_time += time_ns() - now
        raise (e)
    metrics.load_success_count += len(pending_nodes)
    metrics.total_load_time += time_ns() - now
    if local_storage is not None:
        await local_storage.set_all(loaded, ttl, serializer)
    await storage.set_all(loaded, ttl, serializer)
    for node, value in loaded:
        s[node.key_hash()] = cast(C, value)
    return cast(Sequence[C], tuple(s.values()))