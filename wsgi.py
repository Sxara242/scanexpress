from app import app, _validate_startup_configuration
from storage import initialize_storage


_validate_startup_configuration(production=True)
initialize_storage()
