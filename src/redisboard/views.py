from functools import partial
from logging import getLogger

from django.conf import settings
from django.core.paginator import Paginator
from django.http import HttpResponseNotFound
from django.shortcuts import render, redirect
from redis.exceptions import ResponseError

from .utils import PY3
from .utils import LazySlicingIterable
from django.contrib import messages


try:
    from django.utils.datastructures import SortedDict as OrderedDict
except ImportError:
    from collections import OrderedDict
logger = getLogger(__name__)

REDISBOARD_ITEMS_PER_PAGE = getattr(settings, 'REDISBOARD_ITEMS_PER_PAGE', 100)


def safeint(value):
    try:
        return int(value)
    except ValueError:
        return value


def _fixup_pair(pair):
    a, b = pair
    return a, safeint(b)


LENGTH_GETTERS = {
    b'list': lambda conn, key: conn.llen(key),
    b'string': lambda conn, key: conn.strlen(key),
    b'set': lambda conn, key: conn.scard(key),
    b'zset': lambda conn, key: conn.zcount(key, '-inf', '+inf'),
    b'hash': lambda conn, key: conn.hlen(key),
}


def _decode_bytes(value):
    if isinstance(value, bytes):
        return value.decode('utf8')
    else:
        return value


def _get_key_info(conn, key):
    try:
        obj_type = conn.type(key)
        length_getter = LENGTH_GETTERS.get(obj_type)
        if not length_getter:
            return {
                'type': 'none',
                'name': key,
                'length': "n/a",
                'error': "The key does not exist",
                'ttl': "n/a",
                'refcount': "n/a",
                'encoding': "n/a",
                'idletime': "n/a",
            }

        pipe = conn.pipeline()

        try:
            pipe.object('REFCOUNT', key)
            pipe.object('ENCODING', key)
            pipe.object('IDLETIME', key)
            length_getter(pipe, key)
            pipe.ttl(key)

            refcount, encoding, idletime, obj_length, obj_ttl = pipe.execute()
        except ResponseError as exc:
            logger.exception("Failed to get object info for key %r: %s", key, exc)
            return {
                'type': obj_type,
                'name': key,
                'length': "n/a",
                'error': str(exc),
                'ttl': "n/a",
                'refcount': "n/a",
                'encoding': "n/a",
                'idletime': "n/a",
            }
        return {
            'type': _decode_bytes(obj_type),
            'name': key,
            'length': obj_length,
            'ttl': obj_ttl,
            'refcount': refcount,
            'encoding': _decode_bytes(encoding),
            'idletime': idletime,
        }
    except ResponseError as exc:
        logger.exception("Failed to get details for key %r: %s", key, exc)
        return {
            'type': "n/a",
            'length': "n/a",
            'name': key,
            'error': str(exc),
            'ttl': "n/a",
            'refcount': "n/a",
            'encoding': "n/a",
            'idletime': "n/a",
        }


VALUE_GETTERS = {
    'list': lambda conn, key, start=0, end=-1: [
        (pos + start, val)
        for (pos, val) in enumerate(conn.lrange(key, start, end))
    ],
    'string': lambda conn, key, *args: [('string', conn.get(key))],
    'set': lambda conn, key, *args: list(enumerate(conn.smembers(key))),
    'zset': lambda conn, key, start=0, end=-1: [
        (pos + start, val)
        for (pos, val) in enumerate(conn.zrange(key, start, end))
    ],
    'hash': lambda conn, key, *args: conn.hgetall(key).items(),
    'n/a': lambda conn, key, *args: (),
    'none': lambda conn, key, *args: (),
}

def _delete_key(request, conn, db, key):
    db_selected = conn.execute_command('SELECT', db)
    if not db_selected:
        messages.error(request, 'DB connection failed')
        return redirect('admin:redisboard_redisserver_changelist')
    deleted = conn.execute_command('DEL', key)
    if not deleted:
        messages.error(request, f'Unable to delete key {key}')
        return redirect('admin:redisboard_redisserver_changelist')
    messages.success(request, f'Key {key} deleted.')
    return redirect('admin:redisboard_redisserver_changelist')


def _get_key_details(conn, db, key, page):
    conn.execute_command('SELECT', db)
    details = _get_key_info(conn, key)
    details['db'] = db
    if details['type'] in ('list', 'zset'):
        details['data'] = Paginator(
            LazySlicingIterable(
                lambda: details['length'],
                partial(VALUE_GETTERS[details['type']], conn, key)
            ),
            REDISBOARD_ITEMS_PER_PAGE
        ).page(page)
    else:
        details['data'] = VALUE_GETTERS[details['type']](conn, key)

    return details


def _raw_get_db_summary(server, db):
    server.connection.execute_command('SELECT', db)
    pipe = server.connection.pipeline()

    pipe.dbsize()
    for i in range(server.sampling_threshold):
        pipe.randomkey()

    results = pipe.execute()
    size = results.pop(0)
    keys = sorted(set(results))

    pipe = server.connection.pipeline()
    for key in keys:
        pipe.execute_command('DEBUG', 'OBJECT', key)
        pipe.ttl(key)

    total_memory = 0
    volatile_memory = 0
    persistent_memory = 0
    total_keys = 0
    volatile_keys = 0
    persistent_keys = 0
    results = pipe.execute()
    for key, details, ttl in zip(keys, results[::2], results[1::2]):
        if not isinstance(details, dict):
            details = dict(_fixup_pair(i.split(b':'))
                           for i in details.split() if b':' in i)

        length = details[b'serializedlength'] + len(key)

        if ttl:
            persistent_memory += length
            persistent_keys += 1
        else:
            volatile_memory += length
            volatile_keys += 1
        total_memory += length
        total_keys += 1

    if total_keys:
        total_memory = (total_memory / total_keys) * size
    else:
        total_memory = 0

    if persistent_keys:
        persistent_memory = (persistent_memory / persistent_keys) * size
    else:
        persistent_memory = 0

    if volatile_keys:
        volatile_memory = (volatile_memory / volatile_keys) * size
    else:
        volatile_memory = 0
    return dict(
        size=size,
        total_memory=total_memory,
        volatile_memory=volatile_memory,
        persistent_memory=persistent_memory,
    )


def _get_db_summary(server, db):
    try:
        return _raw_get_db_summary(server, db)
    except ResponseError as exc:
        logger.exception("Failed to get summary for db %r: %s", db, exc)
        return dict(
            size=0,
            total_memory=0,
            volatile_memory=0,
            persistent_memory=0,
        )


def _get_db_details(server, db):
    conn = server.connection
    conn.execute_command('SELECT', db)
    size = conn.dbsize()

    key_details = {}
    if size > server.sampling_threshold:
        sampling = True
        pipe = conn.pipeline()
        for _ in (range if PY3 else xrange)(server.sampling_size):  # noqa
            pipe.randomkey()

        for key in set(pipe.execute()):
            key_details[key] = _get_key_info(conn, key)

    else:
        sampling = False
        for key in conn.keys():
            key_details[key] = _get_key_info(conn, key)

    return dict(
        keys=key_details,
        sampling=sampling,
    )

def delete(request, server):
    stats = server.stats
    conn = server.connection
    database_details = OrderedDict()
    if stats['status'] == 'UP':
        if 'key' in request.GET:
            key = request.GET['key']
            db = request.GET.get('db', 0)
            return _delete_key(request, conn, db, key)
    else:
        messages.error(request, f'Server is down')
        return redirect('/bomberman/redisboard/redisserver/')

def inspect(request, server):
    stats = server.stats
    conn = server.connection
    database_details = OrderedDict()
    key_details = None
    if stats['status'] == 'UP':
        if 'key' in request.GET:
            key = request.GET['key']
            db = request.GET.get('db', 0)
            page = request.GET.get('page', 1)
            key_details = _get_key_details(conn, db, key, page)
        else:
            databases = sorted(name[2:] for name in conn.info()
                               if name.startswith('db'))
            total_size = 0
            for db in databases:
                database_details[db] = summary = _get_db_summary(server, db)
                total_size += summary['size']
            if total_size < server.sampling_threshold:
                for db in databases:
                    database_details[db].update(
                        _get_db_details(server, db),
                        active=True,
                    )
            elif 'db' in request.GET:
                db = request.GET['db']
                if db in database_details:
                    database_details[db].update(
                        _get_db_details(server, db),
                        active=True,
                    )
                else:
                    return HttpResponseNotFound("Unknown database.")

    return render(request, "redisboard/inspect.html", {
        'databases': database_details,
        'key_details': key_details,
        'original': server,
        'stats': stats,
        'app_label': 'redisboard',
    })
