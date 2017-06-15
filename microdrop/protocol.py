"""
Copyright 2011 Ryan Fobel and Christian Fobel

This file is part of MicroDrop.

MicroDrop is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

MicroDrop is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with MicroDrop.  If not, see <http://www.gnu.org/licenses/>.
"""

from collections import OrderedDict
import copy
try:
    import cPickle as pickle
except ImportError:
    import pickle
import cStringIO as StringIO
import importlib
import json
import logging
import re
import sys
import time
import types

from microdrop_utility import Version, FutureVersionError
import jsonschema
import pandas as pd
import path_helpers as ph
import yaml
import zmq_plugin as zp
import zmq_plugin.schema

from .plugin_manager import emit_signal


logger = logging.getLogger(__name__)


MESSAGE_SCHEMA = {
    'definitions':
    {'unique_id': {'type': 'string', 'description': 'Typically UUID'},
     'plugin_data': {'type': 'object',
                     'description': 'Plugin state data'},
     'protocol':
     {'description': 'MicroDrop protocol',
      'type': 'object',
      'properties':
      {'name': {'type': 'string', 'description': 'Protocol name'},
       'uuid': {'$ref': '#/definitions/unique_id'},
       'version' : {'type': 'string',
                    'default': '0.2.0',
                    'enum': ['0.2.0'],
                    'description': 'The MicroDrop protocol version'},
       'steps': {"type": "array",
                 "items": {'$ref': '#/definitions/step'}}},
      'required': ['name', 'version']},
     'step':
     {'description': 'MicroDrop protocol step',
      'type': 'object',
      'properties':
      {'plugin_data': {'$ref': '#/definitions/plugin_data'},
       'version' : {'type': 'string',
                    'default': '0.2.0',
                    'enum': ['0.2.0'],
                    'description': 'The MicroDrop step version'},
      'additionalProperties': False}}},
}

PROTOCOL_SCHEMA = copy.deepcopy(MESSAGE_SCHEMA)
PROTOCOL_SCHEMA['allOf'] = [{'$ref': '#/definitions/protocol'}]
STEP_SCHEMA = copy.deepcopy(MESSAGE_SCHEMA)
STEP_SCHEMA['allOf'] = [{'$ref': '#/definitions/step'}]

VALIDATORS = {'protocol': jsonschema.Draft4Validator(PROTOCOL_SCHEMA),
              'step': jsonschema.Draft4Validator(STEP_SCHEMA)}


class SerializationError(Exception):
    '''
    Attributes
    ----------
    message : str
        Error message.
    exceptions : list
        List of objects corresponding to serialization exceptions.

        Objects are in the following form:

        ``{'step': <step number>, 'plugin': <plugin name>, 'data': <plugin data>, error': <error message>}``
    '''
    def __init__(self, message, exceptions):
        super(SerializationError, self).__init__(message)
        self.exceptions = exceptions


def serialize_protocol(protocol_dict, serialize_func):
    '''
    Parameters
    ----------
    protocol_dict : dict
        A MicroDrop protocol in dictionary format.

        See :func:`protocol_to_dict` and :meth:`Protocol.to_dict`.
    serialize_func : function
        Serialization function.

    Returns
    -------
    object
        Result of call to ``serialize_func``.

    Raises
    ------
    SerializationError
        If exception occurs during serialization.

        The ``SerializationError`` object includes an ``exceptions`` attribute
        containing details on errors encountered.  See ``SerializationError``
        class for more details.
    '''
    try:
        # Serialize entire protocol.
        return serialize_func(protocol_dict)
    except Exception, exception:
        # Exception occurred.  Try to identify where exception occurred.
        logger.debug('Error serializing protocol.')
        logger.debug('Search for steps causing exception')
        # Try to serialize each step, and record which steps cause exceptions.
        exception_steps = []
        for i, step_i in enumerate(protocol_dict['steps']):
            try:
                serialize_func(step_i)
            except Exception, exception:
                exception_steps.append({'step': i, 'error': str(exception)})
        logger.debug('Search for plugin(s) causing exception')
        # For each step causing an exception, try to independently serialize
        # data for each plugin, recording which plugins cause exceptions.
        exceptions = []
        for exception_step_i in exception_steps:
            step_i = protocol_dict['steps'][exception_step_i['step']]
            for plugin_name_ij, plugin_data_ij in step_i.iteritems():
                try:
                    serialize_func(plugin_data_ij)
                except Exception, exception:
                    exception_step_i.update({'error': str(exception),
                                             'plugin': plugin_name_ij,
                                             'data': plugin_data_ij})
                    exceptions.append(exception_step_i)
        raise SerializationError('Error serializing protocol.', exceptions)


def protocol_to_frame(protocol_i):
    '''
    Parameters
    ----------
    protocol_i : Protocol
        MicroDrop protocol.

        .. note::
            A MicroDrop protocol object is stored as pickled in the
            ``protocol`` file in each experiment log directory.

    Returns
    -------
    pandas.DataFrame
         Data frame with rows indexed by 0-based step number and columns
         indexed (multi-index) first by plugin name, then by step field name.

         .. note::
             Values may be Python objects.  In future versions
             of MicroDrop, values *may* be restricted to json
             compatible types.
    '''
    plugin_names_i = sorted(reduce(lambda a, b:
                                   a.union(b.plugin_data.keys()),
                                   protocol_i.steps, set()))
    frames_i = OrderedDict()

    for plugin_name_ij in plugin_names_i:
        try:
            frame_ij = pd.DataFrame(map(pickle.loads,
                                        [s.plugin_data.get(plugin_name_ij)
                                         for s in protocol_i.steps]))
        except Exception, exception:
            print >> sys.stderr, exception
        else:
            frames_i[plugin_name_ij] = frame_ij
    df_protocol = pd.concat(frames_i.values(), axis=1, keys=frames_i.keys())
    df_protocol.index.name = 'step_i'
    df_protocol.columns.names = ['plugin_name', 'step_field']
    return df_protocol


def safe_pickle_loads(data):
    '''
    Parameters
    ----------
    data : bytes
        Pickled data.

    Returns
    -------
    object or None
        Deserialized pickled object.

        If exception occurs during unpickling, error is logged and ``None`` is
        returned.
    '''
    try:
        return pickle.loads(data)
    except Exception, exception:
        logger.error('Error deserializing pickle data: `%s`\n%s', data,
                     exception)


def protocol_to_dict(protocol, loaded=True):
    '''
    Convert a :class:`Protocol` to a dictionary representation.

    Each plugin MAY independently implement a custom ``to_dict`` method on the
    respective plugin step options class, along with a corresponding
    ``from_dict`` class method.  If these methods are implemented, the
    resulting dictionary from ``to_dict`` MUST contain the key ``__class__``
    indicating the fully-qualified class name of the step options (used to
    reconstruct the step options with the ``from_dict`` class method).

    Parameters
    ----------
    protocol : Protocol
        MicroDrop protocol.

        .. note::
            A MicroDrop protocol object is stored as pickled in the
            ``protocol`` file in each experiment log directory.
    loaded : bool, optional
        ``True`` if protocol was loaded using :meth:`Protocol.load`.

    Returns
    -------
    dict
        Dictionary object with the following top-level keys:
         - ``name``: Protocol name.
         - ``version``: Protocol version.
         - ``steps``: List of dictionaries, each containing data for a single
           protocol step.
         - ``uuid, optional``: Universally unique identifier.
    '''
    step_data = [{plugin_ij: step_i.get_data(plugin_ij) if loaded else
                  safe_pickle_loads(step_i.get_data(plugin_ij))
                  for plugin_ij in sorted(step_i.plugins)}
                  for step_i in protocol.steps]
    # For each step, use `to_dict` class method to convert Python object
    # to dictionary for plugins where applicable.
    for step_i in step_data:
        to_update_i = []
        for plugin_name_ij, plugin_data_ij in step_i.iteritems():
            if hasattr(plugin_data_ij, 'to_dict'):
                to_update_i.append((plugin_name_ij, plugin_data_ij.to_dict()))
        for plugin_name_ij, plugin_data_ij in to_update_i:
            step_i[plugin_name_ij] = plugin_data_ij
    protocol_dict = {'name': protocol.name,
                     'version': protocol.version,
                     'steps': step_data,
                     'plugin_data': protocol.plugin_data}
    return protocol_dict


def protocol_from_dict(protocol_dict):
    '''
    Convert a protocol dictionary representation to a :class:`Protocol`.

    Each plugin MAY independently implement a custom ``to_dict`` method on the
    respective plugin step options class, along with a corresponding
    ``from_dict`` class method.  If these methods are implemented, the
    fully-qualified class name is looked up in the respective ``__class__``
    item in the step plugin data to reconstruct the step options with the
    corresponding ``from_dict`` class method.

    Parameters
    ----------
    protocol_dict : dict
        Dictionary object with the following top-level keys:
         - ``name``: Protocol name.
         - ``version``: Protocol version.
         - ``steps``: List of dictionaries, each containing data for a single
           protocol step.
         - ``uuid, optional``: Universally unique identifier.

    Returns
    -------
    Protocol
        MicroDrop protocol.
    '''
    try:
        VALIDATORS['protocol'].validate(protocol_dict)
    except jsonschema.ValidationError:
        logging.warning('Error validating protocol dictionary.', exc_info=True)
        raise
    protocol = Protocol(name=protocol_dict['name'])
    assert(protocol.version == protocol_dict['version'])
    protocol.steps = [Step(plugin_data={plugin_ij: data_ij
                                        for plugin_ij, data_ij in
                                        step_i.iteritems()})
                      for step_i in protocol_dict['steps']]

    # For each step, use `from_dict` class method to reconstruct Python object
    # for plugins where applicable.
    for step_i in protocol.steps:
        to_update_i = []
        for plugin_name_ij, plugin_data_ij in step_i.plugin_data.iteritems():
            if '__class__' in plugin_data_ij:
                class_str = plugin_data_ij.pop('__class__')
                module_str = '.'.join(class_str.split('.')[:-1])
                class_name_str = class_str.split('.')[-1]
                module_ij = importlib.import_module(module_str)
                class_ = getattr(module_ij, class_name_str)
                if hasattr(class_, 'from_dict'):
                    to_update_i.append((plugin_name_ij,
                                        class_.from_dict(plugin_data_ij)))
        for plugin_name_ij, plugin_data_ij in to_update_i:
            step_i.plugin_data[plugin_name_ij] = plugin_data_ij

    return protocol


def protocol_to_json(protocol, validate=True, ostream=None, json_kwargs=None,
                     **kwargs):
    '''
    Parameters
    ----------
    protocol : Protocol
        MicroDrop protocol.
    validate : bool, optional
        If ``True``, validate protocol in dictionary form before serializing to
        JSON.
    ostream : file-like, optional
        Output stream to write to.
    kwargs : bool, optional
        ``True`` if protocol was loaded using :meth:`Protocol.load`.

    Returns
    -------
    None or str
        If :data:`ostream` parameter is ``None``, return serialized
        protocol in JSON format as string.

        See :func:`protocol_to_dict` for details on JSON object structure.
    '''
    if ostream is None:
        ostream = StringIO.StringIO()
        return_required = True
    else:
        return_required = False
    protocol_dict = protocol_to_dict(protocol, **kwargs)

    if validate:
        VALIDATORS['protocol'].validate(protocol_dict)

    def serialize_func(obj):
        return json.dump(obj=obj, fp=ostream,
                         cls=zp.schema.PandasJsonEncoder,
                         **(json_kwargs or {}))

    serialize_protocol(protocol_dict, serialize_func)

    if return_required:
        return ostream.getvalue()


def protocol_to_ndjson(protocol, ostream=None):
    '''
    Write protocol as newline delimited JSON (i.e., `ndjson`_, see
    `specification`_).

    The first row is a header JSON object containing **at least** the keys
    ``name`` and ``version``.

    Each subsequent line in the output is a nested JSON object, one line per
    protocol step.  The keys of the top-level object of each step object
    correspond to plugin names.  The second-level keys correspond to the step
    field name.

    Parameters
    ----------
    protocol : Protocol
        MicroDrop protocol.
    ostream : file-like, optional
        Output stream to write to.

    Returns
    -------
    None or str
        If :data:`ostream` parameter is ``None``, return output as string.

    Raises
    ------
    SerializationError
        If exception occurs during serialization.

        The ``SerializationError`` object includes an ``exceptions`` attribute
        containing details on errors encountered.  See ``SerializationError``
        class for more details.

    See Also
    --------
    :func:`protocol_to_json`


    .. _`ndjson`: http://ndjson.org/
    .. _`specification`: http://specs.frictionlessdata.io/ndjson/
    '''
    protocol_dict = protocol.to_dict()

    if ostream is None:
        ostream = StringIO.StringIO()
        return_required = True
    else:
        return_required = False

    steps = protocol_dict.pop('steps')

    def serialize_func(obj):
        return json.dumps(obj, cls=zp.schema.PandasJsonEncoder)

    # Write JSON header (does not include any step data).
    print >> ostream, serialize_func(protocol_dict)
    # Write plugin data for each step to a separate line in the output
    # stream.
    exceptions = []
    for i, step_i in enumerate(steps):
        try:
            print >> ostream, serialize_func(step_i)
        except Exception, exception:
            # Exception occurred while serializing step.
            logger.debug('Error serializing step.')
            # Try to independently serialize data for each plugin,
            # recording which plugins cause exceptions.
            logger.debug('Search for plugin(s) causing exception')
            for plugin_name_ij, plugin_data_ij in step_i.iteritems():
                try:
                    serialize_func(plugin_data_ij)
                except Exception, exception:
                    exception_step_i = {'step': i,
                                        'error': str(exception),
                                        'plugin': plugin_name_ij,
                                        'data': plugin_data_ij}
                    exceptions.append(exception_step_i)
    if exceptions:
        raise SerializationError('Error serializing protocol.', exceptions)
    if return_required:
        return ostream.getvalue()


def _protocol_remove_exceptions(protocol, exceptions, step_getter,
                                plugin_data_getter, inplace=False):
    '''
    Parameters
    ----------
    protocol : Protocol or dict
        MicroDrop protocol or dictionary representation.
    exceptions : list-like
        Exceptions in format recorded in :data:`exceptions` attribute of
        :class:`SerializationError` instances.
    step_getter : function
        Function that takes a protocol object and an integer step number and
        returns a corresponding step object.
    plugin_data_getter : function
        Function that takes a step object and returns a corresponding plugin
        data dictionary.
    inplace : bool, optional
        If ``True``, directly modify :data:`protocol`.

        Otherwise, return modified copy.

        Default is ``False``.

    Returns
    -------
    Protocol or dict or None
        Modified copy of :data:`protocol` if :data:`inplace` is ``False``.

    See also
    --------
    :func:`protocol_remove_exceptions`, :func:`protocol_dict_remove_exceptions`
    '''
    if not inplace:
        protocol = copy.deepcopy(protocol)

    # Delete plugin data that is causing serialization errors.
    for exception_i in exceptions:
        step_i = step_getter(protocol, exception_i['step'])
        plugin_data_i = plugin_data_getter(step_i)
        del plugin_data_i[exception_i['plugin']]
        logger.info('Deleted `%s` for step %s', exception_i['plugin'],
                    exception_i['step'])

    if not inplace:
        return protocol


def protocol_dict_remove_exceptions(protocol_dict, exceptions, inplace=False):
    '''
    Parameters
    ----------
    protocol_dict : dict
        Dictionary object with the following top-level keys:
         - ``name``: Protocol name.
         - ``version``: Protocol version.
         - ``steps``: List of dictionaries, each containing data for a single
           protocol step.
         - ``uuid, optional``: Universally unique identifier.
    exceptions : list-like
        Exceptions in format recorded in :data:`exceptions` attribute of
        :class:`SerializationError` instances.
    inplace : bool, optional
        If ``True``, directly modify :data:`protocol_dict`.

        Otherwise, return modified copy.

        Default is ``False``.

    Returns
    -------
    dict or None
        Modified copy of :data:`protocol_dict` if :data:`inplace` is ``False``.

    See also
    --------
    :func:`protocol_dict_remove_exceptions`
    '''
    step_getter = lambda protocol_i, step_i: protocol_i['steps'][step_i]
    plugin_data_getter = lambda step_i: step_i

    return _protocol_remove_exceptions(protocol_dict, exceptions, step_getter,
                                       plugin_data_getter, inplace=inplace)


def protocol_remove_exceptions(protocol, exceptions, inplace=False):
    '''
    Parameters
    ----------
    protocol : Protocol
        MicroDrop protocol.
    exceptions : list-like
        Exceptions in format recorded in :data:`exceptions` attribute of
        :class:`SerializationError` instances.
    inplace : bool, optional
        If ``True``, directly modify :data:`protocol`.

        Otherwise, return modified copy.

        Default is ``False``.

    Returns
    -------
    Protocol or None
        Modified copy of :data:`protocol` if :data:`inplace` is ``False``.

    See also
    --------
    :func:`protocol_dict_remove_exceptions`
    '''
    step_getter = lambda protocol_i, step_i: protocol_i.steps[step_i]
    plugin_data_getter = lambda step_i: step_i.plugin_data

    return _protocol_remove_exceptions(protocol, exceptions, step_getter,
                                       plugin_data_getter, inplace=inplace)


class Protocol():
    class_version = str(Version(0,2))

    def __init__(self, name=None):
        self.steps = [Step()]
        self.name = name
        self.plugin_data = {}
        self.plugin_fields = {}
        self.n_repeats = 1
        self.current_step_attempt = 0
        self.current_step_number = 0
        self.current_repetition = 0
        self.version = self.class_version

    ###########################################################################
    # Load/save methods
    # -----------------
    @classmethod
    def load(cls, filename):
        """
        Load a Protocol from a file.

        Parameters
        ----------
        filename : str
            Path to file.

        Raises
        ------
        TypeError
            If file is not a :class:`Protocol`.
        FutureVersionError
            If file was written by a future version of the software.
        """
        logger.debug("[Protocol].load(\"%s\")" % filename)
        logger.info("Loading Protocol from %s" % filename)
        filename = ph.path(filename)
        if filename.ext.lower() == '.json':
            with filename.open('r') as input_:
                return cls.from_json(istream=input_)

        start_time = time.time()
        out = None
        with open(filename, 'rb') as f:
            try:
                out = pickle.load(f)
                logger.debug("Loaded object from pickle.")
            except Exception, e:
                logger.debug("Not a valid pickle file. %s." % e)
        if out==None:
            with open(filename, 'rb') as f:
                try:
                    out = yaml.load(f)
                    logger.debug("Loaded object from YAML file.")
                except Exception, e:
                    logger.debug("Not a valid YAML file. %s." % e)
        if out==None:
            raise TypeError
        out.filename = filename

        # enable loading of old protocols that were loaded as relative packages
        # (i.e., not subpackages of microdrop).
        if str(out.__class__) == 'protocol.Protocol':
            out.__class__ = cls

        # check type
        if out.__class__ != cls:
            raise TypeError, "File is wrong type: %s" % out.__class__
        if not hasattr(out, 'version'):
            out.version = str(Version(0))
        out._upgrade()

        for k, v in out.plugin_data.items():
            try:
                out.plugin_data[k] = pickle.loads(v)
            except Exception, e:
                try:
                    out.plugin_data[k] = yaml.load(v)
                except Exception, e:
                    logger.error('[Protocol].load() Error unpickling plugin '
                                 'data for %s', k, exc_info=True)
        for i in range(len(out)):
            for k, v in out[i].plugin_data.items():
                try:
                    out[i].plugin_data[k] = pickle.loads(v)
                except Exception, e:
                    try:
                        # enable loading of old protocols where the
                        # dmf_device_controller was imported as a relative package
                        v = v.replace('!!python/object:gui.dmf_device_controller.',
                                      '!!python/object:microdrop.gui.'
                                      'dmf_device_controller.')
                        out[i].plugin_data[k] = yaml.load(v)
                    except Exception, e:
                        logger.error('[Protocol].load() Error unpickling '
                                     'plugin data for %s', k, exc_info=True)
        logger.debug("[Protocol].load() loaded in %f s." % \
                     (time.time()-start_time))
        return out

    def remove_exceptions(self, exceptions, inplace=False):
        return protocol_remove_exceptions(self, exceptions, inplace=inplace)

    def save(self, filename, format='pickle'):
        out = copy.deepcopy(self)
        if hasattr(out, 'filename'):
            del out.filename

        # convert plugin data objects to strings
        for k, v in out.plugin_data.items():
            out.plugin_data[k] = pickle.dumps(v, -1)

        for step in out.steps:
            for k, v in step.plugin_data.items():
                step.plugin_data[k] = pickle.dumps(v, -1)

        with open(filename, 'wb') as f:
            if format=='pickle':
                pickle.dump(out, f, -1)
            elif format=='yaml':
                yaml.dump(out, f)
            else:
                raise TypeError

    def to_dict(self):
        '''
        Returns
        -------
        dict
            Dictionary object with the following top-level keys:
            - ``name``: Protocol name.
            - ``version``: Protocol version.
            - ``steps``: List of dictionaries, each containing data for a single
            protocol step.
            - ``uuid, optional``: Universally unique identifier.
        '''
        return protocol_to_dict(self, loaded=True)

    @classmethod
    def from_dict(cls, protocol_dict):
        '''
        Parameters
        ----------
        protocol_dict : dict
            Dictionary object with the following top-level keys:
             - ``name``: Protocol name.
             - ``version``: Protocol version.
             - ``steps``: List of dictionaries, each containing data for a single
               protocol step.
             - ``uuid, optional``: Universally unique identifier.

        Returns
        -------
        Protocol
            MicroDrop protocol.
        '''
        return protocol_from_dict(protocol_dict)

    def to_frame(self):
        '''
        Returns
        -------
        pandas.DataFrame
            Data frame with multi-index columns, indexed first by plugin name,
            then by plugin step field name.

            .. note::
                If an exception is encountered while processing a plugin value,
                the plugin causing the exception is skipped and protocol values
                related to the plugin are not included in the result.

        See Also
        --------
        :meth:`to_json`, :meth:`to_ndjson`
        '''
        return protocol_to_frame(self)

    def to_json(self, ostream=None, **kwargs):
        '''
        Parameters
        ----------
        ostream : file-like, optional
            Output stream to write to.

        Returns
        -------
        None or str
            If :data:`ostream` parameter is ``None``, return serialized
            protocol in JSON format as string.

            See :func:`protocol_to_json` for details on JSON object structure.

        See Also
        --------
        :meth:`to_dict`, :meth:`to_ndjson`
        '''
        return protocol_to_json(self, ostream=ostream, json_kwargs=kwargs)

    @classmethod
    def from_json(cls, istream):
        '''
        Parameters
        ----------
        istream : str or file-like
            Input JSON to read protocol from.

            If file-like, read from as an input stream.

            If a string, assume input is JSON serialized protocol string.

        Returns
        -------
        Protocol
            MicroDrop protocol.

        See Also
        --------
        :meth:`to_json`, :meth:`to_dict`, :meth:`to_ndjson`
        '''
        if isinstance(istream, types.StringTypes):
            # Assume input is JSON serialized protocol string.
            load_func = json.loads
        else:
            # Read from `istream` as an input stream.
            load_func = json.load
        protocol_dict = load_func(istream, object_hook=
                                  zp.schema.pandas_object_hook)
        return protocol_from_dict(protocol_dict)

    def to_ndjson(self, ostream=None, ignore_errors=False):
        '''
        Write protocol as newline delimited JSON (i.e., `ndjson`_, see
        `specification`_).

        Parameters
        ----------
        ostream : file-like, optional
            Output stream to write to.
        ignore_errors : bool, optional
            If ``True``, skip any step plugin data that causes an error during
            serialization.

        Returns
        -------
        None or str
            If :data:`ostream` parameter is ``None``, return output as string.

        Raises
        ------
        SerializationError
            If exception occurs during serialization.

            The ``SerializationError`` object includes an ``exceptions``
            attribute containing details on errors encountered.  See
            ``SerializationError`` class for more details.

        See Also
        --------
        :func:`protocol_to_ndjson`, :meth:`to_json`


        .. _`ndjson`: http://ndjson.org/
        .. _`specification`: http://specs.frictionlessdata.io/ndjson/
        '''
        try:
            return protocol_to_ndjson(self, ostream=ostream)
        except SerializationError, exception:
            if not ignore_errors:
                raise
            else:
                logging.warn('Skipping plugin data in steps where exceptions '
                            'encountered during serialization.')
                protocol_clean = self.remove_exceptions(exception.exceptions)
                return protocol_to_ndjson(protocol_clean, ostream=ostream)

    @classmethod
    def from_ndjson(cls, istream=None):
        '''
        Read protocol from newline delimited JSON (i.e., `ndjson`_, see
        `specification`_).

        Parameters
        ----------
        istream : str or file-like
            Input new-line delimited JSON to read protocol from.

            If file-like, read from as an input stream.

            If a string, assume input is new-line delimited JSON serialized
            protocol string.

        Returns
        -------
        Protocol
            MicroDrop protocol.

        See Also
        --------
        :func:`protocol_to_ndjson`, :meth:`to_ndjson`, :meth:`to_json`


        .. _`ndjson`: http://ndjson.org/
        .. _`specification`: http://specs.frictionlessdata.io/ndjson/
        '''
        if isinstance(istream, types.StringTypes):
            # Assume input is new-line delimited JSON serialized protocol
            # string.
            istream = StringIO.StringIO(istream)
        load_f = lambda x: json.loads(x, object_hook=
                                      zp.schema.pandas_object_hook)
        protocol_dict = load_f(istream.readline())
        protocol_dict['steps'] = [load_f(line_i)
                                  for line_i in istream.readlines()]
        return protocol_from_dict(protocol_dict)

    def _upgrade(self):
        """
        Upgrade the serialized object if necessary.

        Raises:
            FutureVersionError: file was written by a future version of the
                software.
        """
        logger.debug("[Protocol]._upgrade()")
        version = Version.fromstring(self.version)
        logger.debug('[Protocol] version=%s, class_version=%s' % (str(version), self.class_version))
        if version > Version.fromstring(self.class_version):
            logger.debug('[Protocol] version>class_version')
            raise FutureVersionError(Version.fromstring(self.class_version), version)
        elif version < Version.fromstring(self.class_version):
            if version < Version(0,1):
                for k, v in self.plugin_data.items():
                    self.plugin_data[k] = yaml.dump(v)
                for step in self.steps:
                    for k, v in step.plugin_data.items():
                        step.plugin_data[k] = yaml.dump(v)
                self.version = str(Version(0,1))
                logger.debug('[Protocol] upgrade to version %s' % self.version)
            if version < Version(0,2):
                self.current_step_attempt = 0
                self.version = str(Version(0,2))
                logger.debug('[Protocol] upgrade to version %s' % self.version)
        # else the versions are equal and don't need to be upgraded

    ###########################################################################
    # Plugin name accessors
    # ---------------------
    @property
    def plugins(self):
        return set(self.plugin_data.keys())

    def plugin_name_lookup(self, name, re_pattern=False):
        if not re_pattern:
            return name

        for plugin_name in self.plugins:
            if re.search(name, plugin_name):
                return plugin_name
        return None

    ###########################################################################
    # Protocol-wide plugin data
    # -------------------------
    def get_data(self, plugin_name):
        logging.debug('[Protocol] plugin_data=%s' % self.plugin_data)
        return self.plugin_data.get(plugin_name)

    def set_data(self, plugin_name, data):
        self.plugin_data[plugin_name] = data

    ###########################################################################
    # Execution state
    # ---------------
    def __len__(self):
        return len(self.steps)

    def __getitem__(self, i):
        return self.steps[i]

    def get_step_number(self, default):
        if default is None:
            return self.current_step_number
        return default

    def get_step(self, step_number=None):
        step_number = self.get_step_number(step_number)
        return self.steps[step_number]

    def current_step(self):
        return self.steps[self.current_step_number]

    def insert_steps(self, step_number=None, count=None, values=None):
        if values is None and count is None:
            raise ValueError, 'Either count or values must be specified'
        elif values is None:
            values = [Step()] * count
        for value in values[::-1]:
            self.insert_step(step_number, value, notify=False)
        emit_signal('on_steps_inserted', args=range(step_number, step_number +
                                                    len(values)))

    def insert_step(self, step_number=None, value=None, notify=True):
        if step_number is None:
            step_number = self.current_step_number
        if value is None:
            value = Step()
        self.steps.insert(step_number, value)
        emit_signal('on_step_created', args=[self.current_step_number])
        if notify:
            emit_signal('on_step_inserted', args=[self.current_step_number])

    def delete_step(self, step_number):
        step_to_remove = self.steps[step_number]
        del self.steps[step_number]
        emit_signal('on_step_removed', args=[step_number, step_to_remove])

        if len(self.steps) == 0:
            # If we deleted the last remaining step, we need to insert a new
            # default Step
            self.insert_step(0, Step())
            self.goto_step(0)
        elif self.current_step_number == len(self.steps):
            self.goto_step(step_number - 1)
        else:
            self.goto_step(self.current_step_number)

    def delete_steps(self, step_ids):
        sorted_ids = sorted(step_ids)
        # Process deletion of steps in reverse order to avoid ID mismatch due
        # to deleted rows.
        sorted_ids.reverse()
        for id in sorted_ids:
            self.delete_step(id)

    def next_step(self):
        if self.current_step_number == len(self.steps) - 1:
            self.insert_step(step_number=self.current_step_number,
                             value=self.current_step().copy(), notify=False)
            self.next_step()
            emit_signal('on_step_inserted', args=[self.current_step_number])
        else:
            self.goto_step(self.current_step_number + 1)

    def next_repetition(self):
        if self.current_repetition < self.n_repeats - 1:
            self.current_repetition += 1
            self.goto_step(0)

    def prev_step(self):
        if self.current_step_number > 0:
            self.goto_step(self.current_step_number - 1)

    def first_step(self):
        self.current_repetition = 0
        self.goto_step(0)

    def last_step(self):
        self.goto_step(len(self.steps) - 1)

    def goto_step(self, step_number):
        logging.debug('[Protocol].goto_step(%s)' % step_number)
        original_step_number = self.current_step_number
        self.current_step_number = step_number
        emit_signal('on_step_swapped', [original_step_number, step_number])


class Step(object):
    def __init__(self, plugin_data=None):
        if plugin_data is None:
            self.plugin_data = {}
        else:
            self.plugin_data = copy.deepcopy(plugin_data)

    def copy(self):
        return Step(plugin_data=copy.deepcopy(self.plugin_data))

    @property
    def plugins(self):
        return set(self.plugin_data.keys())

    def plugin_name_lookup(self, name, re_pattern=False):
        if not re_pattern:
            return name

        for plugin_name in self.plugins:
            if re.search(name, plugin_name):
                return plugin_name
        return None

    def get_data(self, plugin_name):
        logging.debug('[Step] plugin_data=%s' % self.plugin_data)
        return self.plugin_data.get(plugin_name)

    def set_data(self, plugin_name, data):
        self.plugin_data[plugin_name] = data
