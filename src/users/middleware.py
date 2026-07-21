from django.conf import settings
from django.utils import translation

SUPPORTED_LANGUAGES = {code for code, _ in settings.LANGUAGES}


class UserLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            language = request.user.language

            if language in SUPPORTED_LANGUAGES:
                translation.activate(language)
                request.LANGUAGE_CODE = language

        return self.get_response(request)