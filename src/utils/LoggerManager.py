import sys
from loguru import logger

# Remove default logger
logger.remove()

# Custom log level styling
logger.level("DEBUG", color="<blue>", icon="*️⃣DDDEBUG")
logger.level("INFO", color="<white>", icon="ℹ️IIIINFO")
logger.level("SUCCESS", color="<green>", icon="✅SUCCESS")
logger.level("WARNING", color="<yellow>", icon="⚠️WARNING")
logger.level("ERROR", color="<red>", icon="⭕EEERROR")

# Default log format
default_format: str = (
    "<g>{time:MM-DD HH:mm:ss}</g> "
    "[<lvl>{level.icon}</lvl>] "
    "{message}"
)

# Add console output
logger_id = logger.add(
    sys.stdout,
    level="INFO",
    format=default_format,
    diagnose=False
)

# Export the configured logger
__all__ = ["logger"]
