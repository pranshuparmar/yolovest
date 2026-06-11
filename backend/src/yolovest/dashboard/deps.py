"""Per-app dependency bundle handed to every route module.

Route modules receive the auth dependencies as plain callables (they
are FastAPI dependency functions closing over the app's password
container and throttle, built in create_app).
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Deps:
    verify_credentials: Callable[..., str]
    verify_download_credentials: Callable[..., str]
    # Mutable password container shared with the login/Basic-auth
    # closures — change-password mutates it in place.
    password: dict[str, str]
