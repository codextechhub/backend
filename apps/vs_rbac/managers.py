"""
Custom QuerySet and Manager for automatic tenant-aware filtering.

Usage in models:
    class Student(models.Model):
        school = models.ForeignKey(School, ...)
        branch = models.ForeignKey(Branch, ...)
        
        objects = TenantAwareManager()  # <-- Add this
        all_objects = models.Manager()  # <-- Unscoped fallback
"""
from __future__ import annotations

from django.db import models

from core.thread_locals import get_current_school

# TODO: Add objects = TenantAwareManager() and all_objects = models.Manager() to your models to enable automatic tenant-aware filtering and unscoped access.


class TenantAwareQuerySet(models.QuerySet):
    """
    QuerySet that automatically filters by school context.
    
    If request.school exists in thread-local storage, all queries 
    are scoped to that school. Vision staff bypass this scoping.
    """
    
    def __init__(self, *args, **kwargs):
        self._school_context = None
        super().__init__(*args, **kwargs)
    
    def _clone(self):
        """Preserve school context when cloning queryset."""
        clone = super()._clone()
        clone._school_context = self._school_context
        return clone
    
    def for_school(self, school):
        """
        Explicitly set school context for this queryset.
        
        Use this to override automatic scoping or query a different school.
        """
        clone = self._clone()
        clone._school_context = school
        return clone
    
    def _filter_by_school(self):
        """
        Apply school filter if context is set.
        
        Automatically detects whether the model has:
        - Direct 'school' FK
        - Indirect 'branch__school' FK
        """
        if self._school_context is None:
            return self
        
        # Determine the school FK field name
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        if 'school' in model_fields:
            # Direct FK: school = models.ForeignKey(School)
            return self.filter(school=self._school_context)
        
        elif 'branch' in model_fields:
            # Indirect FK: branch = models.ForeignKey(Branch)
            # Branch has school FK
            return self.filter(branch__school=self._school_context)
        
        else:
            # Model doesn't have school FK - return unfiltered
            # This is safe for platform-level models (School, VisionStaff, etc.)
            return self
    
    def all(self):
        """Override all() to apply school filtering."""
        return super().all()._filter_by_school()
    
    def filter(self, *args, **kwargs):
        """Override filter() to apply school filtering."""
        return super().filter(*args, **kwargs)._filter_by_school()
    
    def exclude(self, *args, **kwargs):
        """Override exclude() to apply school filtering."""
        return super().exclude(*args, **kwargs)._filter_by_school()


class TenantAwareManager(models.Manager):
    """
    Manager that returns TenantAwareQuerySet.
    
    Automatically injects school filtering when request context 
    is available via thread-local storage (set by middleware).
    """
    
    def get_queryset(self):
        """
        Return a queryset with automatic school filtering.
        
        Reads school from thread-local storage set by 
        TenantContextMiddleware during request processing.
        """
        qs = TenantAwareQuerySet(self.model, using=self._db)
        
        # Get school from shared thread-local storage
        school = get_current_school()
        
        if school:
            qs = qs.for_school(school)
        
        return qs
    
    def for_school(self, school):
        """
        Explicitly scope queries to an school.
        
        Use when you need to override automatic scoping or 
        query a different school than the current context.
        """
        return self.get_queryset().for_school(school)