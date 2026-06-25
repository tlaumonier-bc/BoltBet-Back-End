import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lightning_map_game_backend.settings")

# Initialise the Django app registry BEFORE importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from channels.auth import AuthMiddlewareStack
from lightning.routing import websocket_urlpatterns

# Start the in-process background workers (ingest + purge + trim + resolver).
# Runs only when the app is served (daphne imports this module), and only for
# the threads enabled via the RUN_*_IN_PROCESS flags. No-op otherwise.
from lightning.background import start_background_threads
start_background_threads()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        ),
    }
)
