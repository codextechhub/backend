"""
Thread-local storage for request context.

Shared across middleware and managers to enable automatic school filtering.
"""
from threading import local

# Global thread-local storage instance
_thread_locals = local()


def get_current_school():
    """Get the school from thread-local storage."""
    return getattr(_thread_locals, 'school', None)


def set_current_school(school):
    """Set the school in thread-local storage."""
    _thread_locals.school = school


def clear_current_school():
    """Clear the school from thread-local storage."""
    if hasattr(_thread_locals, 'school'):
        delattr(_thread_locals, 'school')