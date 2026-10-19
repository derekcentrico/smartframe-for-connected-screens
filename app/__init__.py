"""
SmartFrame application factory.
"""

import logging

from flask import Flask

from .config import (
    MAX_UPLOAD_MB,
    UPLOAD_AUTH_ENABLED,
    UPLOAD_PASSWORD,
    UPLOAD_USERNAME,
)

logger = logging.getLogger(__name__)

_WEAK_PASSWORDS = {"", "smartframe", "changeme", "admin", "password"}


def create_app():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not UPLOAD_AUTH_ENABLED:
        logger.warning("UPLOAD_AUTH_ENABLED is off; upload endpoint is unprotected")
    elif not UPLOAD_USERNAME or not UPLOAD_PASSWORD:
        logger.error(
            "UPLOAD_AUTH_ENABLED is on but UPLOAD_USERNAME or UPLOAD_PASSWORD "
            "is not set; upload auth will reject all requests until configured"
        )
    elif UPLOAD_PASSWORD in _WEAK_PASSWORDS:
        logger.warning(
            "UPLOAD_PASSWORD is set to a weak/default value; change it"
        )

    app = Flask(__name__, template_folder="templates")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

    from .routes import routes_bp
    from .upload import upload_bp

    app.register_blueprint(upload_bp)
    app.register_blueprint(routes_bp)

    return app
