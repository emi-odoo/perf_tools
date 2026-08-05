
import functools
import inspect
import threading
import time

from dataclasses import dataclass, replace
from types import FrameType
from typing import Optional

from odoo.models import BaseModel
from odoo.modules.registry import Registry

_orig_setup = Registry.setup_models
_PATCHED = '_perf_tools_patched'
_THRESHOLD_MIN_TIME_MS = 90 # we won't log anything that spent less than this on its own
_THRESHOLD_ROOT_TIME_MS = 200 # an entry point is worth logging even when its children ate the time
_LOG_MODEL = 'slow.execution'

# the instrumented calls currently running in this thread, innermost last, as
# [callee, time spent in instrumented children] pairs
_local = threading.local()

@dataclass(frozen=True)
class CallIdentifier:
    model : str
    function_name : str
    filename : str
    line_number : int

    @classmethod
    def from_frame(cls, frame: FrameType) -> Optional["CallIdentifier"]:
        code = frame.f_code
        if 'self' not in code.co_varnames[:code.co_argcount]:
            return None
        self_obj = frame.f_locals.get('self')
        fname = code.co_filename
        if (
            self_obj is not None
            and isinstance(self_obj, BaseModel)
            and '/odoo/models.py' not in fname
            and '/odoo/fields.py' not in fname
            and '/odoo/api.py' not in fname
        ):
            return cls(self_obj._name, code.co_name, fname, frame.f_lineno)
        return None

    @staticmethod
    def _class_model_name(odoo_class) -> str:
        own = vars(odoo_class)
        name = own.get('_name') or own.get('_inherit')
        if isinstance(name, (list, tuple)):
            name = name[0] if name else None
        return name or getattr(odoo_class, '_name', None) or odoo_class.__name__

    @classmethod
    def from_function(cls, odoo_class, func_name: str, func) -> "CallIdentifier":
        code = inspect.unwrap(func).__code__
        return cls(
            model=cls._class_model_name(odoo_class),
            function_name=func_name,
            filename=code.co_filename,
            line_number=code.co_firstlineno,
        )

    def on_record(self, self_obj) -> "CallIdentifier":
        """Self, but named after the model the call actually ran on.

        A generic method is defined on a class that is not the model it runs on:
        `web_read` on `base`, `read` on `BaseModel`, anything on a mixin. Without
        this, every single one of its calls is logged under that defining name
        instead of `sale.order`. The file and line still point at the definition.
        """
        if isinstance(self_obj, BaseModel) and self_obj._name != self.model:
            return replace(self, model=self_obj._name)
        return self

def _find_business_caller(frame=None) -> tuple[CallIdentifier, FrameType]:
    frame = frame or inspect.currentframe()
    while frame:
        if identifier:=CallIdentifier.from_frame(frame):
            return identifier, frame
        frame = frame.f_back
    return None, frame

def _unwrap(raw):
    return raw.__func__ if isinstance(raw, (classmethod, staticmethod)) else raw


def _already_patched(raw) -> bool:
    return getattr(_unwrap(raw), _PATCHED, False)


def _call_stack() -> list:
    try:
        return _local.stack
    except AttributeError:
        stack = _local.stack = []
        return stack


def _schedule_log_creation(model_obj, callee, caller, tot_time, own_time, depth):
    # a classmethod gets its class as first argument, a plain function may get
    # anything: only a record set gives us the environment to log through
    if not isinstance(model_obj, BaseModel):
        return
    env = model_obj.env
    if _LOG_MODEL not in env.registry.models:
        # this patch is process wide, the module may not be installed here
        return
    env[_LOG_MODEL]._log_slow_call(callee, caller, tot_time, own_time, depth)


def _add_log_to_func(odoo_class, func_name, raw):
    is_descriptor = isinstance(raw, (classmethod, staticmethod))
    original = _unwrap(raw)
    callee = CallIdentifier.from_function(odoo_class, func_name, original)
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        stack = _call_stack()
        # the record set is kept as is: resolving the model it stands for is
        # only worth doing for the few calls we end up logging
        stack.append([callee, 0.0, args[0] if args else kwargs.get('self')])
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            tot_time = (time.perf_counter() - start) * 1000
            _, children_time, model_obj = stack.pop()
            own_time = tot_time - children_time
            if stack:
                # to our caller, the whole of our duration is time in a child
                stack[-1][1] += tot_time
            is_root = not stack
            if (
                own_time > _THRESHOLD_MIN_TIME_MS
                or (is_root and tot_time > _THRESHOLD_ROOT_TIME_MS)
            ):
                # the call we are nested in, or, for an entry point, whatever
                # frame got us into the ORM in the first place
                caller = (
                    _find_business_caller()[0] if is_root
                    else stack[-1][0].on_record(stack[-1][2])
                )
                _schedule_log_creation(
                    model_obj, callee.on_record(model_obj), caller,
                    tot_time, own_time, len(stack),
                )

    setattr(wrapper, _PATCHED, True)
    setattr(odoo_class, func_name, type(raw)(wrapper) if is_descriptor else wrapper)


def _patch_functions(odoo_class):
    for func_name, raw in list(vars(odoo_class).items()):
        if not (inspect.isfunction(raw) or isinstance(raw, (classmethod, staticmethod))):
            continue
        if _already_patched(raw):
            continue
        _add_log_to_func(odoo_class, func_name, raw)

MODELS_TO_EXCLUDE = ['ir.http', _LOG_MODEL] # never instrument the logger itself


def _patch_registry(registry):

    already_patched = set()
    for model in registry.models.values():
        for klass in model.mro():
            if getattr(klass, '_name', False) in MODELS_TO_EXCLUDE:
                continue 
            if klass in already_patched:
                continue
            _patch_functions(klass)
            already_patched.add(klass)


@functools.wraps(_orig_setup)
def setup_models(self, cr):
    _orig_setup(self, cr)
    _patch_registry(self)

Registry.setup_models = setup_models
