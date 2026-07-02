import json
import os
from datetime import datetime, timedelta
import atexit
from typing import List, Optional
from pydantic import BaseModel

from testora.util.ClassificationResult import Classification


class Event(BaseModel):
    timestamp: str = ""
    pr_nb: int
    message: str


class PREvent(Event):
    title: str
    url: str


class TestExecutionEvent(Event):
    code: str
    output: str


class ComparisonEvent(Event):
    test_code: str
    old_output: str
    new_output: str


class PreClassificationEvent(Event):
    test_code: str
    old_output: str
    new_output: str


class ClassificationEvent(Event):
    test_code: str
    old_output: str
    new_output: str
    classification: Classification
    classification_explanation: str
    old_is_crash: bool
    new_is_crash: bool


class SelectBehaviorEvent(Event):
    expected_output: int


class LLMEvent(Event):
    content: str


class ErrorEvent(Event):
    details: str


class CoverageEvent(Event):
    details: str


class ClassifierEvalEvent(Event):
    label: str
    predictions: str


events: List[Event] = []
last_time_stored = datetime.now()
last_file_stored_to: Optional[str] = None


def append_event(evt):
    global last_time_stored

    evt.timestamp = datetime.now().isoformat()
    events.append(evt)
    print(json.dumps(evt.dict(), indent=2))

    if datetime.now() - last_time_stored > timedelta(minutes=5):
        store_logs()
        last_time_stored = datetime.now()


def get_logs_as_json():
    return json.dumps([evt.dict() for evt in events], indent=2)


def store_logs():
    global last_file_stored_to
    event_dicts = [evt.model_dump() for evt in events]
    fixed = os.environ.get("TESTORA_LOG_FILE")
    if fixed:
        out_file = fixed
    else:
        timestamp = datetime.now().isoformat()
        out_file = f"logs_{timestamp}.json"

    tmp_file = f"{out_file}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(event_dicts, f, indent=2)
    os.replace(tmp_file, out_file)

    # remove previous rolling log from this run (not when path is fixed per PR)
    if not fixed and last_file_stored_to is not None and last_file_stored_to != out_file:
        try:
            os.remove(last_file_stored_to)
        except FileNotFoundError:
            pass
    last_file_stored_to = out_file


def reset_logs():
    global events
    events = []
    last_file_stored_to = None


def start_logging():
    atexit.register(store_logs)
