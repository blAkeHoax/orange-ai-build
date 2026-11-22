"""
brain/api_client.py - Desktop client for Orange API.

Responsible exclusively for communicating with Orange API over HTTP.
Provides a clean interface for conversational inference, handles connection
failures, timeouts, and HTTP errors gracefully, and prevents secret exposure.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional, Union
from dotenv import load_dotenv

from brain.paths import get_project_root
from brain.logger import logger

# Load Desktop environment configuration
_env_path = os.path.join(get_project_root(), ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)


class OrangeAPIError(Exception):
    """Base exception for all Orange API client errors."""
    pass


class OrangeAPIConnectionError(OrangeAPIError):
    """Raised when the Orange API server is unreachable, connection is refused, or times out."""
    pass


class OrangeAPIResponseError(OrangeAPIError):
    """Raised when Orange API returns an HTTP error or an invalid response payload."""
    pass


DEFAULT_ORANGE_API_URL = "https://orange-api-cuuo.onrender.com"
DEFAULT_ASSIST_TIMEOUT: float = float(os.environ.get("ORANGE_API_TIMEOUT", "105.0"))
class OrangeAPIClient:
    """
    HTTP client for the Orange API backend.
    """

    def __init__(self, api_url: Optional[str] = None, api_key: Optional[str] = None):
        if not api_url:
            env_url = os.environ.get("ORANGE_API_URL")
            if env_url:
                api_url = env_url
            else:
                try:
                    from gui.settings import get_api_url
                    configured = get_api_url()
                    if configured:
                        api_url = configured
                except Exception:
                    pass
        raw_url = api_url or DEFAULT_ORANGE_API_URL
        self.api_url = raw_url.rstrip("/")

        if api_key is not None:
            self.api_key = str(api_key).strip()
        else:
            self.api_key = os.environ.get("ORANGE_API_KEY", "").strip()

    def is_available(self, timeout: float = 1.0) -> bool:
        """
        Quick, non-blocking check to determine if Orange API is online.
        Returns True if /health returns status online within timeout.
        """
        url = f"{self.api_url}/health"
        req = urllib.request.Request(url, headers={"User-Agent": "OrangeAIDesktop/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    return data.get("status") == "online"
        except Exception:
            return False
        return False

    def assist(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
        system_instruction: Optional[str] = None,
        profile: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        task_type: Optional[str] = None,
        modalities: Optional[List[str]] = None,
        timeout: float = DEFAULT_ASSIST_TIMEOUT,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Send a conversational request to POST /assist on Orange API.
        Accepts text-only conversational assistance queries.

        Returns a dictionary:
            {"reply": str, "model": str, "success": bool}

        Raises:
            TypeError: If image attachments are submitted.
            OrangeAPIConnectionError: If API cannot be reached or request times out.
            OrangeAPIResponseError: If API returns an HTTP error status or malformed data.
        """
        if "image" in kwargs and kwargs["image"] is not None:
            raise TypeError("Image attachments are not supported; Orange AI is a text-only assistant.")

        # Backward-compatibility: if positional assist(message, history) was called
        # where second argument might be a history list
        if isinstance(history, str):
            history = [{"role": "user", "content": history}]

        url = f"{self.api_url}/assist"

        payload: Dict[str, Any] = {
            "message": message,
            "history": history or [],
            "profile": profile or "",
        }

        if system_instruction:
            payload["system_instruction"] = system_instruction
        if provider:
            payload["provider"] = provider
        if model and model.strip() and model.strip().lower() != "auto":
            payload["model"] = model.strip()
        if task_type:
            payload["task_type"] = task_type
        if modalities is not None:
            payload["modalities"] = modalities

        try:
            body_bytes = json.dumps(payload).encode("utf-8")
        except Exception as e:
            logger.error("Failed to serialize assist request: %s", e)
            raise OrangeAPIResponseError("Failed to serialize request payload.") from e

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "OrangeAIDesktop/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.status
                raw_data = response.read().decode("utf-8")

                if status_code != 200:
                    logger.warning("Orange API returned HTTP status %d", status_code)
                    raise OrangeAPIResponseError(
                        f"Orange API returned status {status_code}."
                    )

                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError as e:
                    logger.error("Failed to parse Orange API response JSON: %s", e)
                    raise OrangeAPIResponseError("Orange API returned malformed response.") from e

                if not isinstance(data, dict) or "reply" not in data:
                    logger.error("Unexpected response structure from Orange API: %s", data)
                    raise OrangeAPIResponseError("Invalid response format from Orange API.")

                return {
                    "reply": data.get("reply", ""),
                    "model": data.get("model", "unknown"),
                    "success": data.get("success", True),
                }

        except urllib.error.HTTPError as e:
            error_detail = ""
            try:
                error_body = e.read().decode("utf-8")
                error_json = json.loads(error_body)
                error_detail = error_json.get("detail", "")
            except Exception:
                pass

            # Sanitize and truncate error detail to avoid leaking large values or base64 in logs
            safe_detail = str(error_detail)
            if len(safe_detail) > 500:
                safe_detail = safe_detail[:500] + "... [truncated]"

            logger.warning("Orange API HTTP error %d: %s", e.code, safe_detail)
            if e.code == 401:
                auth_msg = (
                    f"Orange API authentication failed (HTTP 401): {safe_detail}. "
                    "Please check your ORANGE_API_KEY configuration."
                    if safe_detail
                    else "Orange API authentication failed (HTTP 401). Please check your ORANGE_API_KEY configuration."
                )
                raise OrangeAPIResponseError(auth_msg) from e
            if safe_detail:
                raise OrangeAPIResponseError(f"Orange API error ({e.code}): {safe_detail}") from e
            raise OrangeAPIResponseError(f"Orange API error: HTTP {e.code}.") from e

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("Failed to connect to Orange API at %s: %s", self.api_url, e)
            raise OrangeAPIConnectionError(
                f"Orange API is currently unreachable at {self.api_url}. "
                "Make sure Orange API is running."
            ) from e

        except OrangeAPIError:
            raise

        except Exception as e:
            logger.error("Unexpected error contacting Orange API: %s", e)
            raise OrangeAPIError("An unexpected error occurred while communicating with Orange API.") from e
