from collections import OrderedDict
from copy import deepcopy
from functools import reduce
try:
    import pickle as pickle
except ImportError:
    import pickle
import datetime as dt
import logging
import os
import time
import uuid

from microdrop_utility import is_int, Version, FutureVersionError
from path_helpers import path
import arrow
import pandas as pd
import yaml

from logging_helpers import _L  #: .. versionadded:: 2.20


logger = logging.getLogger(__name__)


def log_data_to_frame(log_data_i):
    '''
    Parameters
    ----------
    log_data_i : microdrop.experiment_log.ExperimentLog
        MicroDrop experiment log, as pickled in the ``data``
        file in each experiment log directory.

    Returns
    -------
    (pd.Series, pd.DataFrame)
        Tuple containing:
        - Experiment information, including UTC start time,
        MicroDrop software version, list of plugin versions,
        etc.
        - Data frame with multi-index columns, indexed first by
        plugin name, then by plugin field name.

        .. note::
            Values may be Python objects.  In future versions
            of MicroDrop, values *may* be restricted to json
            compatible types.
    '''
    def log_frame_experiment_info(df_log):
        experiment_info = df_log['core'].iloc[0].copy()
        experiment_info.update(df_log['core'].iloc[-1])

        start_time = arrow.get(experiment_info['start time']).naive
        experiment_info['utc_start_time'] = start_time.isoformat()
        for k in ('step', 'start time', 'time', 'attempt', 'utc_timestamp'):
            if k in experiment_info.index:
                del experiment_info[k]
        return experiment_info.dropna()

    plugin_names_i = sorted(reduce(lambda a, b: a.union(list(b.keys())),
                                   log_data_i.data, set()))
    frames_i = OrderedDict()

    for plugin_name_ij in plugin_names_i:
        try:
            frame_ij = pd.DataFrame([pickle.loads(v)
                                        if v else {} for v in [s.get(plugin_name_ij)
                                        for s in log_data_i.data]])
        except Exception as exception:
            print((plugin_name_ij, exception))
        else:
            frames_i[plugin_name_ij] = frame_ij
    df_log_i = pd.concat(list(frames_i.values()), axis=1, keys=list(frames_i.keys()))

    start_time_i = arrow.get(df_log_i.iloc[0][('core', 'start time')]).naive
    df_log_i[('core', 'utc_timestamp')] = \
        (start_time_i + df_log_i[('core', 'time')]
         .map(lambda s: dt.timedelta(seconds=s) if s == s else None))
    df_log_i.sort_index(axis=1, inplace=True)
    experiment_info = log_frame_experiment_info(df_log_i)
    experiment_info['uuid'] = log_data_i.uuid
    df_log_i.dropna(subset=[('core', 'step'), ('core', 'attempt')],
                    inplace=True)
    return experiment_info, df_log_i


class ExperimentLog():
    class_version = str(Version(0, 3, 0))

    def __init__(self, directory=None):
        self.directory = directory
        self.data = []
        self.version = self.class_version
        self.uuid = str(uuid.uuid4())
        self._get_next_id()
        self.metadata = {}  # Meta data, keyed by plugin name.
        _L().info('new log with id=%s and uuid=%s', self.experiment_id,
                  self.uuid)

    def _get_next_id(self):
        if self.directory is None:
            self.experiment_id = None
            return
        if os.path.isdir(self.directory) is False:
            os.makedirs(self.directory)
        logs = path(self.directory).listdir()
        self.experiment_id = 0
        for d in logs:
            if is_int(d.name):
                i = int(d.name)
                if i >= self.experiment_id:
                    self.experiment_id = i
                    # increment the experiment_id if the current directory is
                    # not empty
                    if len(d.listdir()):
                        self.experiment_id += 1
        log_path = self.get_log_path()
        if not log_path.isdir():
            log_path.makedirs_p()

    def _upgrade(self):
        """
        Upgrade the serialized object if necessary.

        Raises:
            FutureVersionError: file was written by a future version of the
                software.
        """
        logger = _L()  # use logger with method context
        version = Version.fromstring(self.version)
        logger.debug('version=%s, class_version=%s',
                     str(version), self.class_version)
        if version > Version.fromstring(self.class_version):
            logger.debug('version > class_version')
            raise FutureVersionError
        if version < Version(0, 1, 0):
            new_data = []
            plugin_name = None
            for step_data in self.data:
                if "control board hardware version" in list(step_data.keys()):
                    plugin_name = "wheelerlab.dmf_control_board_" + \
                        step_data["control board hardware version"]
            for i in range(len(self.data)):
                new_data.append({})
                for k, v in list(self.data[i].items()):
                    if plugin_name and k in ("FeedbackResults",
                                             "SweepFrequencyResults",
                                             "SweepVoltageResults"):
                        try:
                            new_data[i][plugin_name] = {k: pickle.loads(v)}
                        except Exception as e:
                            logger.error("Couldn't load experiment log data "
                                         "for plugin: %s. %s.", plugin_name, e)
                    else:
                        if "core" not in new_data[i]:
                            new_data[i]["core"] = {}
                        new_data[i]["core"][k] = v

            # serialize objects to yaml strings
            for i in range(len(self.data)):
                for plugin_name, plugin_data in list(new_data[i].items()):
                    new_data[i][plugin_name] = yaml.dump(plugin_data)
            self.data = new_data
            self.version = str(Version(0, 1, 0))
        if version < Version(0, 2, 0):
            self.uuid = str(uuid.uuid4())
            self.version = str(Version(0, 2, 0))
        if version < Version(0, 3, 0):
            self.metadata = {}
            self.version = str(Version(0, 3, 0))
        # else the versions are equal and don't need to be upgraded

    @classmethod
    def load(cls, filename):
        """
        Load an experiment log from a file.

        Args:
            filename: path to file.
        Raises:
            TypeError: file is not an experiment log.
            FutureVersionError: file was written by a future version of the
                software.
        """
        logger = _L()  # use logger with method context
        logger.info("Loading Experiment log from %s", filename)
        out = None
        start_time = time.time()
        with open(filename, 'rb') as f:
            try:
                out = pickle.load(f)
                logger.debug("Loaded object from pickle.")
            except Exception as e:
                logger.debug("Not a valid pickle file. %s." % e)
        if out is None:
            with open(filename, 'rb') as f:
                try:
                    out = yaml.load(f)
                    logger.debug("Loaded object from YAML file.")
                except Exception as e:
                    logger.debug("Not a valid YAML file. %s." % e)
        if out is None:
            raise TypeError
        out.filename = filename
        # check type
        if out.__class__ != cls:
            raise TypeError
        if not hasattr(out, 'version'):
            out.version = str(Version(0))
        out._upgrade()
        # load objects from serialized strings
        for i in range(len(out.data)):
            for plugin_name, plugin_data in list(out.data[i].items()):
                try:
                    out.data[i][plugin_name] = pickle.loads(plugin_data)
                except Exception as e:
                    logger.debug("Not a valid pickle string ("
                                 "plugin: %s). %s." % (plugin_name, e))
                    try:
                        out.data[i][plugin_name] = yaml.load(plugin_data)
                    except Exception as e:
                        logger.error("Couldn't load experiment log data for "
                                     "plugin: %s. %s." % (plugin_name, e))
        logger.debug("loaded in %f s.", time.time() - start_time)
        return out

    def save(self, filename=None, format='pickle'):
        if filename is None:
            log_path = self.get_log_path()
            filename = os.path.join(log_path, "data")
        else:
            log_path = path(filename).parent

        if self.data:
            out = deepcopy(self)
            # serialize plugin dictionaries to strings
            for i in range(len(out.data)):
                for plugin_name, plugin_data in list(out.data[i].items()):
                    if format == 'pickle':
                        out.data[i][plugin_name] = pickle.dumps(plugin_data)
                    elif format == 'yaml':
                        out.data[i][plugin_name] = yaml.dump(plugin_data)
                    else:
                        raise TypeError
            with open(filename, 'wb') as f:
                if format == 'pickle':
                    pickle.dump(out, f, -1)
                elif format == 'yaml':
                    yaml.dump(out, f)
                else:
                    raise TypeError
        return log_path

    def start_time(self):
        data = self.get("start time")
        for val in data:
            if val:
                return val
        start_time = time.time()
        self.add_data({"start time": start_time})
        return start_time

    def get_log_path(self):
        return path(self.directory).joinpath(str(self.experiment_id))

    def add_step(self, step_number, attempt=0):
        self.data.append({'core': {'step': step_number,
                                   'time': (time.time() - self.start_time()),
                                   'attempt': attempt}})

    def add_data(self, data, plugin_name='core'):
        if not self.data:
            self.data.append({})
        if plugin_name not in self.data[-1]:
            self.data[-1][plugin_name] = {}
        for k, v in list(data.items()):
            self.data[-1][plugin_name][k] = v

    def get(self, name, plugin_name='core'):
        var = []
        for d in self.data:
            if plugin_name in d and list(d[plugin_name].keys()).count(name):
                var.append(d[plugin_name][name])
            else:
                var.append(None)
        return var

    def to_frame(self):
        '''
        Returns
        -------
        (pd.Series, pd.DataFrame)
            Tuple containing:
             - Experiment information, including UTC start time, MicroDrop
               software version, list of plugin versions, etc.
            - Data frame with multi-index columns, indexed first by plugin
              name, then by plugin field name.

            .. note::
                Values may be Python objects.  In future versions
                of MicroDrop, values *may* be restricted to json
                compatible types.
        '''
        return log_data_to_frame(self)

    @property
    def empty(self):
        '''
        Returns
        -------
        bool
            `True` if experiment log contains data or experiment log directory
            contains one or more files/directories.


        .. versionadded:: 2.32.3
        '''
        if self.get_log_path().listdir():
            # Experiment log contains files and/or directories.
            return False
        elif [x for x in self.get('step') if x is not None]:
            # Experiment log contains in-memory data.
            return False
        else:
            # Experiment log is empty.
            return True
