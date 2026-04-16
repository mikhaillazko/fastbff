from fastapi.params import Depends as ParamDepends


class WebDepends(ParamDepends):
    """Marks a dependency as belonging to the web layer.

    Use instead of ``Depends`` for parameters that FastAPI should resolve —
    authentication context, transport, pagination, and similar concerns.
    These parameters are excluded from the internal DI graph.
    """
