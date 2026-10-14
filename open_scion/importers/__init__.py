"""Adapters for importing existing version-control histories."""

from .git import import_git

__all__ = ["import_git"]
