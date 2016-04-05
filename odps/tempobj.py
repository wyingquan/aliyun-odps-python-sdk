#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import os
import platform
import sys
import subprocess
import glob
import tempfile
import json
import time
import uuid
import hashlib
import threading
import traceback
from multiprocessing.pool import ThreadPool

from six import iteritems, string_types, itervalues

from .compat import pickle
from .config import options
from .errors import NoSuchObject
from .utils import is_main_process, build_pyodps_dir

TEMP_ROOT = build_pyodps_dir('tempobjs')
SESSION_KEY = '%d_%s' % (int(time.time()), uuid.uuid4())
CLEANER_THREADS = 100

CLEANUP_SCRIPT_TMPL = """
import os
import sys
import json

try:
    os.unlink(os.path.realpath(__file__))
except Exception:
    pass

temp_codes = json.loads(r\"\"\"
{odps_info}
\"\"\".strip())
import_paths = json.loads(r\"\"\"
{import_paths}
\"\"\".strip())
biz_ids = json.loads(r\"\"\"
{biz_ids}
\"\"\".strip())

sys.path.extend(import_paths)
from odps import ODPS, tempobj

try:
    import pydevd
    tempobj.cleanup_timeout = None
except:
    tempobj.cleanup_timeout = 5
tempobj.cleanup_mode = True
tempobj.host_pid = {host_pid}
tempobj.biz_ids = set(biz_ids)

for o_desc in temp_codes:
    ODPS(**o_desc)
os._exit(0)
"""


cleanup_mode = False
cleanup_timeout = 0
host_pid = os.getpid()
biz_ids = set([options.biz_id, ]) if options.biz_id else set(['default', ])


class ExecutionEnv(object):
    def __init__(self, **kwargs):
        global biz_ids

        self.os = os
        self.sys = sys
        self.biz_ids = biz_ids
        self.json = json
        self.import_paths = sys.path
        self.subprocess = subprocess
        self.temp_dir = tempfile.gettempdir()
        self.template = CLEANUP_SCRIPT_TMPL
        self.traceback = traceback
        self.is_main_process = is_main_process()
        for k, v in iteritems(kwargs):
            setattr(self, k, v)


class TempObject(object):
    _keys = []
    _type = ''

    def __hash__(self):
        if self._keys:
            return hash(tuple(getattr(self, k) for k in self._keys))
        return super(TempObject, self).__hash__()

    def __eq__(self, other):
        if not isinstance(other, TempObject):
            return False
        if self._type != other._type:
            return False
        return all(getattr(self, k) == getattr(other, k) for k in self._keys)


class TempTable(TempObject):
    _keys = 'table', 'project'
    _type = 'Table'

    def __init__(self, table, project):
        self.table = table
        self.project = project

    def drop(self, odps):
        odps.run_sql('drop table if exists %s' % self.table, project=self.project)


class TempModel(TempObject):
    _keys = 'model', 'project'
    _type = 'OfflineModel'

    def __init__(self, model, project):
        self.model = model
        self.project = project

    def drop(self, odps):
        try:
            odps.delete_offline_model(self.model, self.project)
        except NoSuchObject:
            pass


class ObjectRepository(object):
    def __init__(self, file_name):
        self._container = set()
        self._file_name = file_name
        if os.path.exists(file_name):
            self.load()

    def put(self, obj, dump=True):
        self._container.add(obj)
        if dump:
            self.dump()

    def cleanup(self, odps):
        cleaned = []

        def cleaner_thread(obj):
            try:
                obj.drop(odps)
                cleaned.append(obj)
            except:
                pass

        pool = ThreadPool(CLEANER_THREADS)
        if self._container:
            pool.map(cleaner_thread, self._container)
            pool.close()
            pool.join()
        for obj in cleaned:
            if obj in self._container:
                self._container.remove(obj)
        if not self._container:
            os.unlink(self._file_name)
        else:
            self.dump()

    def dump(self):
        with open(self._file_name, 'wb') as outf:
            pickle.dump(list(self._container), outf, protocol=0)
            outf.close()

    def load(self):
        try:
            with open(self._file_name, 'rb') as inpf:
                contents = pickle.load(inpf)
            self._container.update(contents)
        except EOFError:
            pass


class ObjectRepositoryLib(dict):
    def __init__(self, *args, **kwargs):
        super(ObjectRepositoryLib, self).__init__(*args, **kwargs)
        self._env = ExecutionEnv(odps_info=_odps_info)

    def __del__(self):
        global cleanup_mode
        if cleanup_mode or not self._env.is_main_process:
            return
        self._exec_cleanup_script()

    def _exec_cleanup_script(self):
        if not self:
            return

        env = self._env
        import_paths = json.dumps(env.import_paths)
        odps_info_json = json.dumps(list(itervalues(env.odps_info)))
        biz_ids_json = json.dumps(list(self._env.biz_ids))
        script = env.template.format(import_paths=import_paths, odps_info=odps_info_json, host_pid=env.os.getpid(),
                                     biz_ids=biz_ids_json)

        script_name = env.temp_dir + env.os.sep + 'tmp_' + str(env.os.getpid()) + '_cleanup_script.py'
        with open(script_name, 'w') as script_file:
            script_file.write(script)
            script_file.close()
        env.subprocess.call([env.sys.executable, script_name], close_fds=True)


_odps_info = dict()
_cleaned_keys = set()
_obj_repos = ObjectRepositoryLib()  # this line should be put last due to initialization dependency


def _is_pid_running(pid):
    if 'windows' in platform.platform().lower():
        task_lines = os.popen('TASKLIST /FI "PID eq {0}" /NH'.format(pid)).read().strip().splitlines()
        if not task_lines:
            return False
        return str(pid) in set(task_lines[0].split())
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def clean_objects(odps):
    global cleanup_timeout, host_pid

    if not is_main_process():
        return

    odps_key = _gen_repository_key(odps)
    if odps_key in _cleaned_keys:
        return
    _cleaned_keys.add(odps_key)

    files = []
    for biz_id in biz_ids:
        files.extend(glob.glob(os.path.join(TEMP_ROOT, biz_id, odps_key, '*.his')))

    def clean_thread():
        for fn in files:
            writer_pid = int(fn.rsplit('__', 1)[-1].split('.', 1)[0])

            # we do not clean running process, unless its pid equals host_pid
            if writer_pid != host_pid and _is_pid_running(writer_pid):
                continue

            repo = ObjectRepository(fn)
            repo.cleanup(odps)

    thread_obj = threading.Thread(target=clean_thread)
    thread_obj.start()
    if cleanup_timeout == 0:
        return
    else:
        if cleanup_timeout < 0:
            cleanup_timeout = None
        thread_obj.join(cleanup_timeout)


def _gen_repository_key(odps):
    return hashlib.md5('####'.join([odps.account.access_id, odps.account.secret_access_key, odps.endpoint,
                                    odps.project]).encode('utf-8')).hexdigest()


def _put_objects(odps, objs):
    global biz_ids
    odps_key = _gen_repository_key(odps)

    biz_id = options.biz_id if options.biz_id else 'default'
    biz_ids.add(biz_id)
    if odps not in _obj_repos:
        _odps_info[odps_key] = dict(access_id=odps.account.access_id, secret_access_key=odps.account.secret_access_key,
                                    project=odps.project, endpoint=odps.endpoint)
        file_dir = os.path.join(TEMP_ROOT, biz_id, odps_key)
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)
        file_name = os.path.join(file_dir, 'temp_objs_{0}__{1}.his'.format(SESSION_KEY, os.getpid()))
        _obj_repos[odps_key] = ObjectRepository(file_name)
    [_obj_repos[odps_key].put(o, False) for o in objs]
    _obj_repos[odps_key].dump()


def register_temp_table(odps, table, project=None):
    if isinstance(table, string_types):
        table = [table, ]
    _put_objects(odps, [TempTable(t, project if project else odps.project) for t in table])


def register_temp_model(odps, model, project=None):
    if isinstance(model, string_types):
        model = [model, ]
    _put_objects(odps, [TempModel(m, project if project else odps.project) for m in model])