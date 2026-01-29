from django.http import JsonResponse
from django.conf import settings
from django.db import connection

def debug_session(request):
    try:
        db_host = settings.DATABASES['default']['HOST']
        db_name = settings.DATABASES['default']['NAME']
        db_user = settings.DATABASES['default']['USER']
        
        session_key = request.session.session_key
        session_cookie = request.COOKIES.get(settings.SESSION_COOKIE_NAME)
        
        user_is_auth = request.user.is_authenticated
        user_details = f"{request.user.username} (ID: {request.user.id})" if user_is_auth else "Anonymous"
        
        return JsonResponse({
            "status": "DEBUG_INFO",
            "session_cookie_domain": getattr(settings, 'SESSION_COOKIE_DOMAIN', 'Not Set'),
            "session_cookie_name": settings.SESSION_COOKIE_NAME,
            "secret_key_start": settings.SECRET_KEY[:5] + "...",
            "database": {
                "host": db_host,
                "name": db_name,
                "user": db_user
            },
            "request_cookies": request.COOKIES,
            "session_key_from_request": session_key,
            "user_authenticated": user_is_auth,
            "user_details": user_details,
        })
    except Exception as e:
        return JsonResponse({"error": str(e)})
