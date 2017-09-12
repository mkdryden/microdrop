from argparse import ArgumentParser

from path_helpers import path


MICRODROP_PARSER = ArgumentParser(description='MicroDrop: graphical user '
                                  'interface for the DropBot Digital '
                                  'Microfluidics control system.',
                                  add_help=False)
MICRODROP_PARSER.add_argument('-c', '--config', type=path, default=None)


def base_path():
    return path(__file__).abspath().parent


def glade_path():
    '''
    Return path to `.glade` files used by `gtk` to construct views.
    '''
    return base_path().joinpath('gui', 'glade')
