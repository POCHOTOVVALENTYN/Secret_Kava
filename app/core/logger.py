# app/core/logger.py
import logging
import sys
import structlog
from app.core.config import settings

def setup_logging() -> None:
    """Configures structured JSON logging for production and readable console logs for local development."""
    
    # Define processors based on environment
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.ENVIRONMENT == "production":
        # JSON formatting for production log shipping (Loki, Elasticsearch, Datadog)
        processors.append(structlog.processors.JSONRenderer())
        renderer_logger = structlog.BytesLogger()
    else:
        # High readability colored terminal output for developers
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
        renderer_logger = structlog.WriteLogger(sys.stdout)

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.INFO if settings.ENVIRONMENT == "production" else logging.DEBUG
        ),
        cache_logger_on_first_use=True,
    )

    # Bridge standard logging to structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO if settings.ENVIRONMENT == "production" else logging.DEBUG,
    )
    
    # Suppress verbose standard libraries noise
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
