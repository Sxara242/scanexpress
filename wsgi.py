import atexit

from app import app, _dispatch_hardware_event, _validate_startup_configuration
from hardware_events import start_hardware_event_service
from storage import initialize_storage


_validate_startup_configuration(production=True)
initialize_storage()

_HARDWARE_EVENT_SERVICE = start_hardware_event_service(
    _dispatch_hardware_event,
    logger=app.logger,
)

if _HARDWARE_EVENT_SERVICE is not None:
    atexit.register(_HARDWARE_EVENT_SERVICE.stop)
