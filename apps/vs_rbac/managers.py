"""
Custom QuerySet and Manager for automatic tenant-aware filtering.

Usage in models:
    class Student(models.Model):
        institution = models.ForeignKey(Institution, ...)
        branch = models.ForeignKey(Branch, ...)
        
        objects = TenantAwareManager()  # <-- Add this
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q


class TenantAwareQuerySet(models.QuerySet):
    """
    QuerySet that automatically filters by institution context.
    
    If request.institution exists, all queries are scoped to that institution.
    Vision staff bypass this scoping.
    """
    
    def __init__(self, *args, **kwargs):
        self._institution_context = None
        super().__init__(*args, **kwargs)
    
    def _clone(self):
        clone = super()._clone()
        clone._institution_context = self._institution_context
        return clone
    
    def for_institution(self, institution):
        """Explicitly set institution context for this queryset."""
        clone = self._clone()
        clone._institution_context = institution
        return clone
    
    def _filter_by_institution(self):
        """Apply institution filter if context is set."""
        if self._institution_context is None:
            return self
        
        # Determine the institution FK field name
        # Convention: models have either 'institution' or 'branch__institution'
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        if 'institution' in model_fields:
            return self.filter(institution=self._institution_context)
        elif 'branch' in model_fields:
            return self.filter(branch__institution=self._institution_context)
        else:
            # Model doesn't have institution FK - return unfiltered
            return self
    
    def all(self):
        return self._filter_by_institution()
    
    def filter(self, *args, **kwargs):
        return super().filter(*args, **kwargs)._filter_by_institution()
    
    def exclude(self, *args, **kwargs):
        return super().exclude(*args, **kwargs)._filter_by_institution()


class TenantAwareManager(models.Manager):
    """
    Manager that returns TenantAwareQuerySet.
    
    Automatically injects institution filtering when request context is available.
    """
    
    def get_queryset(self):
        qs = TenantAwareQuerySet(self.model, using=self._db)
        
        # Try to get institution from thread-local storage or middleware
        # This requires setting thread-local in middleware
        from threading import local
        _thread_locals = getattr(local(), 'request_context', None)
        
        if _thread_locals and hasattr(_thread_locals, 'institution'):
            institution = _thread_locals.institution
            if institution:
                qs = qs.for_institution(institution)
        
        return qs
    
    def for_institution(self, institution):
        """Explicitly scope queries to an institution."""
        return self.get_queryset().for_institution(institution),