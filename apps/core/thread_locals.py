"""
Thread-local storage for request context.

Shared across middleware and managers to enable automatic institution filtering.
"""
from threading import local

# Global thread-local storage instance
_thread_locals = local()


def get_current_institution():
    """Get the institution from thread-local storage."""
    return getattr(_thread_locals, 'institution', None)


def set_current_institution(institution):
    """Set the institution in thread-local storage."""
    _thread_locals.institution = institution


def clear_current_institution():
    """Clear the institution from thread-local storage."""
    if hasattr(_thread_locals, 'institution'):
        delattr(_thread_locals, 'institution')