try:
    from . import microdrop_plugin
except ImportError:
    import sys

    print('Error importing command_plugin', file=sys.stderr)
