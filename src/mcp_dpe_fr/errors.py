"""Error types. Messages are written for an LLM: they say what to do next."""


class DpeError(Exception):
    """Base class; `str(e)` is safe to show to the model."""


class InvalidInput(DpeError):
    pass


class InsufficientData(DpeError):
    pass


class UpstreamError(DpeError):
    pass
