import time
import pickle
import datetime
import logging

from functools import wraps
from inspect import _signature_from_function, Signature

from .utils import CachemeUtils


logger = logging.getLogger('cacheme')


cacheme_tags = dict()


class CacheMe(object):
    connection_set = False
    settings_set = False
    utils = None

    @classmethod
    def set_connection(cls, connection):
        cls.conn = connection
        cls.connection_set = True

    @classmethod
    def update_settings(cls, settings):
        cls.CACHEME = cls.merge_settings(settings)
        cls.settings_set = True

    @classmethod
    def merge_settings(cls, settings):
        CACHEME = {
            'ENABLE_CACHE': True,
            'REDIS_CACHE_PREFIX': 'CM:',  # key prefix for cache
            'REDIS_CACHE_SCAN_COUNT': 10,
            'REDIS_URL': 'redis://localhost:6379/0',
            'THUNDERING_HERD_RETRY_COUNT': 5,
            'THUNDERING_HERD_RETRY_TIME': 20
        }

        CACHEME.update(settings)
        return type('CACHEME', (), CACHEME)

    def __init__(self, key, invalid_keys=None, hit=None, miss=None, tag=None, skip=False, timeout=None, invalid_signals=None):

        if not self.connection_set:
            raise Exception('No connection find, please use set_connection first!')
        if not self.settings_set:
            self.update_settings({})
            logger.warning('No custom settings found, use default.')

        self.__class__.utils = CachemeUtils(self.CACHEME, self.conn)

        self.key_prefix = self.CACHEME.REDIS_CACHE_PREFIX
        self.deleted = self.key_prefix + 'delete'

        if not self.CACHEME.ENABLE_CACHE:
            return
        self.key = key
        self.invalid_keys = invalid_keys
        self.hit = hit
        self.miss = miss
        self.tag = tag
        self.skip = skip
        self.timeout = timeout
        self.progress_key = self.key_prefix + 'progress'
        self.invalid_signals = invalid_signals

        self.conn = self.conn
        self.link()

    def __call__(self, func):

        self.function = func

        self.tag = self.tag or func.__name__
        cacheme_tags[self.tag] = self

        @wraps(func)
        def wrapper(*args, **kwargs):
            if not self.CACHEME.ENABLE_CACHE:
                return self.function(*args, **kwargs)

            # bind args and kwargs to true function params
            signature = _signature_from_function(Signature, func)
            bind = signature.bind(*args, **kwargs)
            bind.apply_defaults()

            # then apply args and kwargs to a container,
            # in this way, we can have clear lambda with just one
            # argument, and access what we need from this container
            self.container = type('Container', (), bind.arguments)

            if callable(self.skip) and self.skip(self.container):
                return self.function(*args, **kwargs)
            elif self.skip:
                return self.function(*args, **kwargs)

            key = self.key_prefix + self.key(self.container)

            if self.timeout:
                result = self.get_key(key)

            if self.conn.srem(self.deleted, key):
                result = self.function(*args, **kwargs)
                self.set_result(key, result)
                self.container.cacheme_result = result
                self.add_to_invalid_list(key, args, kwargs)
                return result

            if self.timeout is None:
                result = self.get_key(key)

            if result is None:

                if self.add_to_progress(key) == 0:  # already in progress
                    for i in range(self.CACHEME.THUNDERING_HERD_RETRY_COUNT):
                        time.sleep(self.CACHEME.THUNDERING_HERD_RETRY_TIME/1000)
                        result = self.get_key(key)
                        if result:
                            return result

                result = self.get_result_from_func(args, kwargs, key)
                self.set_result(key, result)
                self.remove_from_progress(key)
                self.container.cacheme_result = result
                self.add_to_invalid_list(key, args, kwargs)
            else:
                if self.hit:
                    self.hit(key, result, self.container)
                result = result

            self.container = None
            return result

        return wrapper

    @property
    def keys(self):
        return self.conn.smembers(self.CACHEME.REDIS_CACHE_PREFIX + self.tag)

    @keys.setter
    def keys(self, val):
        self.conn.sadd(self.CACHEME.REDIS_CACHE_PREFIX + self.tag, val)

    def invalid_all(self):
        keys = self.keys
        if not keys:
            return
        self.conn.sadd(self.deleted, *keys)
        self.conn.unlink(self.CACHEME.REDIS_CACHE_PREFIX + self.tag)

    def get_result_from_func(self, args, kwargs, key):
        if self.miss:
            self.miss(key, self.container)

        start = datetime.datetime.now()
        result = self.function(*args, **kwargs)
        end = datetime.datetime.now()
        delta = (end - start).total_seconds() * 1000
        logger.debug(
            '[CACHEME FUNC LOG] key: "%s", time: %s ms' % (key, delta)
        )
        return result

    def set_result(self, key, result):
        self.set_key(key, result)

    def get_key(self, key):
        key, field = self.utils.split_key(key)
        if self.timeout:
            result = self.utils.hget_with_ttl(key, field)
        else:
            result = self.conn.hget(key, field)

        if result:
            result = pickle.loads(result)
        return result

    def set_key(self, key, value):
        self.keys = key
        value = pickle.dumps(value)
        key, field = self.utils.split_key(key)
        if self.timeout:
            self.utils.hset_with_ttl(key, field, value, self.timeout)
        else:
            self.conn.hset(key, field, value)

    def push_key(self, key, value):
        return self.conn.sadd(key, value)

    def add_to_invalid_list(self, key, args, kwargs):
        invalid_keys = self.invalid_keys

        if not invalid_keys:
            return

        invalid_keys = invalid_keys(self.container)
        invalid_keys = self.utils.flat_list(invalid_keys)
        for invalid_key in set(filter(lambda x: x is not None, invalid_keys)):
            invalid_key += ':invalid'
            invalid_key = self.key_prefix + invalid_key
            self.push_key(invalid_key, key)

    def link(self):
        pass

    def remove_from_progress(self, key):
        self.conn.srem(self.progress_key, key)

    def add_to_progress(self, key):
        return self.conn.sadd(self.progress_key, key)
