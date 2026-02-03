"""
Grok Chat 服务
"""

import re
import orjson
from typing import Dict, List, Any
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import (
    AppException,
    UpstreamException,
    ValidationException,
    ErrorType,
)
from app.services.grok.models.model import ModelService
from app.services.grok.services.assets import UploadService
from app.services.grok.processors import StreamProcessor, CollectProcessor
from app.services.grok.utils.retry import retry_on_status
from app.services.grok.utils.headers import apply_statsig, build_sso_cookie
from app.services.grok.utils.stream import wrap_stream_with_usage
from app.services.token import get_token_manager, EffortType


CHAT_API = "https://grok.com/rest/app-chat/conversations/new"


@dataclass
class ChatRequest:
    """聊天请求数据"""

    model: str
    messages: List[Dict[str, Any]]
    stream: bool = None
    think: bool = None


class MessageExtractor:
    """消息内容提取器"""

    # 需要上传的类型
    UPLOAD_TYPES = {"image_url", "input_audio", "file"}
    # 视频模式不支持的类型
    VIDEO_UNSUPPORTED = {"input_audio", "file"}

    # OpenWebUI 常注入的附件占位块（会干扰模型/导致回显）
    _ATTACHED_FILES_BLOCK_RE = re.compile(r"(?s)<attached_files>.*?</attached_files>\s*")
    _ATTACHED_FILE_TAG_RE = re.compile(r"<file\s+[^>]*?/>")

    # 防复述提示语（旧版行为）
    _NO_REPEAT_HINT = "[注意：请根据以上对话历史回答当前问题，不要重复历史回复中的内容。]"

    @staticmethod
    def extract(
        messages: List[Dict[str, Any]], is_video: bool = False
    ) -> tuple[str, List[tuple[str, str]]]:
        """
        从 OpenAI 消息格式提取内容

        Args:
            messages: OpenAI 格式消息列表
            is_video: 是否为视频模型

        Returns:
            (text, attachments): 拼接后的文本和需要上传的附件列表

        Raises:
            ValueError: 视频模型遇到不支持的内容类型
        """
        texts: List[str] = []
        attachments = []  # 需要上传的附件 (URL 或 base64)

        # 先抽取每条消息的文本，保留角色信息用于合并
        extracted: List[Dict[str, str]] = []

        # 用于将图片附件绑定到对话轮次：在文本中插入占位符 [image att:N]
        image_att_index = 0

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            parts = []

            if isinstance(content, str):
                text = content
                text = MessageExtractor._ATTACHED_FILES_BLOCK_RE.sub("", text)
                text = MessageExtractor._ATTACHED_FILE_TAG_RE.sub("", text)
                if text.strip():
                    parts.append(text)

            elif isinstance(content, list):
                for item in content:
                    item_type = item.get("type", "")

                    if item_type == "text":
                        text = item.get("text", "")
                        text = MessageExtractor._ATTACHED_FILES_BLOCK_RE.sub("", text)
                        text = MessageExtractor._ATTACHED_FILE_TAG_RE.sub("", text)
                        if text.strip():
                            parts.append(text)

                    elif item_type == "image_url":
                        image_data = item.get("image_url", {})
                        url = (
                            image_data.get("url", "")
                            if isinstance(image_data, dict)
                            else str(image_data)
                        )
                        if url:
                            attachments.append(("image", url))
                            image_att_index += 1
                            # 旧版：在文本中按出现顺序插入占位符，保证图片-轮次-问题绑定
                            parts.append(f"[image att:{image_att_index}]")

                    elif item_type == "input_audio":
                        if is_video:
                            raise ValueError("视频模型不支持 input_audio 类型")
                        audio_data = item.get("input_audio", {})
                        data = (
                            audio_data.get("data", "")
                            if isinstance(audio_data, dict)
                            else str(audio_data)
                        )
                        if data:
                            attachments.append(("audio", data))

                    elif item_type == "file":
                        if is_video:
                            raise ValueError("视频模型不支持 file 类型")
                        file_data = item.get("file", {})
                        url = file_data.get("url", "") or file_data.get("data", "")
                        if isinstance(file_data, str):
                            url = file_data
                        if url:
                            attachments.append(("file", url))

            if parts:
                extracted.append({"role": role, "text": "\n".join(parts)})

        # 找到最后一条 user 消息
        last_user_index = next(
            (
                i
                for i in range(len(extracted) - 1, -1, -1)
                if extracted[i]["role"] == "user"
            ),
            None,
        )

        is_multi_turn = len(extracted) > 1

        for i, item in enumerate(extracted):
            role = item["role"] or "user"
            text = item["text"]

            if not is_multi_turn:
                texts.append(text)
                continue

            is_last_user = i == last_user_index
            if role == "system":
                texts.append(f"[系统指令]: {text}")
            elif role == "user":
                if is_last_user:
                    texts.append(f"[当前问题]: {text}")
                else:
                    texts.append(f"[历史用户消息]: {text}")
            elif role == "assistant":
                texts.append(f"[历史AI回复]: {text}")
            else:
                texts.append(f"[{role}]: {text}")

        if is_multi_turn:
            texts.append(MessageExtractor._NO_REPEAT_HINT)

        return "\n\n".join(texts), attachments


class ChatRequestBuilder:
    """请求构造器"""

    @staticmethod
    def build_headers(token: str) -> Dict[str, str]:
        """构造请求头"""
        user_agent = get_config("security.user_agent")
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Baggage": "sentry-environment=production,sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Origin": "https://grok.com",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Referer": "https://grok.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="136", "Chromium";v="136", "Not(A:Brand";v="24"',
            "Sec-Ch-Ua-Arch": "arm",
            "Sec-Ch-Ua-Bitness": "64",
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Model": "",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": user_agent,
        }

        apply_statsig(headers)
        headers["Cookie"] = build_sso_cookie(token)

        return headers

    @staticmethod
    def build_payload(
        message: str,
        model: str,
        mode: str = None,
        file_attachments: List[str] = None,
        image_attachments: List[str] = None,
    ) -> Dict[str, Any]:
        """构造请求体"""
        merged_attachments = []
        if file_attachments:
            merged_attachments.extend(file_attachments)
        if image_attachments:
            merged_attachments.extend(image_attachments)

        payload = {
            "temporary": get_config("chat.temporary"),
            "modelName": model,
            "message": message,
            "fileAttachments": merged_attachments,
            "imageAttachments": [],
            "disableSearch": False,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "forceConcise": False,
            "toolOverrides": {},
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "responseMetadata": {
                "modelConfigOverride": {"modelMap": {}},
                "requestModelDetails": {"modelId": model},
            },
            "disableMemory": get_config("chat.disable_memory"),
            "deviceEnvInfo": {
                "darkModeEnabled": False,
                "devicePixelRatio": 2,
                "screenWidth": 2056,
                "screenHeight": 1329,
                "viewportWidth": 2056,
                "viewportHeight": 1083,
            },
        }

        if mode:
            payload["modelMode"] = mode

        return payload


class GrokChatService:
    """Grok API 调用服务"""

    def __init__(self, proxy: str = None):
        self.proxy = proxy or get_config("network.base_proxy_url")

    async def chat(
        self,
        token: str,
        message: str,
        model: str = "grok-3",
        mode: str = None,
        stream: bool = None,
        file_attachments: List[str] = None,
        image_attachments: List[str] = None,
        raw_payload: Dict[str, Any] = None,
    ):
        """发送聊天请求"""
        if stream is None:
            stream = get_config("chat.stream")

        headers = ChatRequestBuilder.build_headers(token)
        payload = (
            raw_payload
            if raw_payload is not None
            else ChatRequestBuilder.build_payload(
                message, model, mode, file_attachments, image_attachments
            )
        )
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        timeout = get_config("network.timeout")

        logger.debug(
            f"Chat request: model={model}, mode={mode}, stream={stream}, attachments={len(file_attachments or [])}"
        )

        # 建立连接
        async def establish_connection():
            browser = get_config("security.browser")
            session = AsyncSession(impersonate=browser)
            try:
                response = await session.post(
                    CHAT_API,
                    headers=headers,
                    data=orjson.dumps(payload),
                    timeout=timeout,
                    stream=True,
                    proxies=proxies,
                )

                if response.status_code != 200:
                    content = ""
                    try:
                        content = await response.text()
                    except Exception:
                        pass

                    logger.error(
                        f"Chat failed: status={response.status_code}, token={token[:10]}..."
                    )

                    await session.close()
                    raise UpstreamException(
                        message=f"Grok API request failed: {response.status_code}",
                        details={"status": response.status_code, "body": content},
                    )

                logger.info(f"Chat connected: model={model}, stream={stream}")
                return session, response

            except UpstreamException:
                raise
            except Exception as e:
                logger.error(f"Chat request error: {e}")
                await session.close()
                raise UpstreamException(
                    message=f"Chat connection failed: {str(e)}",
                    details={"error": str(e)},
                )

        # 重试机制
        def extract_status(e: Exception) -> int | None:
            if isinstance(e, UpstreamException) and e.details:
                return e.details.get("status")
            return None

        session = None
        response = None
        try:
            session, response = await retry_on_status(
                establish_connection, extract_status=extract_status
            )
        except Exception as e:
            status_code = extract_status(e)
            if status_code:
                token_mgr = await get_token_manager()
                reason = str(e)
                if isinstance(e, UpstreamException) and e.details:
                    body = e.details.get("body")
                    if body:
                        reason = f"{reason} | body: {body}"
                await token_mgr.record_fail(token, status_code, reason)
            raise

        # 流式传输
        async def stream_response():
            try:
                async for line in response.aiter_lines():
                    yield line
            finally:
                if session:
                    await session.close()

        return stream_response()

    async def chat_openai(self, token: str, request: ChatRequest):
        """OpenAI 兼容接口"""
        model_info = ModelService.get(request.model)
        if not model_info:
            raise ValidationException(f"Unknown model: {request.model}")

        grok_model = model_info.grok_model
        mode = model_info.model_mode
        is_video = model_info.is_video

        # 提取消息和附件
        try:
            message, attachments = MessageExtractor.extract(
                request.messages, is_video=is_video
            )
            logger.debug(
                f"Extracted message length={len(message)}, attachments={len(attachments)}"
            )
        except ValueError as e:
            raise ValidationException(str(e))

        # 上传附件
        file_ids = []
        if attachments:
            upload_service = UploadService()
            try:
                for attach_type, attach_data in attachments:
                    file_id, _ = await upload_service.upload(attach_data, token)
                    file_ids.append(file_id)
                    logger.debug(
                        f"Attachment uploaded: type={attach_type}, file_id={file_id}"
                    )
            finally:
                await upload_service.close()

        stream = (
            request.stream if request.stream is not None else get_config("chat.stream")
        )

        response = await self.chat(
            token,
            message,
            grok_model,
            mode,
            stream,
            file_attachments=file_ids,
            image_attachments=[],
        )

        return response, stream, request.model


class ChatService:
    """Chat 业务服务"""

    @staticmethod
    async def completions(
        model: str,
        messages: List[Dict[str, Any]],
        stream: bool = None,
        thinking: str = None,
    ):
        """Chat Completions 入口"""
        # 获取 token
        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()

        token = None
        for pool_name in ModelService.pool_candidates_for_model(model):
            token = token_mgr.get_token(pool_name)
            if token:
                break

        if not token:
            raise AppException(
                message="No available tokens. Please try again later.",
                error_type=ErrorType.RATE_LIMIT.value,
                code="rate_limit_exceeded",
                status_code=429,
            )

        # 解析参数：默认隐藏思考过程
        # - 非 thinking 模型：默认走 chat.thinking 配置
        # - *-thinking 模型：默认隐藏（除非 thinking=enabled）
        if thinking == "enabled":
            think = True
        elif thinking == "disabled":
            think = False
        else:
            think = False if str(model).endswith("-thinking") else get_config("chat.thinking", False)

        is_stream = stream if stream is not None else get_config("chat.stream", True)

        # 构造请求
        chat_request = ChatRequest(
            model=model, messages=messages, stream=is_stream, think=think
        )

        # 请求 Grok
        service = GrokChatService()
        response, _, model_name = await service.chat_openai(token, chat_request)

        # 处理响应
        if is_stream:
            logger.debug(f"Processing stream response: model={model}")
            processor = StreamProcessor(model_name, token, think)
            return wrap_stream_with_usage(
                processor.process(response), token_mgr, token, model
            )

        # 非流式
        logger.debug(f"Processing non-stream response: model={model}")
        result = await CollectProcessor(model_name, token).process(response)
        try:
            model_info = ModelService.get(model)
            effort = (
                EffortType.HIGH
                if (model_info and model_info.cost.value == "high")
                else EffortType.LOW
            )
            await token_mgr.consume(token, effort)
            logger.info(f"Chat completed: model={model}, effort={effort.value}")
        except Exception as e:
            logger.warning(f"Failed to record usage: {e}")
        return result


__all__ = [
    "GrokChatService",
    "ChatRequest",
    "ChatRequestBuilder",
    "MessageExtractor",
    "ChatService",
]
