# Lazy exports to avoid pulling in the full FastAPI import chain
# when only server.tenant or other submodules are needed.


def create_app(*args, **kwargs):
    from .app import create_app as _create_app
    return _create_app(*args, **kwargs)


def create_saas_app(*args, **kwargs):
    from .app import create_saas_app as _create_saas_app
    return _create_saas_app(*args, **kwargs)


__all__ = ["create_app", "create_saas_app"]

