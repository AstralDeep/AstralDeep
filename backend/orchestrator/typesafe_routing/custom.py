"""Calls user-configured System One providers through the approved bounded egress helper.
Responses retain the typed answer views consumed by decision.py and runner.py.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from types import SimpleNamespace
from urllib.parse import urlsplit

from shared import external_http

from .client import load_sdk

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CustomDecisionError(ValueError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def normalize_base_url(base_url: str) -> str:
    try:
        if (not isinstance(base_url, str) or not 1 <= len(base_url) <= 4096
                or any(ord(character) < 33 or ord(character) == 127 for character in base_url)):
            raise ValueError
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError
    except ValueError:
        raise CustomDecisionError("Enter a valid decisions base URL without credentials, a query, or a fragment.") from None
    return base_url.rstrip("/")


def _number(value, lower=0.0, upper=1.0):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not lower <= value <= upper):
        raise ValueError
    return float(value)


def _questions(questions, sdk):
    if not isinstance(questions, Mapping) or not questions:
        raise CustomDecisionError("At least one System One question is required.")
    result = {}
    for name, question in questions.items():
        if isinstance(question, Mapping):
            result[name] = dict(question)
            continue
        kind = next((kind for kind, cls in (("noul", sdk.noul), ("score", sdk.score),
            ("choice", sdk.choice)) if isinstance(question, cls)), None)
        if kind is None:
            raise CustomDecisionError("The System One question is invalid.")
        entry = {"type": kind}
        for attribute in ("instructions", "criteria"):
            value = getattr(question, attribute, None)
            if value is not None:
                entry[attribute] = value
        result[name] = entry
    return result


def _answers(content, questions):
    try:
        raw = json.loads(content)
        answers = raw["answers"]
        buckets = {"noul": {}, "choice": {}, "score": {}}
        for name, question in questions.items():
            answer = answers[name]
            kind = question["type"]
            if answer["type"] != kind:
                raise ValueError
            if kind == "noul":
                value = SimpleNamespace(noul=_number(answer["noul"]))
            elif kind == "choice":
                criteria = question["criteria"]
                choice = answer["choice"]
                probabilities = answer["probabilities"]
                if (choice not in criteria or not isinstance(probabilities, dict)
                        or choice not in probabilities or set(probabilities) - set(criteria)):
                    raise ValueError
                value = SimpleNamespace(choice=choice, confidence=_number(answer["confidence"]),
                    probabilities={key: _number(probability) for key, probability in probabilities.items()})
            elif kind == "score":
                value = SimpleNamespace(score=_number(answer["score"], upper=len(question["criteria"]) - 1))
            else:
                raise ValueError
            buckets[kind][name] = value
        return SimpleNamespace(nouls=buckets["noul"], choices=buckets["choice"], scores=buckets["score"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CustomDecisionError("The provider did not return valid System One answers.") from None


class CustomDecisionClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = normalize_base_url(base_url)
        if (not isinstance(model, str) or not model.strip() or len(model) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in model)):
            raise CustomDecisionError("Enter a valid decisions model name.")
        self.model = model.strip()
        self._closed = False

    def _surface(self):
        return load_sdk()

    async def system_one(self, *, api_key: str, state, questions, timeout: float):
        if self._closed:
            raise CustomDecisionError("The decisions client is closed.")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise CustomDecisionError("The decisions timeout is invalid.")
        questions = _questions(questions, self._surface())
        try:
            body = {"model": self.model, "state": state, "questions": questions}
            if len(json.dumps(body, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
                raise CustomDecisionError("The decisions request is too large.")
        except (TypeError, ValueError) as error:
            if isinstance(error, CustomDecisionError):
                raise
            raise CustomDecisionError("The decisions request is invalid.") from None
        url = self.base_url + ("/systemone" if self.base_url.endswith("/v1") else "/v1/systemone")
        try:
            response = await asyncio.wait_for(asyncio.to_thread(external_http.request,
                "POST", url, api_key=api_key, json_body=body, timeout=timeout,
                max_response_bytes=MAX_RESPONSE_BYTES, allow_redirects=False,
                trust_environment=False), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise CustomDecisionError("The decisions provider did not respond in time.", status_code=408) from None
        except external_http.AuthFailedError:
            raise CustomDecisionError("The decisions provider rejected the API key.", status_code=401) from None
        except external_http.RateLimitedError:
            raise CustomDecisionError("The decisions provider is temporarily unavailable.", status_code=429) from None
        except external_http.ServiceUnreachableError:
            raise CustomDecisionError("The decisions provider could not be reached.", status_code=503) from None
        except external_http.BadRequestError:
            raise CustomDecisionError("The decisions provider refused the request.", status_code=400) from None
        except external_http.ExternalHttpError:
            raise CustomDecisionError("The decisions request was blocked by the server's egress policy.") from None
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            raise CustomDecisionError("The provider did not return valid System One answers.")
        return _answers(response.content, questions)

    async def aclose(self):
        self._closed = True


async def probe_custom(*, base_url: str, model: str, api_key: str = "", timeout: float = 5.0) -> None:
    client = CustomDecisionClient(base_url, model)
    try:
        await client.system_one(api_key=api_key, state="Hello.",
            questions={"ready": {"type": "noul", "instructions": "Is this a greeting?"}}, timeout=timeout)
    finally:
        await client.aclose()
