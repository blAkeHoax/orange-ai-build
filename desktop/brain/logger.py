"""
Simple file logging for Orange AI.

Right now, errors (an API connection error, a tool exception, etc.) just
print to the terminal and vanish the moment you close the window. This
gives every run a persistent log file so you can actually look back at
what happened, especially for intermittent issues like rate limits or
a flaky tool call that's hard to catch live.

USAGE elsewhere in the project:
    from brain.logger import logger
    logger.info("Something happened")
    logger.warning("API call failed: %s", e)
    logger.error("Tool crashed: %s", e)
"""

import logging
import os
from brain.paths import get_project_root

LOG_PATH = os.path.join(get_project_root(), "orange_ai.log")

logger = logging.getLogger("orange_ai")
logger.setLevel(logging.INFO)

# Avoid adding duplicate handlers if this module gets imported more than
# once (e.g. by multiple modules during the same run).
if not logger.handlers:
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)