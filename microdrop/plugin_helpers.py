from collections import namedtuple
import logging

from microdrop_utility import Version
import path_helpers as ph
import yaml

from .app_context import get_app
from .logging_helpers import _L  #: .. versionadded:: X.X.X
from .plugin_manager import (IPlugin, ExtensionPoint, emit_signal,
                             get_service_instance_by_name)

logger = logging.getLogger(__name__)

PluginMetaData = namedtuple('PluginMetaData',
                            'package_name plugin_name version')
PluginMetaData.as_dict = lambda self: dict([(k, str(v))
                                            for k, v in zip(self._fields,
                                                            self)])


def from_dict(data):
    package_name = data['package_name']
    plugin_name = data['plugin_name']
    version = Version.fromstring(data['version'])
    return PluginMetaData(package_name, plugin_name, version)


PluginMetaData.from_dict = staticmethod(from_dict)


def get_plugin_info(plugin_root):
    '''
    Load the plugin properties metadata from a plugin directory.

    Parameters
    ----------
    plugin_root : str
        Path to plugin directory.

    Returns
    -------
    namedtuple or None
        Plugin metadata in the form ``(package_name, plugin_name, version)``.

        Returns ``None`` if plugin is not installed or is invalid.
    '''
    plugin_root = ph.path(plugin_root)
    properties = plugin_root.joinpath('properties.yml')

    if not properties.isfile():
        return None
    else:
        with properties.open('r') as f_properties:
            properties_dict = yaml.load(f_properties)
            plugin_metadata = PluginMetaData.from_dict(properties_dict)
        return plugin_metadata


class AppDataController(object):
    ###########################################################################
    # Callback methods
    def on_plugin_enable(self):
        """
        Handler called once the plugin instance has been enabled.
        """
        app = get_app()
        defaults = self.get_default_app_options()

        # update app data from config file
        if self.name in app.config.data:
            self.set_app_values(app.config.data[self.name])

        data = app.get_data(self.name)
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        app.set_data(self.name, data)

        if not self.name in app.config.data:
            app.config.data[self.name] = self.get_app_values()

    ###########################################################################
    # Accessor methods
    def get_default_app_options(self):
        if self.AppFields:
            return dict([(k, v.value)
                         for k, v in
                         self.AppFields.from_defaults().iteritems()])
        else:
            return dict()

    def get_app_form_class(self):
        return self.AppFields

    def get_app_fields(self):
        return self.AppFields.field_schema_mapping.keys()

    def get_app_values(self):
        if not hasattr(self, 'name'):
            raise NotImplementedError
        app = get_app()
        return app.get_data(self.name)

    def get_app_value(self, key):
        if not hasattr(self, 'name'):
            raise NotImplementedError
        app = get_app()
        values_dict = app.get_data(self.name)
        return values_dict.get(key,
                               self.AppFields.field_schema_mapping[key]
                               .default)

    @staticmethod
    def get_plugin_app_values(plugin_name):
        observers = ExtensionPoint(IPlugin)
        service = observers.service(plugin_name)
        if hasattr(service, 'get_app_values'):
            return service.get_app_values()
        return None

    ###########################################################################
    # Mutator methods
    def set_app_values(self, values_dict):
        '''
        .. versionchanged:: 2.17.1
            Log invalid keys using **info** level (not **error**) to prevent a
            pop-up dialog from being displayed.
        '''
        if not values_dict:
            return
        if not hasattr(self, 'name'):
            raise NotImplementedError
        for k in values_dict.keys():
            if k not in self.AppFields.field_schema_mapping.keys():
                _L().info("Invalid key (%s) in configuration file section: "
                          "[%s].", k, self.name)
                # remove invalid key from config file
                values_dict.pop(k)
        elements = self.AppFields(value=values_dict)
        if not elements.validate():
            raise ValueError('Invalid values: %s' % elements.errors)
        values = dict([(k, v.value)
                       for k, v in elements.iteritems()
                       if v.value is not None])
        app = get_app()
        app_data = app.get_data(self.name)
        app_data.update(values)
        app.set_data(self.name, app_data)
        emit_signal('on_app_options_changed', [self.name], interface=IPlugin)


class StepOptionsController(object):
    @staticmethod
    def get_plugin_step_values(plugin_name, step_number=None):
        observers = ExtensionPoint(IPlugin)
        service = observers.service(plugin_name)
        if hasattr(service, 'get_step_values'):
            return service.get_step_values(step_number)
        return None

    def get_default_step_options(self):
        if self.get_step_form_class() is None:
            return dict()
        return dict([(k, v.value)
                     for k, v in self.StepFields.from_defaults().iteritems()])

    def get_step_form_class(self):
        return getattr(self, 'StepFields', None)

    def get_step_fields(self):
        if self.get_step_form_class() is None:
            return []
        return self.StepFields.field_schema_mapping.keys()

    def get_step_values(self, step_number=None):
        return self.get_step_options(step_number)

    def get_step_value(self, name, step_number=None):
        step = self.get_step(step_number)

        options = step.get_data(self.name)
        if options:
            if name not in options and (name in self.StepFields
                                        .field_schema_mapping):
                return self.StepFields.from_defaults()[name]
            return options[name]
        return None

    def set_step_values(self, values_dict, step_number=None):
        '''
        Consider a scenario where most step options are simple types that are
        supported by `flatland` and can be listed in `StepOptions` (e.g.,
        `Integer`, `Boolean`, etc.), but there is at least one step option that
        is a type not supported by `flatland`, such as a `numpy.array`.

        Currently, this requires custom handling for all methods related to
        step options, as in the case of the DMF control board. Instead, during
        validation of step option values, we could simply exclude options that
        are not listed in the `StepOptions` definition from the validation, but
        pass along *all* values to be saved in the protocol.

        This should maintain backwards compatibility while simplifying the
        addition of arbitrary Python data types as step options.
        '''
        step_number = self.get_step_number(step_number)
        _L().debug('set_step[%d]: values_dict=%s', step_number, values_dict)
        validate_dict = dict([(k, v) for k, v in values_dict.iteritems()
                              if k in self.StepFields.field_schema_mapping])
        validation_result = self.StepFields(value=validate_dict)
        if not validation_result.validate():
            raise ValueError('Invalid values: %s' % validation_result.errors)
        values = values_dict.copy()
        for name, field in validation_result.iteritems():
            if field.value is None:
                continue
            values[name] = field.value

        if values:
            app = get_app()
            step = app.protocol.steps[step_number]
            step.set_data(self.name, values)
            emit_signal('on_step_options_changed', [self.name, step_number],
                        interface=IPlugin)

    def get_step(self, step_number):
        step_number = self.get_step_number(step_number)
        return get_app().protocol.steps[step_number]

    def get_step_number(self, default):
        if default is None:
            return get_app().protocol.current_step_number
        return default

    def get_step_options(self, step_number=None):
        step = self.get_step(step_number)
        options = step.get_data(self.name)
        if options is None:
            # No data is registered for this plugin (for this step).
            options = self.get_default_step_options()
            step.set_data(self.name, options)
        return options


def hub_execute_async(*args, **kwargs):
    service = get_service_instance_by_name('microdrop.device_info_plugin',
                                           env='microdrop')
    return service.plugin.execute_async(*args, **kwargs)


def hub_execute(*args, **kwargs):
    service = get_service_instance_by_name('microdrop.device_info_plugin',
                                           env='microdrop')
    try:
        return service.plugin.execute(*args, **kwargs)
    except RuntimeError, exception:
        if '**MUST** be a plugin in the local registry' in str(exception):
            # TODO Add `ZmqPlugin` command to refresh plugin registry.
            # Try resetting the plugin sockets to refresh the plugin registry.
            service.plugin.reset()
            return service.plugin.execute(*args, **kwargs)
    except:
        _L().info('plugin registry: %s', service.plugin.plugin_registry.keys())
        raise
