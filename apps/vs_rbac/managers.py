"""
Custom QuerySet and Manager for automatic tenant-aware filtering.

Usage in models:
    class Student(models.Model):
        institution = models.ForeignKey(Institution, ...)
        branch = models.ForeignKey(Branch, ...)
        
        objects = TenantAwareManager()  # <-- Add this
        all_objects = models.Manager()  # <-- Unscoped fallback
"""
from __future__ import annotations

from django.db import models

from core.thread_locals import get_current_institution

# TODO: Add objects = TenantAwareManager() and all_objects = models.Manager() to your models to enable automatic tenant-aware filtering and unscoped access.


class TenantAwareQuerySet(models.QuerySet):
    """
    QuerySet that automatically filters by institution context.
    
    If request.institution exists in thread-local storage, all queries 
    are scoped to that institution. Vision staff bypass this scoping.
    """
    
    def __init__(self, *args, **kwargs):
        self._institution_context = None
        super().__init__(*args, **kwargs)
    
    def _clone(self):
        """Preserve institution context when cloning queryset."""
        clone = super()._clone()
        clone._institution_context = self._institution_context
        return clone
    
    def for_institution(self, institution):
        """
        Explicitly set institution context for this queryset.
        
        Use this to override automatic scoping or query a different institution.
        """
        clone = self._clone()
        clone._institution_context = institution
        return clone
    
    def _filter_by_institution(self):
        """
        Apply institution filter if context is set.
        
        Automatically detects whether the model has:
        - Direct 'institution' FK
        - Indirect 'branch__institution' FK
        """
        if self._institution_context is None:
            return self
        
        # Determine the institution FK field name
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        if 'institution' in model_fields:
            # Direct FK: institution = models.ForeignKey(Institution)
            return self.filter(institution=self._institution_context)
        
        elif 'branch' in model_fields:
            # Indirect FK: branch = models.ForeignKey(Branch)
            # Branch has institution FK
            return self.filter(branch__institution=self._institution_context)
        
        else:
            # Model doesn't have institution FK - return unfiltered
            # This is safe for platform-level models (Institution, VisionStaff, etc.)
            return self
    
    def all(self):
        """Override all() to apply institution filtering."""
        return super().all()._filter_by_institution()
    
    def filter(self, *args, **kwargs):
        """Override filter() to apply institution filtering."""
        return super().filter(*args, **kwargs)._filter_by_institution()
    
    def exclude(self, *args, **kwargs):
        """Override exclude() to apply institution filtering."""
        return super().exclude(*args, **kwargs)._filter_by_institution()


class TenantAwareManager(models.Manager):
    """
    Manager that returns TenantAwareQuerySet.
    
    Automatically injects institution filtering when request context 
    is available via thread-local storage (set by middleware).
    """
    
    def get_queryset(self):
        """
        Return a queryset with automatic institution filtering.
        
        Reads institution from thread-local storage set by 
        TenantContextMiddleware during request processing.
        """
        qs = TenantAwareQuerySet(self.model, using=self._db)
        
        # Get institution from shared thread-local storage
        institution = get_current_institution()
        
        if institution:
            qs = qs.for_institution(institution)
        
        return qs
    
    def for_institution(self, institution):
        """
        Explicitly scope queries to an institution.
        
        Use when you need to override automatic scoping or 
        query a different institution than the current context.
        """
        return self.get_queryset().for_institution(institution)