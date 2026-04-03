# vs_institutions/pagination.py

from rest_framework.pagination import PageNumberPagination

class InstitutionPagination(PageNumberPagination):
    page_size = 20                      # default per page
    page_size_query_param = "page_size" # allow ?page_size=50
    max_page_size = 100                 # hard cap