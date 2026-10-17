from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from contracts.models import ModelCatalogDiscoveryView
from services.account_processing import account_processing_slot
from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.diagnostics import sanitize_diagnostic_text
from utils.helper import is_supported_image_model


class ModelCatalogDiscoveryError(RuntimeError):
    """A safe, user-displayable error while discovering upstream models."""


class ModelCatalogDiscoveryUnavailableError(ModelCatalogDiscoveryError):
    """No eligible ChatGPT Web account is available for discovery."""


class ModelCatalogDiscoveryService:
    """Read the shared ChatGPT Web model catalogue from one valid account."""

    @staticmethod
    def _source_account() -> dict[str, Any] | None:
        candidates = [
            account
            for account in account_service.list_accounts()
            if isinstance(account, dict)
            and account_service._normalize_source_type(account.get("source_type")) == "web"
            and account_service._is_account_selectable(account, allow_limited=True)
            and str(account.get("access_token") or "").strip()
        ]
        candidates.sort(key=lambda account: account.get("status") != "正常")
        return candidates[0] if candidates else None

    def discover(self) -> ModelCatalogDiscoveryView:
        account = self._source_account()
        if account is None:
            raise ModelCatalogDiscoveryUnavailableError(
                "没有可读取模型目录的有效 ChatGPT Web 账号，请先添加或恢复一个账号。"
            )

        original_token = str(account.get("access_token") or "").strip()
        active_token = ""
        backend: OpenAIBackendAPI | None = None
        try:
            active_token = account_service.ensure_access_token(
                original_token,
                event="model_catalog_discovery",
                raise_on_error=True,
            )
            with account_processing_slot():
                try:
                    backend = OpenAIBackendAPI(access_token=active_token)
                    response = backend.list_models()
                finally:
                    if backend is not None:
                        backend.close()

            raw_models = response.get("data", []) if isinstance(response, dict) else []
            models = sorted({
                model
                for item in raw_models
                if isinstance(item, dict)
                and (model := str(item.get("id") or "").strip())
                and len(model) <= 200
                and not any(ord(char) < 32 or ord(char) == 127 for char in model)
                and not is_supported_image_model(model)
            }) if isinstance(raw_models, list) else []
            if not models:
                raise ModelCatalogDiscoveryError("上游没有返回可用的对话模型。")

            return ModelCatalogDiscoveryView(
                models=tuple(models),
                fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        except ModelCatalogDiscoveryError:
            raise
        except Exception as exc:
            safe_message = sanitize_diagnostic_text(
                exc,
                sensitive_values=(
                    original_token,
                    active_token,
                    account.get("email"),
                    account.get("name"),
                ),
                limit=500,
            )
            raise ModelCatalogDiscoveryError(
                safe_message or "读取上游模型目录失败，请检查账号状态后重试。"
            ) from None


model_catalog_discovery_service = ModelCatalogDiscoveryService()
