#!/usr/bin/env python3
"""Common Configuration Utilities for Standalone Scripts.

This module provides standardized configuration file discovery functionality
for all standalone scripts in the scripts directory.

Search Priority:
1. Command line parameter (if specified)
2. Current working directory (./config.yaml)
3. Project root directory (../config.yaml)
4. Parent directory (../../config.yaml)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def find_config_file(
    config_arg: str | None = None, logger: logging.Logger | None = None
) -> str | None:
    """Find configuration file following the standard search priority.

    Args:
        config_arg: Configuration file path from command line argument (takes priority)
        logger: Logger instance for messages (optional)

    Returns:
        str: Path to configuration file if found, None otherwise

    Search Priority:
    1. Command line parameter (if specified) - ERROR if file doesn't exist
    2. Current working directory (./config.yaml)
    3. Project root directory (../config.yaml)
    4. Parent directory (../../config.yaml)

    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Priority 1: Command line parameter (if specified)
    if config_arg:
        if Path(config_arg).is_file():
            logger.info("Using configuration file from command line: %s", config_arg)
            return config_arg
        logger.error("Specified configuration file not found: %s", config_arg)
        return None

    # Priority 2-4: Search standard locations
    search_paths = [
        "./config.yaml",  # Current directory
        "../config.yaml",  # Project root
        "../../config.yaml",  # Parent directory
    ]

    for config_path in search_paths:
        if Path(config_path).is_file():
            # Resolve to absolute path for clear logging
            abs_path = str(Path(config_path).resolve())
            logger.info("Found configuration file: %s", abs_path)
            return config_path

    # No configuration file found
    logger.error("No configuration file found in any of the following locations:")
    for path in search_paths:
        abs_path = str(Path(path).resolve())
        logger.error("  - %s", abs_path)

    return None


def load_config_with_fallback(
    config_arg: str | None = None,
    loader_func: Callable[[str], dict[str, Any]] | None = None,
    logger: logging.Logger | None = None,
) -> dict | None:
    """Find and load configuration file with standardized error handling.

    Args:
        config_arg: Configuration file path from command line argument
        loader_func: Function to load config (e.g., load_static_config)
        logger: Logger instance for messages

    Returns:
        dict: Loaded configuration dictionary, or None if failed

    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Find config file
    config_path = find_config_file(config_arg, logger)
    if not config_path:
        return None

    # Load config if loader function provided
    if loader_func:
        try:
            config = loader_func(config_path)
            if config:
                logger.info("Successfully loaded configuration from: %s", config_path)
                return config
            logger.error("Configuration file exists but failed to load: %s", config_path)
            return None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            logger.exception("Error loading configuration from %s", config_path)
            return None

    # Just return the path if no loader function
    return config_path
