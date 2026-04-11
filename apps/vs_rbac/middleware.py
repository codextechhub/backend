"""
School context enforcement middleware.

This middleware injects school context into every request and enforces
tenant boundary isolation at the ORM level.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from vs_schools.models import School
from core.thread_locals import set_current_school, clear_current_school


def _get_school_from_request(request: HttpRequest):
    """
    Extract school from request context.
    
    Priority order:
    1. Explicitly set request.school_id (e.g., from API token/JWT)
    2. User's default school (if school-scoped user)
    3. Vision staff can access all schools (no filter)
    """
    # If already resolved in this request
    if hasattr(request, "_cached_school"):
        return request._cached_school
    
    user = getattr(request, "user", None)
    
    # Unauthenticated or no user
    if not user or not user.is_authenticated:
        request._cached_school = None
        return None
    
    # Vision staff bypass school scoping
    if getattr(user, "user_type", None) == "VS_STAFF":
        request._cached_school = None
        return None
    
    # Check if school was explicitly set (e.g., via JWT claim or header)
    if hasattr(request, "school_id"):
        try:
            school = School.objects.get(pk=request.school_id)
            request._cached_school = school
            return school
        except School.DoesNotExist:
            raise PermissionDenied("Invalid school context.")
    
    # Fall back to user's default school
    user_school_id = getattr(user, "school_id", None)
    
    if user_school_id:
        try:
            school = School.objects.get(pk=user_school_id)
            request._cached_school = school
            return school
        except School.DoesNotExist:
            raise PermissionDenied("User's school does not exist.")
    
    # No school context available
    request._cached_school = None
    return None


class TenantContextMiddleware:
    """
    Injects school context into every request.
    
    Sets request.school as a lazy-loaded property AND stores it in
    thread-local storage for automatic ORM filtering via TenantAwareManager.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request: HttpRequest):
        # Clear any previous school context from thread-local
        clear_current_school()
        
        # Lazy-load school to avoid unnecessary DB queries
        request.school = SimpleLazyObject(
            lambda: _get_school_from_request(request)
        )
        print(f"TenantContextMiddleware: Resolved school={request.school} for user={getattr(request.user, 'id', None)}")  # Debug log
        # Force evaluation of the lazy object and set in thread-local for ORM access
        # Accessing via bool() triggers SimpleLazyObject._setup()
        if request.school:
            set_current_school(request.school)
        
        # Process request
        response = self.get_response(request)
        
        # Clean up thread-local after request completes
        clear_current_school()
        
        return response


class TenantBoundaryEnforcementMiddleware:
    """
    Enforces tenant boundary checks on sensitive operations.
    
    This middleware runs AFTER authentication and tenant context injection.
    It validates that:
    - School-scoped users can only access their own school's data
    - Cross-school references are blocked
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request: HttpRequest):
        user = getattr(request, "user", None)
        
        # Skip enforcement for unauthenticated requests (handled by auth layer)
        if not user or not user.is_authenticated:
            return self.get_response(request)
        
        # Skip enforcement for Vision staff (they can access all schools)
        if getattr(user, "user_type", None) == "VS_STAFF":
            return self.get_response(request)
        
        # Enforce that school-scoped users have valid school context
        school = getattr(request, "school", None)
        
        if not school:
            # School user with no school context = security violation
            user_type = getattr(user, "user_type", None)
            if user_type in {"SCHOOL_ADMIN", "STAFF", "STUDENT", "PARENT"}:
                raise PermissionDenied(
                    "School context required for school-scoped users."
                )
        
        response = self.get_response(request)
        return response