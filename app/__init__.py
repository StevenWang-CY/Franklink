"""Frank - AI Career Counselor via iMessage."""

__version__ = "1.0.0"
__author__ = "Frank Team"
__description__ = "AI-powered career counselor for college students via iMessage"

# Import Celery app for task discovery (optional - only if Celery is installed)
try:
    from app.celery_app import app as celery_app
    __all__ = ['celery_app']
except ImportError:
    # Celery not installed - this is OK for scripts that don't need it
    celery_app = None
    __all__ = []