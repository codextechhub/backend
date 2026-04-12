# vs_schools/pagination.py

from rest_framework.pagination import PageNumberPagination

class SchoolPagination(PageNumberPagination):
    page_size = 10  # default per page
    page_size_query_param = "page_size" # allow ?page_size=50
    max_page_size = 100 # hard cap