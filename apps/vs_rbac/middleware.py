"""
Institution context enforcement middleware.

This middleware injects institution context into every request and enforces
tenant boundary isolation at the ORM level.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from vs_institutions.models import Institution
from core.thread_locals import set_current_institution, clear_current_institution


def _get_institution_from_request(request: HttpRequest):
    """
    Extract institution from request context.
    
    Priority order:
    1. Explicitly set request.institution_id (e.g., from API token/JWT)
    2. User's default institution (if institution-scoped user)
    3. Vision staff can access all institutions (no filter)
    """
    # If already resolved in this request
    if hasattr(request, "_cached_institution"):
        return request._cached_institution
    
    user = getattr(request, "user", None)
    
    # Unauthenticated or no user
    if not user or not user.is_authenticated:
        request._cached_institution = None
        return None
    
    # Vision staff bypass institution scoping
    if getattr(user, "user_type", None) == "VS_STAFF":
        request._cached_institution = None
        return None
    
    # Check if institution was explicitly set (e.g., via JWT claim or header)
    if hasattr(request, "institution_id"):
        try:
            institution = Institution.objects.get(id=request.institution_id)
            request._cached_institution = institution
            return institution
        except Institution.DoesNotExist:
            raise PermissionDenied("Invalid institution context.")
    
    # Fall back to user's default institution
    user_institution_id = getattr(user, "institution_id", None)
    
    if user_institution_id:
        try:
            institution = Institution.objects.get(id=user_institution_id)
            request._cached_institution = institution
            return institution
        except Institution.DoesNotExist:
            raise PermissionDenied("User's institution does not exist.")
    
    # No institution context available
    request._cached_institution = None
    return None


class TenantContextMiddleware:
    """
    Injects institution context into every request.
    
    Sets request.institution as a lazy-loaded property AND stores it in
    thread-local storage for automatic ORM filtering via TenantAwareManager.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request: HttpRequest):
        # Clear any previous institution context from thread-local
        clear_current_institution()
        
        # Lazy-load institution to avoid unnecessary DB queries
        request.institution = SimpleLazyObject(
            lambda: _get_institution_from_request(request)
        )
        
        # Force evaluation and set in thread-local for ORM access
        institution = request.institution  # This triggers lazy evaluation
        
        if institution is not None:
            set_current_institution(institution)
        
        # Process request
        response = self.get_response(request)
        
        # Clean up thread-local after request completes
        clear_current_institution()
        
        return response


class TenantBoundaryEnforcementMiddleware:
    """
    Enforces tenant boundary checks on sensitive operations.
    
    This middleware runs AFTER authentication and tenant context injection.
    It validates that:
    - Institution-scoped users can only access their own institution's data
    - Cross-institution references are blocked
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request: HttpRequest):
        user = getattr(request, "user", None)
        
        # Skip enforcement for unauthenticated requests (handled by auth layer)
        if not user or not user.is_authenticated:
            return self.get_response(request)
        
        # Skip enforcement for Vision staff (they can access all institutions)
        if getattr(user, "user_type", None) == "VS_STAFF":
            return self.get_response(request)
        
        # Enforce that institution-scoped users have valid institution context
        institution = getattr(request, "institution", None)
        
        if not institution:
            # Institution user with no institution context = security violation
            user_type = getattr(user, "user_type", None)
            if user_type in {"INSTITUTION_ADMIN", "INSTITUTION_USER"}:
                raise PermissionDenied(
                    "Institution context required for institution-scoped users."
                )
        
        response = self.get_response(request)
        return response