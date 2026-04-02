import logging
import time
from typing import Any, Dict, Optional

from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class HeroSMSServiceError(Exception):
    pass


class HeroSMSAPIError(HeroSMSServiceError):
    pass


class HeroSMSTimeoutError(HeroSMSServiceError):
    pass


class HeroSMSClient:
    DEFAULT_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

    STATUS_SMS_SENT = 1
    STATUS_COMPLETE = 6
    STATUS_CANCEL = 8

    _WAITING_STATUSES = {
        "STATUS_WAIT_CODE",
        "STATUS_WAIT_RETRY",
        "STATUS_WAIT_RESEND",
        "STATUS_WAIT_RETRY_GET",
        "STATUS_PREPARE",
        "STATUS_WAIT_CODE_TEXT",
    }

    _TERMINAL_FAILURE_STATUSES = {
        "STATUS_CANCEL",
        "STATUS_CANCELLED",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        normalized = {
            "base_url": self.DEFAULT_BASE_URL,
            "timeout": 30,
            "max_retries": 3,
            "poll_timeout": 180,
            "poll_interval": 5,
            **(config or {}),
        }

        api_key = str(normalized.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("缺少必需配置: api_key")

        normalized["api_key"] = api_key
        normalized["base_url"] = str(
            normalized.get("base_url") or self.DEFAULT_BASE_URL
        ).strip()
        if not normalized["base_url"]:
            raise ValueError("base_url 不能为空")

        self.config = normalized
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=RequestConfig(
                timeout=int(self.config.get("timeout", 30)),
                max_retries=int(self.config.get("max_retries", 3)),
            ),
        )

    def _build_params(self, action: str, **params: Any) -> Dict[str, Any]:
        request_params: Dict[str, Any] = {
            "action": action,
            "api_key": self.config["api_key"],
        }
        for key, value in params.items():
            if value is None:
                continue
            request_params[key] = value
        return request_params

    def _looks_like_json(self, text: str) -> bool:
        stripped = str(text or "").strip()
        return stripped.startswith("{") or stripped.startswith("[")

    def _ensure_api_success(self, action: str, payload: Any, raw_text: str) -> Any:
        if not isinstance(payload, dict):
            return payload

        status = str(payload.get("status") or "").strip()
        normalized = status.lower()
        success_statuses = {
            "success",
            "ok",
            "access_balance",
            "access_number",
            "status_ok",
        }
        if normalized in success_statuses or status.startswith(("ACCESS_", "STATUS_")):
            return payload
        if status and not status.startswith(("ACCESS_", "STATUS_")):
            raise HeroSMSAPIError(f"Hero SMS API 错误 ({action}): {raw_text or status}")
        return payload

    def _parse_text_response(self, action: str, text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            raise HeroSMSServiceError(f"Hero SMS 返回空响应: {action}")

        if raw.startswith("ACCESS_NUMBER:"):
            parts = raw.split(":", 2)
            if len(parts) != 3:
                raise HeroSMSServiceError(f"无法解析号码响应: {raw}")
            return {
                "action": action,
                "status": "ACCESS_NUMBER",
                "activation_id": parts[1],
                "phone_number": parts[2],
                "raw": raw,
            }

        if raw.startswith("ACCESS_BALANCE:"):
            balance_text = raw.split(":", 1)[1].strip()
            try:
                balance = float(balance_text)
            except ValueError as exc:
                raise HeroSMSServiceError(f"无法解析余额响应: {raw}") from exc
            return {
                "action": action,
                "status": "ACCESS_BALANCE",
                "balance": balance,
                "raw": raw,
            }

        if raw.startswith("STATUS_OK:"):
            return {
                "action": action,
                "status": "STATUS_OK",
                "code": raw.split(":", 1)[1].strip(),
                "raw": raw,
            }

        if ":" in raw:
            status, payload = raw.split(":", 1)
            return {
                "action": action,
                "status": status.strip(),
                "message": payload.strip(),
                "raw": raw,
            }

        return {
            "action": action,
            "status": raw,
            "raw": raw,
        }

    def _request(
        self,
        action: str,
        *,
        expect_json: bool = False,
        **params: Any,
    ) -> Any:
        url = self.config["base_url"]
        request_params = self._build_params(action, **params)

        try:
            response = self.http_client.get(
                url,
                params=request_params,
                headers={
                    "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
                },
            )
        except Exception as exc:
            raise HeroSMSServiceError(f"请求 Hero SMS 失败 ({action}): {exc}") from exc

        if response.status_code >= 400:
            raise HeroSMSServiceError(
                f"Hero SMS HTTP 错误 ({action}): {response.status_code} - {response.text[:300]}"
            )

        raw_text = response.text.strip()
        content_type = str(response.headers.get("Content-Type") or "").lower()

        if (
            expect_json
            or "application/json" in content_type
            or self._looks_like_json(raw_text)
        ):
            try:
                payload = response.json()
            except Exception:
                if expect_json:
                    raise HeroSMSServiceError(
                        f"Hero SMS 返回的不是有效 JSON ({action}): {raw_text[:300]}"
                    )
                payload = self._parse_text_response(action, raw_text)
        else:
            payload = self._parse_text_response(action, raw_text)

        return self._ensure_api_success(action, payload, raw_text)

    def _resolve_service(self, service: Optional[str]) -> str:
        resolved = str(service or self.config.get("service") or "").strip()
        if not resolved:
            raise ValueError("缺少 service 参数，且配置中未提供默认 service")
        return resolved

    def _resolve_country(self, country: Optional[Any]) -> Optional[Any]:
        resolved = country if country is not None else self.config.get("country")
        if resolved is None:
            return None

        if isinstance(resolved, int):
            return resolved

        text = str(resolved).strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)

        raise ValueError(
            "Hero SMS country 必须是国家数字 ID，例如 187，不能填 US 这类国家代码"
        )

    def _normalize_status_response(self, response: Any) -> Dict[str, Any]:
        if isinstance(response, dict):
            status = str(
                response.get("status")
                or response.get("Status")
                or response.get("activationStatus")
                or ""
            ).strip()

            code = response.get("code") or response.get("sms") or response.get("otp")
            if isinstance(code, list) and code:
                first_item = code[0]
                if isinstance(first_item, dict):
                    code = (
                        first_item.get("code")
                        or first_item.get("sms")
                        or first_item.get("text")
                    )
                else:
                    code = first_item

            if isinstance(code, dict):
                code = code.get("code") or code.get("sms") or code.get("text")

            if not status and code:
                status = "STATUS_OK"

            normalized = dict(response)
            normalized["status"] = status
            normalized["code"] = str(code).strip() if code is not None else None
            normalized.setdefault("raw", response)
            return normalized

        return self._parse_text_response("getStatus", str(response or ""))

    def _is_waiting_status(self, status: str) -> bool:
        text = str(status or "").strip()
        return text in self._WAITING_STATUSES or text.startswith("STATUS_WAIT")

    def get_balance(self) -> Dict[str, Any]:
        response = self._request("getBalance")
        if not isinstance(response, dict):
            raise HeroSMSServiceError(f"余额响应格式错误: {response}")
        return response

    def get_countries(self) -> Any:
        return self._request("getCountries", expect_json=True)

    def get_services_list(self, country: Optional[Any] = None) -> Any:
        return self._request(
            "getServicesList",
            expect_json=True,
            country=self._resolve_country(country),
        )

    def get_number(
        self,
        service: Optional[str] = None,
        country: Optional[Any] = None,
        *,
        operator: Optional[str] = None,
        max_price: Optional[Any] = None,
        forward: Optional[Any] = None,
        ref: Optional[Any] = None,
        verification: Optional[Any] = None,
        soft_id: Optional[Any] = None,
        **extra_params: Any,
    ) -> Dict[str, Any]:
        response = self._request(
            "getNumber",
            service=self._resolve_service(service),
            country=self._resolve_country(country),
            operator=operator,
            maxPrice=max_price,
            forward=forward,
            ref=ref,
            verification=verification,
            softId=soft_id,
            **extra_params,
        )
        if not isinstance(response, dict):
            raise HeroSMSServiceError(f"号码响应格式错误: {response}")
        if response.get("status") != "ACCESS_NUMBER":
            raise HeroSMSServiceError(f"获取号码失败: {response}")
        return response

    def get_status(self, activation_id: Any) -> Dict[str, Any]:
        response = self._request("getStatus", id=activation_id)
        return self._normalize_status_response(response)

    def get_status_v2(self, activation_id: Any) -> Dict[str, Any]:
        response = self._request("getStatusV2", expect_json=True, id=activation_id)
        return self._normalize_status_response(response)

    def set_status(
        self,
        activation_id: Any,
        status: int,
        *,
        forward: Optional[Any] = None,
    ) -> Dict[str, Any]:
        response = self._request(
            "setStatus",
            id=activation_id,
            status=int(status),
            forward=forward,
        )
        if not isinstance(response, dict):
            raise HeroSMSServiceError(f"设置状态响应格式错误: {response}")
        return response

    def complete_activation(self, activation_id: Any) -> Dict[str, Any]:
        return self.set_status(activation_id, self.STATUS_COMPLETE)

    def cancel_activation(self, activation_id: Any) -> Dict[str, Any]:
        return self.set_status(activation_id, self.STATUS_CANCEL)

    def wait_for_code(
        self,
        activation_id: Any,
        *,
        timeout: Optional[int] = None,
        poll_interval: Optional[float] = None,
        use_status_v2: bool = False,
    ) -> Dict[str, Any]:
        resolved_timeout = int(timeout or self.config.get("poll_timeout", 180))
        resolved_interval = float(poll_interval or self.config.get("poll_interval", 5))
        if resolved_timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if resolved_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")

        deadline = time.monotonic() + resolved_timeout
        last_response: Optional[Dict[str, Any]] = None

        while time.monotonic() < deadline:
            current = (
                self.get_status_v2(activation_id)
                if use_status_v2
                else self.get_status(activation_id)
            )
            last_response = self._normalize_status_response(current)
            status = str(last_response.get("status") or "").strip()

            if status == "STATUS_OK":
                code = str(last_response.get("code") or "").strip()
                if not code:
                    raise HeroSMSServiceError(
                        f"Hero SMS 返回 STATUS_OK 但未提供验证码: {last_response}"
                    )
                return last_response

            if status in self._TERMINAL_FAILURE_STATUSES:
                raise HeroSMSAPIError(f"号码激活已终止: {last_response}")

            if not self._is_waiting_status(status):
                raise HeroSMSAPIError(f"收到非预期状态: {last_response}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(resolved_interval, remaining))

        raise HeroSMSTimeoutError(
            f"等待短信验证码超时，activation_id={activation_id}, last_response={last_response}"
        )

    def reserve_number_and_wait_code(
        self,
        service: Optional[str] = None,
        country: Optional[Any] = None,
        *,
        timeout: Optional[int] = None,
        poll_interval: Optional[float] = None,
        use_status_v2: bool = False,
        auto_complete: bool = True,
        cancel_on_error: bool = True,
        initial_status: Optional[int] = None,
        **number_kwargs: Any,
    ) -> Dict[str, Any]:
        reservation = self.get_number(
            service=service,
            country=country,
            **number_kwargs,
        )
        activation_id = reservation["activation_id"]

        try:
            initial_status_response = None
            if initial_status is not None:
                initial_status_response = self.set_status(
                    activation_id,
                    int(initial_status),
                )

            status_response = self.wait_for_code(
                activation_id,
                timeout=timeout,
                poll_interval=poll_interval,
                use_status_v2=use_status_v2,
            )

            completion = None
            if auto_complete:
                completion = self.complete_activation(activation_id)

            return {
                "activation_id": activation_id,
                "phone_number": reservation.get("phone_number"),
                "code": status_response.get("code"),
                "reservation": reservation,
                "initial_status_response": initial_status_response,
                "status_response": status_response,
                "completion": completion,
            }
        except Exception:
            if cancel_on_error:
                try:
                    cancel_response = self.cancel_activation(activation_id)
                    logger.warning(
                        "Hero SMS 激活失败，已尝试取消 activation_id=%s: %s",
                        activation_id,
                        cancel_response,
                    )
                except Exception as cancel_exc:
                    logger.warning(
                        "Hero SMS 激活失败且取消也失败 activation_id=%s: %s",
                        activation_id,
                        cancel_exc,
                    )
            raise
