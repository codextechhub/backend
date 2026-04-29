# core/pagination.py

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
import math


class XVSPagination(PageNumberPagination):
    page_size_query_param = 'page_size'  # allows ?page_size=N override
    max_page_size = 100
    page_query_param = 'page'            # ?page=2

    def get_paginated_response(self, data):
        total_items = self.page.paginator.count
        page_size = self.get_page_size(self.request)
        total_pages = math.ceil(total_items / page_size) if page_size else 1
        current_page = self.page.number

        return Response({
            "success": True,
            "message": "Data retrieved successfully",
            "pagination": {
                "currentPage": current_page,
                "pageSize": page_size,
                "totalItems": total_items,
                "totalPages": total_pages,
                "next": self.get_next_link(),
                "previous": self.get_previous_link()
            },
            "data": data,
        })

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "message": {"type": "string"},
                "pagination": {
                    "type": "object",
                    "properties": {
                        "currentPage": {"type": "integer"},
                        "pageSize": {"type": "integer"},
                        "totalItems": {"type": "integer"},
                        "totalPages": {"type": "integer"},
                        "next": {"type": "string", "nullable": True},
                        "previous": {"type": "string", "nullable": True}
                    }
                },
                "data": schema,
            }
        }