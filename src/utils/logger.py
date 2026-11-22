# Standard library imports for logging and creating the logs directory.
import logging
import os

# Folder and file path where pipeline logs are written.
LOG_DIRECTORY = "logs"
LOG_FILE_PATH = os.path.join(LOG_DIRECTORY, "pipeline.log")


# Create and return a logger that writes to both the console and logs/pipeline.log.
def get_logger(name: str) -> logging.Logger:
    # Create the logs folder if it does not already exist.
    os.makedirs(LOG_DIRECTORY, exist_ok=True)

    # Fetch the logger for the given module name and set its level to INFO.
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Return immediately if handlers are already attached to prevent duplicate log lines.
    if len(logger.handlers) > 0:
        return logger

    # Set up a consistent format with timestamp, level, logger name, and message.
    log_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt=log_format, datefmt=date_format)

    # Create a handler that sends log messages to the console.
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # Create a handler that writes log messages to logs/pipeline.log.
    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # Attach both handlers to the logger.
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    # Keep log messages from propagating to the root logger and showing twice.
    logger.propagate = False

    # Return the ready-to-use logger.
    return logger