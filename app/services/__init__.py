"""Application services with lazy compatibility exports."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.database import database_service
    from app.services.llm import LLMRegistry, llm_service

__all__ = ["database_service", "LLMRegistry", "llm_service"]


def __getattr__(name: str) -> object:
    """Load legacy package exports only when explicitly requested."""
    if name == "database_service":
        return import_module("app.services.database").database_service
    if name in {"LLMRegistry", "llm_service"}:
        return getattr(import_module("app.services.llm"), name)
    raise AttributeError(name)
