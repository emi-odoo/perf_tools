import functools
import logging
import threading

import odoo
from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)

_BUFFER_KEY = 'performance_tools_method_executions.pending'
_MAX_PENDING = 1000 # per transaction, so a pathological one can't eat the memory

# the records below are created through the ORM, i.e. through patched methods:
# this thread local flag keeps that write from logging itself in a loop
_local = threading.local()


class SlowExecution(models.Model):
    _name = 'slow.execution'
    _description = "Slow Execution"

    model = fields.Char(index=True)
    method = fields.Char()
    filename = fields.Char()
    line_number = fields.Integer()
    duration_ms = fields.Float(help="Wall time of the call, nested calls included")
    own_duration_ms = fields.Float(
        index=True,
        help="Wall time of the call minus the time spent in the instrumented calls it made",
    )
    depth = fields.Integer(help="Number of instrumented calls this one runs inside of, 0 for an entry point")
    caller_model = fields.Char(index=True)
    caller_method = fields.Char()
    caller_filename = fields.Char()
    caller_line_number = fields.Integer()

    @api.model
    def _log_slow_call(self, callee, caller, duration_ms, own_duration_ms, depth):
        """Buffer one slow call, to be stored when the current cursor commits.

        Nothing is written here: we are in the middle of a business transaction
        that may still be rolled back, and creating a record per call would slow
        down the very thing we are measuring.
        """
        if getattr(_local, 'flushing', False):
            return
        cr = self.env.cr
        pending = cr.postcommit.data.get(_BUFFER_KEY)
        if pending is None:
            # the buffer and its hook only live for the current transaction:
            # `postcommit` is cleared both by commit() and by rollback()
            pending = cr.postcommit.data[_BUFFER_KEY] = []
            cr.postcommit.add(functools.partial(self._postcommit_create_logs, cr.dbname, pending))
        if len(pending) < _MAX_PENDING:
            pending.append((callee, caller, duration_ms, own_duration_ms, depth))

    @api.model
    def _postcommit_create_logs(self, dbname, pending):
        """Postcommit hook: create the records buffered by :meth:`_log_slow_call`.

        It runs once the transaction is over, hence the cursor of its own. It
        must never raise either: an exception here would surface in whoever
        called ``commit()``, i.e. it would break an already successful request.
        """
        if not pending:
            return
        vals_list = [{
            'model': callee.model,
            'method': callee.function_name,
            'filename': callee.filename,
            'line_number': callee.line_number,
            'duration_ms': duration_ms,
            'own_duration_ms': own_duration_ms,
            'depth': depth,
            # a caller taken from the call stack is known by its definition
            # line, one taken from a frame by the line it was calling us from
            'caller_model': caller.model if caller else False,
            'caller_method': caller.function_name if caller else False,
            'caller_filename': caller.filename if caller else False,
            'caller_line_number': caller.line_number if caller else 0,
        } for callee, caller, duration_ms, own_duration_ms, depth in pending]
        pending.clear()
        _local.flushing = True
        try:
            registry = odoo.registry(dbname)
            if self._name not in registry.models:
                # the module may have been uninstalled since we buffered
                return
            with registry.cursor() as cr:
                # a fresh registry may hold classes this record set doesn't know
                api.Environment(cr, SUPERUSER_ID, {})[self._name].create(vals_list)
        except Exception:
            _logger.exception("Could not store %s slow method execution(s)", len(vals_list))
        finally:
            _local.flushing = False
