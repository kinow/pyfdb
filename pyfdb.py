#
# Copyright 2017-2018 European Centre for Medium-Range Weather Forecasts (ECMWF).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import Iterable

import cffi
import os
import platform
from pkg_resources import parse_version

__version__ = '0.0.1'

__fdb_version__ = "5.6.0"

ffi = cffi.FFI()


class FDBException(RuntimeError):
    pass


class PatchedLib:
    """
    Patch a CFFI library with error handling

    Finds the header file associated with the FDB C API and parses it, loads the shared library,
    and patches the accessors with automatic python-C error handling.
    """
    __type_names = {}

    def __init__(self):

        ffi.cdef(self.__read_header())
        libName = {
            'Linux': 'libfdb5.so',
            'Darwin': 'libfdb5.dylib',
            'Windows': 'libfdb5.dll'
        }
        self.__lib = ffi.dlopen(libName[platform.system()])

        # Todo: Version check against __version__

        # All of the executable members of the CFFI-loaded library are functions in the FDB
        # C API. These should be wrapped with the correct error handling. Otherwise forward
        # these on directly.

        for f in dir(self.__lib):
            try:
                attr = getattr(self.__lib, f)
                setattr(self, f, self.__check_error(attr, f) if callable(attr) else attr)
            except Exception as e:
                print(e)
                print("Error retrieving attribute", f, "from library")

        # Initialise the library, and sett it up for python-appropriate behaviour

        self.fdb_initialise_api()

        # Check the library version

        tmp_str = ffi.new('char**')
        self.fdb_version(tmp_str)
        versionstr = ffi.string(tmp_str[0]).decode('utf-8')

        if parse_version(versionstr) < parse_version(__fdb_version__):
            raise RuntimeError("Version of libfdb found is too old. {} < {}".format(versionstr, __fdb_version__))

    #    def type_name(self, typ: 'DataType'):
    #        name = self.__type_names.get(typ, None)
    #        if name is not None:
    #            return name

    #        name_tmp = ffi.new('char**')
    #        self.odc_column_type_name(typ, name_tmp)
    #        name = ffi.string(name_tmp[0]).decode('utf-8')
    #        self.__type_names[typ] = name
    #        return name

    def __read_header(self):
        with open(os.path.join(os.path.dirname(__file__), 'processed_fdb.h'), 'r') as f:
            return f.read()

    def __check_error(self, fn, name):
        """
        If calls into the ODC library return errors, ensure that they get detected and reported
        by throwing an appropriate python exception.
        """

        def wrapped_fn(*args, **kwargs):
            retval = fn(*args, **kwargs)
            if retval != self.__lib.FDB_SUCCESS:
                error_str = "Error in function {}: {}".format(name, self.__lib.fdb_error_string(retval))
                raise FDBException(error_str)
            return retval

        return wrapped_fn


# Bootstrap the library

lib = PatchedLib()


# Construct lookups/constants as is useful

# @unique
# class DataType(IntEnum):
#    IGNORE = lib.ODC_IGNORE
#    INTEGER = lib.ODC_INTEGER
#    DOUBLE = lib.ODC_DOUBLE
#    REAL = lib.ODC_REAL
#    STRING = lib.ODC_STRING
#    BITFIELD = lib.ODC_BITFIELD

# IGNORE = DataType.IGNORE
# INTEGER = DataType.INTEGER
# REAL = DataType.REAL
# STRING = DataType.STRING
# BITFIELD = DataType.BITFIELD
# DOUBLE = DataType.DOUBLE

class Key:
    __key = None

    def __init__(self, keys):
        key = ffi.new('fdb_key_t**')
        lib.fdb_key_init(key)

        # Set free function
        self.__key = ffi.gc(key[0], lib.fdb_key_clean)

        for k, v in keys.items():
            self.set(k, v)

    def set(self, k, v):
        lib.fdb_key_add(self.__key, ffi.new('char[]', k.encode('ascii')), ffi.new('char[]', v.encode('ascii')))

    @property
    def ctype(self):
        return self.__key


class Request:
    __request = None

    def __init__(self, request):
        newrequest = ffi.new('fdb_request_t**')

        # we assume a retrieve request represented as a dictionary
        lib.fdb_request_init(newrequest)
        self.__request = ffi.gc(newrequest[0], lib.fdb_request_clean)

        for name, values in request.items():
            self.value(name, values)

    def value(self, name, values):
        if name and name != 'verb':
            cvals = []
            if isinstance(values, (str, int)):
                values = [values]
            for value in values:
                if isinstance(value, int):
                    value = str(value)
                cval = ffi.new("char[]", value.encode('ascii'))
                cvals.append(cval)

            lib.fdb_request_add(self.__request,
                                ffi.new('char[]', name.encode('ascii')),
                                ffi.new('char*[]', cvals), len(values))

    @property
    def ctype(self):
        return self.__request


class ListIterator:
    __iterator = None
    __seenKeys = []

    def __init__(self, fdb, request):
        iterator = ffi.new("fdb_listiterator_t**")
        lib.fdb_listiterator_init(iterator)
        self.__iterator = ffi.gc(iterator[0], lib.fdb_listiterator_clean)

        if request:
            lib.fdb_list(fdb.ctype, Request(request).ctype, self.__iterator)
        else:
            lib.fdb_list(fdb.ctype, ffi.NULL, self.__iterator)

        self.__seenKeys = []

    def __iter__(self):
        elstr = ffi.new("char[200]")

        exist = True
        while exist:
            cexist = ffi.new("bool*")
            lib.fdb_listiterator_next(self.__iterator, cexist, elstr, 200)
            exist = cexist[0]
            if exist:
                out = ffi.string(elstr).decode('ascii')
                if out not in self.__seenKeys:
                    self.__seenKeys.append(out)
                    yield out


class DataRetriever:
    __dataread = None
    __opened = False

    def __init__(self, fdb, request):
        dataread = ffi.new('fdb_datareader_t **')
        lib.fdb_datareader_init(dataread)
        self.__dataread = ffi.gc(dataread[0], lib.fdb_datareader_clean)

        lib.fdb_retrieve(fdb.ctype, Request(request).ctype, self.__dataread)

    mode = 'rb'

    def open(self):
        if not self.__opened:
            self.__opened = True
            lib.fdb_datareader_open(self.__dataread)

    def close(self):
        if self.__opened:
            self.__opened = False
            lib.fdb_datareader_close(self.__dataread)

    def skip(self, count):
        self.open()
        if isinstance(count, int):
            lib.fdb_datareader_skip(self.__dataread, count)

    def seek(self, where):
        self.open()
#        print('seek', where)
        if isinstance(where, int):
            lib.fdb_datareader_seek(self.__dataread, where)

    def tell(self):
        self.open()
        where = ffi.new("long*")
        lib.fdb_datareader_tell(self.__dataread, where)
        return where[0]

    def read(self, count):
        self.open()
        if isinstance(count, int):
            buf = bytearray(count)
            read = ffi.new('long*')
            lib.fdb_datareader_read(self.__dataread, ffi.from_buffer(buf), count, read)
            return buf[0:read[0]]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        None


class FDB:
    """This is the main container class for accessing FDB"""
    __fdb = None

    def __init__(self):
        fdb = ffi.new('fdb_t**')
        lib.fdb_init(fdb)

        # Set free function
        self.__fdb = ffi.gc(fdb[0], lib.fdb_clean)

    def archive(self, key, data):
        lib.fdb_archive(self.ctype, Key(key).ctype, ffi.from_buffer(data), len(data))

    def list(self, request=None):
        return ListIterator(self, request)

    def retrieve(self, request):
        return DataRetriever(self, request)

    @property
    def ctype(self):
        return self.__fdb


fdb = None

def archive(key, data):
    global fdb
    if not fdb:
        fdb = FDB()
    fdb.archive(key, data)


def list(request):
    global fdb
    if not fdb:
        fdb = FDB()
    return ListIterator(fdb, request)


def retrieve(request):
    global fdb
    if not fdb:
        fdb = FDB()
    return DataRetriever(fdb, request)

