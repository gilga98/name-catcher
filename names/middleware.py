from django.http import HttpResponse

from .ai.base import AIError


class AIErrorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, AIError):
            return HttpResponse(str(exception), status=502, content_type="text/plain")
        return None
