from pydantic import BaseModel
from typing import List
import json
import logging
import time
import os
from patchguru import Config
from patchguru.utils.Logger import setup_logging, get_logger
import atexit

# Global event list
_USAGE: list = []

log_dir: str | None = None
text_log_file: str | None = None
json_log_file: str | None = None
logger: logging.Logger | None = None


def _flat_logs() -> bool:
    return os.environ.get("PATCHGURU_LOG_FLAT", "").strip().lower() in ("1", "true", "yes")


def configure_log_dir(base_dir: str, *, flat: bool | None = None) -> str:
    """Configure where events.jsonl / events.log / llm_usage.json are written."""
    global log_dir, text_log_file, json_log_file, logger

    use_flat = _flat_logs() if flat is None else flat
    session = os.environ.get("PATCHGURU_LOG_SESSION")
    if use_flat:
        log_dir = base_dir
    elif session:
        log_dir = os.path.join(base_dir, session)
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_dir = os.path.join(base_dir, stamp)
        while os.path.exists(log_dir):
            time.sleep(1)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            log_dir = os.path.join(base_dir, stamp)

    os.makedirs(log_dir, exist_ok=True)
    text_log_file = os.path.join(log_dir, "events.log")
    json_log_file = os.path.join(log_dir, "events.jsonl")
    setup_logging("DEBUG", log_file=text_log_file)

    if logger is None:
        logger = get_logger("PatchGuru", log_file=text_log_file)
    else:
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
                handler.close()
        file_handler = logging.FileHandler(text_log_file)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    return log_dir


def init_pr_log_dir(project: str, pr_nb: int) -> str:
    """Write event logs next to results.json under the PR cache directory."""
    override = os.environ.get("PATCHGURU_LOG_DIR")
    if override:
        return configure_log_dir(override, flat=_flat_logs())
    base = os.path.join(Config.CACHE_DIR, "oracles", project, str(pr_nb))
    return configure_log_dir(base, flat=True)


def _ensure_log_dir() -> None:
    if log_dir is not None:
        return
    configure_log_dir(Config.LOG_DIR)


def store_usage():
    if log_dir is None:
        return
    with open(os.path.join(log_dir, "llm_usage.json"), "w") as f:
        json.dump(_USAGE, f, indent=2)


atexit.register(store_usage)


class Event(BaseModel):
    level: str = "INFO"
    timestamp: str = ""
    pr_nb: int = -1
    type: str = "GeneralInfo"
    message: str | List[str] = ""
    info: dict = {}


def append_event(evt):
    _ensure_log_dir()
    evt.timestamp = time.strftime("%Y%m%d-%H%M%S")
    if isinstance(evt.message, list):
        evt.message = "\n".join(evt.message)
    if evt.level == "ERROR":
        logger.error(f"{evt.type} - {evt.message}")
    elif evt.level == "WARNING":
        logger.warning(f"{evt.type} - {evt.message}")
    elif evt.level == "DEBUG":
        logger.debug(f"{evt.type} - {evt.message}")
    else:
        assert evt.level == "INFO", f"Unknown log level: {evt.level}"
        logger.info(f"{evt.type} - {evt.message}")

    if evt.type == "LLMQuery":
        _USAGE.append(evt.info)
    with open(json_log_file, "a") as f:
        f.write(json.dumps(evt.dict()) + "\n")
