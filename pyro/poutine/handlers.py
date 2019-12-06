"""
Poutine is a library of composable effect handlers for recording and modifying the
behavior of Pyro programs. These lower-level ingredients simplify the implementation
of new inference algorithms and behavior.

Handlers can be used as higher-order functions, decorators, or context managers
to modify the behavior of functions or blocks of code:

For example, consider the following Pyro program:

    >>> def model(x):
    ...     s = pyro.param("s", torch.tensor(0.5))
    ...     z = pyro.sample("z", dist.Normal(x, s))
    ...     return z ** 2

We can mark sample sites as observed using ``condition``,
which returns a callable with the same input and output signatures as ``model``:

    >>> conditioned_model = poutine.condition(model, data={"z": 1.0})

We can also use handlers as decorators:

    >>> @pyro.condition(data={"z": 1.0})
    ... def model(x):
    ...     s = pyro.param("s", torch.tensor(0.5))
    ...     z = pyro.sample("z", dist.Normal(x, s))
    ...     return z ** 2

Or as context managers:

    >>> with pyro.condition(data={"z": 1.0}):
    ...     s = pyro.param("s", torch.tensor(0.5))
    ...     z = pyro.sample("z", dist.Normal(0., s))
    ...     y = z ** 2

Handlers compose freely:

    >>> conditioned_model = poutine.condition(model, data={"z": 1.0})
    >>> traced_model = poutine.trace(conditioned_model)

Many inference algorithms or algorithmic components can be implemented
in just a few lines of code::

    guide_tr = poutine.trace(guide).get_trace(...)
    model_tr = poutine.trace(poutine.replay(conditioned_model, trace=guide_tr)).get_trace(...)
    monte_carlo_elbo = model_tr.log_prob_sum() - guide_tr.log_prob_sum()
"""

import functools
import re

from pyro.poutine import util
from pyro.poutine.messenger import Messenger
from pyro.util import get_rng_state, set_rng_seed, set_rng_state

from .block_messenger import BlockMessenger
from .broadcast_messenger import BroadcastMessenger
from .condition_messenger import ConditionMessenger
from .do_messenger import DoMessenger
from .enum_messenger import EnumMessenger
from .escape_messenger import EscapeMessenger
from .infer_config_messenger import InferConfigMessenger
from .lift_messenger import LiftMessenger
from .markov_messenger import MarkovMessenger
from .mask_messenger import MaskMessenger
from .plate_messenger import PlateMessenger  # noqa F403
from .replay_messenger import ReplayMessenger
from .runtime import NonlocalExit
from .scale_messenger import ScaleMessenger
from .trace_messenger import TraceMessenger
from .uncondition_messenger import UnconditionMessenger


class SeedMessenger(Messenger):
    """
    Handler to set the random number generator to a pre-defined state by setting its
    seed. This is the same as calling :func:`pyro.set_rng_seed` before the
    call to `fn`. This handler has no additional effect on primitive statements on the
    standard Pyro backend, but it might intercept ``pyro.sample`` calls in other
    backends. e.g. the NumPy backend.

    :param fn: a stochastic function (callable containing Pyro primitive calls).
    :param int rng_seed: rng seed.
    """
    def __init__(self, rng_seed):
        assert isinstance(rng_seed, int)
        self.rng_seed = rng_seed
        super(SeedMessenger, self).__init__()

    def __enter__(self):
        self.old_state = get_rng_state()
        set_rng_seed(self.rng_seed)

    def __exit__(self, type, value, traceback):
        set_rng_state(self.old_state)


############################################
# Begin primitive operations
############################################


_msngrs = [
    BlockMessenger,
    BroadcastMessenger,
    ConditionMessenger,
    DoMessenger,
    EnumMessenger,
    EscapeMessenger,
    InferConfigMessenger,
    LiftMessenger,
    MarkovMessenger,
    MaskMessenger,
    ReplayMessenger,
    ScaleMessenger,
    SeedMessenger,
    TraceMessenger,
    UnconditionMessenger,
]


def _make_handler(msngr_cls):

    def handler(fn=None, *args, **kwargs):
        if fn is not None and not callable(fn):
            raise ValueError(
                "{} is not callable, did you mean to pass it as a keyword arg?".format(fn))
        msngr = msngr_cls(*args, **kwargs)
        return msngr(fn) if fn is not None else msngr

    return handler


_re1 = re.compile('(.)([A-Z][a-z]+)')
_re2 = re.compile('([a-z0-9])([A-Z])')
for _msngr_cls in _msngrs:
    _handler_name = _re2.sub(
        r'\1_\2', _re1.sub(r'\1_\2', _msngr_cls.__name__.split("Messenger")[0])).lower()
    _handler = _make_handler(_msngr_cls)
    _handler.__module__ = __name__
    _handler.__doc__ = _msngr_cls.__doc__
    _handler.__name__ = _handler_name
    locals()[_handler_name] = _handler


#########################################
# Begin composite operations
#########################################

def queue(fn=None, queue=None, max_tries=None,
          extend_fn=None, escape_fn=None, num_samples=None):
    """
    Used in sequential enumeration over discrete variables.

    Given a stochastic function and a queue,
    return a return value from a complete trace in the queue.

    :param fn: a stochastic function (callable containing Pyro primitive calls)
    :param queue: a queue data structure like multiprocessing.Queue to hold partial traces
    :param max_tries: maximum number of attempts to compute a single complete trace
    :param extend_fn: function (possibly stochastic) that takes a partial trace and a site,
        and returns a list of extended traces
    :param escape_fn: function (possibly stochastic) that takes a partial trace and a site,
        and returns a boolean value to decide whether to exit
    :param num_samples: optional number of extended traces for extend_fn to return
    :returns: stochastic function decorated with poutine logic
    """

    if max_tries is None:
        max_tries = int(1e6)

    if extend_fn is None:
        extend_fn = util.enum_extend

    if escape_fn is None:
        escape_fn = util.discrete_escape

    if num_samples is None:
        num_samples = -1

    def wrapper(wrapped):
        def _fn(*args, **kwargs):

            for i in range(max_tries):
                assert not queue.empty(), \
                    "trying to get() from an empty queue will deadlock"

                next_trace = queue.get()
                try:
                    ftr = trace(escape(replay(wrapped, trace=next_trace),  # noqa: F821
                                       escape_fn=functools.partial(escape_fn,
                                                                   next_trace)))
                    return ftr(*args, **kwargs)
                except NonlocalExit as site_container:
                    site_container.reset_stack()
                    for tr in extend_fn(ftr.trace.copy(), site_container.site,
                                        num_samples=num_samples):
                        queue.put(tr)

            raise ValueError("max tries ({}) exceeded".format(str(max_tries)))
        return _fn

    return wrapper(fn) if fn is not None else wrapper


def markov(fn=None, history=1, keep=False, dim=None, name=None):
    """
    Markov dependency declaration.

    This can be used in a variety of ways:
    - as a context manager
    - as a decorator for recursive functions
    - as an iterator for markov chains

    :param int history: The number of previous contexts visible from the
        current context. Defaults to 1. If zero, this is similar to
        :class:`pyro.plate`.
    :param bool keep: If true, frames are replayable. This is important
        when branching: if ``keep=True``, neighboring branches at the same
        level can depend on each other; if ``keep=False``, neighboring branches
        are independent (conditioned on their share"
    :param int dim: An optional dimension to use for this independence index.
        Interface stub, behavior not yet implemented.
    :param str name: An optional unique name to help inference algorithms match
        :func:`pyro.markov` sites between models and guides.
        Interface stub, behavior not yet implemented.
    """
    if fn is None:
        # Used as a decorator with bound args
        return MarkovMessenger(history=history, keep=keep, dim=dim, name=name)
    if not callable(fn):
        # Used as a generator
        return MarkovMessenger(history=history, keep=keep, dim=dim, name=name).generator(iterable=fn)
    # Used as a decorator with bound args
    return MarkovMessenger(history=history, keep=keep, dim=dim, name=name)(fn)
