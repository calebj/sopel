# coding=utf-8
from __future__ import unicode_literals, absolute_import, print_function, division

import imp
import importlib
import os.path
import re
import sys
from types import ModuleType

from sopel.tools import compile_rule, itervalues, get_command_regexp, get_nickname_command_regexp

if sys.version_info.major >= 3:
    basestring = (str, bytes)


try:
    _reload = reload
except NameError:
    try:
        _reload = importlib.reload
    except AttributeError:
        _reload = imp.reload


def get_module_description(path):
    good_file = (os.path.isfile(path) and
                 path.endswith('.py') and not path.startswith('_'))
    good_dir = (os.path.isdir(path) and
                os.path.isfile(os.path.join(path, '__init__.py')))
    if good_file:
        name = os.path.basename(path)[:-3]
        return (name, path, imp.PY_SOURCE)
    elif good_dir:
        name = os.path.basename(path)
        return (name, path, imp.PKG_DIRECTORY)
    else:
        return None


def _update_modules_from_dir(modules, directory):
    # Note that this modifies modules in place
    for path in os.listdir(directory):
        path = os.path.join(directory, path)
        result = get_module_description(path)
        if result:
            modules[result[0]] = result[1:]


def enumerate_modules(config, show_all=False):
    """Map the names of modules to the location of their file.

    Return a dict mapping the names of modules to a tuple of the module name,
    the pathname and either `imp.PY_SOURCE` or `imp.PKG_DIRECTORY`. This
    searches the regular modules directory and all directories specified in the
    `core.extra` attribute of the `config` object. If two modules have the same
    name, the last one to be found will be returned and the rest will be
    ignored. Modules are found starting in the regular directory, followed by
    `~/.sopel/modules`, and then through the extra directories in the order
    that the are specified.

    If `show_all` is given as `True`, the `enable` and `exclude`
    configuration options will be ignored, and all modules will be shown
    (though duplicates will still be ignored as above).
    """
    modules = {}

    # First, add modules from the regular modules directory
    main_dir = os.path.dirname(os.path.abspath(__file__))
    modules_dir = os.path.join(main_dir, 'modules')
    _update_modules_from_dir(modules, modules_dir)
    for path in os.listdir(modules_dir):
        break

    # Then, find PyPI installed modules
    # TODO does this work with all possible install mechanisms?
    try:
        import sopel_modules
    except Exception:  # TODO: Be specific
        pass
    else:
        for directory in sopel_modules.__path__:
            _update_modules_from_dir(modules, directory)

    # Next, look in ~/.sopel/modules
    home_modules_dir = os.path.join(config.homedir, 'modules')
    if not os.path.isdir(home_modules_dir):
        os.makedirs(home_modules_dir)
    _update_modules_from_dir(modules, home_modules_dir)

    # Last, look at all the extra directories.
    for directory in config.core.extra:
        _update_modules_from_dir(modules, directory)

    # Coretasks is special. No custom user coretasks.
    ct_path = os.path.join(main_dir, 'coretasks.py')
    modules['coretasks'] = (ct_path, imp.PY_SOURCE)

    # If caller wants all of them, don't apply white and blacklists
    if show_all:
        return modules

    # Apply whitelist, if present
    enable = config.core.enable
    if enable:
        enabled_modules = {'coretasks': modules['coretasks']}
        for module in enable:
            if module in modules:
                enabled_modules[module] = modules[module]
        modules = enabled_modules

    # Apply blacklist, if present
    exclude = config.core.exclude
    for module in exclude:
        if module in modules:
            del modules[module]

    return modules


def trim_docstring(doc):
    """Get the docstring as a series of lines that can be sent"""
    if not doc:
        return []
    lines = doc.expandtabs().splitlines()
    indent = sys.maxsize
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))
    trimmed = [lines[0].strip()]
    if indent < sys.maxsize:
        for line in lines[1:]:
            trimmed.append(line[:].rstrip())
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    return trimmed


def clean_callable(func, config):
    """Compiles the regexes, moves commands into func.rule, fixes up docs and
    puts them in func._docs, and sets defaults"""
    nick = config.core.nick
    alias_nicks = config.core.alias_nicks
    prefix = config.core.prefix
    help_prefix = config.core.help_prefix
    func._docs = {}
    doc = trim_docstring(func.__doc__)
    example = None

    func.unblockable = getattr(func, 'unblockable', False)
    func.priority = getattr(func, 'priority', 'medium')
    func.thread = getattr(func, 'thread', True)
    func.rate = getattr(func, 'rate', 0)
    func.channel_rate = getattr(func, 'channel_rate', 0)
    func.global_rate = getattr(func, 'global_rate', 0)

    if not hasattr(func, 'event'):
        func.event = ['PRIVMSG']
    else:
        if isinstance(func.event, basestring):
            func.event = [func.event.upper()]
        else:
            func.event = [event.upper() for event in func.event]

    if hasattr(func, 'rule'):
        if isinstance(func.rule, basestring):
            func.rule = [func.rule]
        func.rule = [compile_rule(nick, rule, alias_nicks) for rule in func.rule]

    if hasattr(func, 'commands') or hasattr(func, 'nickname_commands'):
        func.rule = getattr(func, 'rule', [])
        for command in getattr(func, 'commands', []):
            regexp = get_command_regexp(prefix, command)
            func.rule.append(regexp)
        for command in getattr(func, 'nickname_commands', []):
            regexp = get_nickname_command_regexp(nick, command, alias_nicks)
            func.rule.append(regexp)
        if hasattr(func, 'example'):
            example = func.example[0]["example"]
            example = example.replace('$nickname', nick)
            if example[0] != help_prefix and not example.startswith(nick):
                example = help_prefix + example[len(help_prefix):]
        if doc or example:
            cmds = []
            cmds.extend(getattr(func, 'commands', []))
            cmds.extend(getattr(func, 'nickname_commands', []))
            for command in cmds:
                func._docs[command] = (doc, example)

    if hasattr(func, 'intents'):
        func.intents = [re.compile(intent, re.IGNORECASE) for intent in func.intents]


def load_module(name, path, type_):
    """Load a module, and sort out the callables and shutdowns"""
    if type_ == imp.PY_SOURCE:
        with open(path) as mod:
            module = imp.load_module(name, mod, path, ('.py', 'U', type_))
    elif type_ == imp.PKG_DIRECTORY:
        module = imp.load_module(name, None, path, ('', '', type_))
    else:
        raise TypeError('Unsupported module type')

    return module, os.path.getmtime(path)


def is_triggerable(obj):
    return any(hasattr(obj, attr) for attr in ('rule', 'intents', 'commands', 'nickname_commands'))


def clean_module(module, config):
    callables = []
    shutdowns = []
    jobs = []
    urls = []

    for obj in itervalues(vars(module)):
        if callable(obj):
            if getattr(obj, '__name__', None) == 'shutdown':
                shutdowns.append(obj)
            elif is_triggerable(obj):
                clean_callable(obj, config)
                callables.append(obj)
            elif hasattr(obj, 'interval'):
                clean_callable(obj, config)
                jobs.append(obj)
            elif hasattr(obj, 'url_regex'):
                urls.append(obj)

    return callables, jobs, shutdowns, urls


# https://github.com/thodnev/reload_all
def reload_all(top_module, max_depth=20, raise_immediately=False,
               pre_reload=None, reload_if=None):
    '''
    A reload function, which recursively traverses through
    all submodules of top_module and reloads them from most-
    nested to least-nested. Only modules containing __file__
    attribute could be reloaded.

    Returns a dict of not reloaded(due to errors) modules:
      key = module, value = exception
    Optional attribute max_depth defines maximum recursion
    limit to avoid infinite loops while tracing
    '''
    # modules to reload: K=module, V=depth
    for_reload = dict()

    def trace_reload(module, depth):  # recursive
        depth += 1

        if type(module) is ModuleType and depth < max_depth:
            # check condition if provided
            if reload_if is not None and not reload_if(module, depth):
                return

            # if module is deeper and could be reloaded
            if for_reload.get(module, 0) < depth and hasattr(module, '__file__'):
                for_reload[module] = depth

            # trace through all attributes recursively
            for name, attr in module.__dict__.items():
                trace_reload(attr, depth)

    # start tracing
    trace_reload(top_module, 0)
    reload_list = sorted(for_reload, reverse=True, key=lambda k: for_reload[k])
    not_reloaded = dict()

    for module in reload_list:
        if pre_reload is not None:
            pre_reload(module)

        try:
            _reload(module)
        except Exception:  # catch and write all errors
            if raise_immediately:
                raise
            not_reloaded[module] = sys.exc_info()[0]

    return not_reloaded
