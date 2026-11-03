"""Thin import shim so the code runs on either `httpx` or its drop-in fork `httpx2`."""

try:  # pragma: no cover - which one is present depends on the environment
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    import httpx2 as httpx  # type: ignore

__all__ = ["httpx"]
