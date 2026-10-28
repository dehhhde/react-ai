from unittest.mock import patch

import pytest

from react_ai.llm.base_model_client import (
    BaseModelClient,
    ChatMessage,
    ChatRole,
    ModelConfig,
    ModelProvider,
    ModelResponse,
    ModelUsage,
    NonRetryableError,
    RetryableError,
)


class DummySuccessClient(BaseModelClient):
    provider = ModelProvider.CUSTOM
    default_model = "test-model"
    name = "dummy_client"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.call_count = 0

    def _do_chat(self, messages, config, trace_id):
        self.call_count += 1
        return ModelResponse(
            content="Hello world",
            model=config.model,
            usage=ModelUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class DummyFailingClient(BaseModelClient):
    provider = ModelProvider.CUSTOM
    name = "failing_client"

    def __init__(self, failure_type=RetryableError, **kwargs):
        super().__init__(**kwargs)
        self.failure_type = failure_type
        self.call_count = 0

    def _do_chat(self, messages, config, trace_id):
        self.call_count += 1
        raise self.failure_type("Mocked error")


class DummyRetryThenSuccessClient(BaseModelClient):
    provider = ModelProvider.CUSTOM
    name = "retry_success_client"

    def __init__(self, fail_times=2, **kwargs):
        super().__init__(**kwargs)
        self.fail_times = fail_times
        self.call_count = 0

    def _do_chat(self, messages, config, trace_id):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise RetryableError(f"Temporary failure {self.call_count}")
        return ModelResponse(
            content="Recovered after retry",
            model=config.model,
            usage=ModelUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


def test_chat_success():
    client = DummySuccessClient()
    messages = [ChatMessage(role=ChatRole.USER, content="Hi")]
    config = ModelConfig(model="test-gpt")

    response = client.chat(messages, config, trace_id="trace-123")

    assert response.content == "Hello world"
    assert response.trace_id == "trace-123"
    assert response.model == "test-gpt"
    assert response.latency_ms >= 0
    assert response.usage.total_tokens == 30
    assert client.total_calls == 1
    assert client.total_failures == 0
    assert client.total_tokens == 30

    stats = client.get_stats()
    assert stats["name"] == "dummy_client"
    assert stats["provider"] == "custom"
    assert stats["total_calls"] == 1
    assert stats["total_failures"] == 0
    assert stats["failure_rate"] == 0.0
    assert not stats["has_fallback"]


def test_empty_messages_validation():
    client = DummySuccessClient()
    with pytest.raises(NonRetryableError, match="messages 不能为空"):
        client.chat([])


@patch("time.sleep", return_value=None)
def test_retry_on_retryable_error(mock_sleep):
    client = DummyRetryThenSuccessClient(fail_times=2)
    messages = [ChatMessage(role=ChatRole.USER, content="Hi")]
    config = ModelConfig(max_retries=3, retry_base_delay=0.1)

    response = client.chat(messages, config)

    assert response.content == "Recovered after retry"
    assert client.call_count == 3
    assert mock_sleep.call_count == 2
    assert client.total_calls == 1
    assert client.total_failures == 0


def test_abort_on_non_retryable_error():
    client = DummyFailingClient(failure_type=NonRetryableError)
    messages = [ChatMessage(role=ChatRole.USER, content="Hi")]
    config = ModelConfig(max_retries=3)

    with pytest.raises(NonRetryableError, match="模型调用最终失败"):
        client.chat(messages, config)

    assert client.call_count == 1
    assert client.total_calls == 1
    assert client.total_failures == 1


@patch("time.sleep", return_value=None)
def test_fallback_mechanism(mock_sleep):
    fallback_client = DummySuccessClient()
    fallback_client.name = "backup_model"

    primary_client = DummyFailingClient(failure_type=RetryableError, fallback_client=fallback_client)
    messages = [ChatMessage(role=ChatRole.USER, content="Hi")]
    config = ModelConfig(max_retries=2, retry_base_delay=0.01)

    response = primary_client.chat(messages, config)

    assert "[降级模型 backup_model] Hello world" in response.content
    assert primary_client.call_count == 2
    assert fallback_client.call_count == 1


@patch("time.sleep", return_value=None)
def test_all_retries_and_fallback_exhausted(mock_sleep):
    fallback_client = DummyFailingClient(failure_type=NonRetryableError)
    fallback_client.name = "failing_backup"

    primary_client = DummyFailingClient(failure_type=RetryableError, fallback_client=fallback_client)
    messages = [ChatMessage(role=ChatRole.USER, content="Hi")]
    config = ModelConfig(max_retries=2, retry_base_delay=0.01)

    with pytest.raises(NonRetryableError, match="模型调用最终失败"):
        primary_client.chat(messages, config)

    assert primary_client.total_failures == 1


def test_build_messages_and_to_dict():
    history = [
        ChatMessage(role=ChatRole.USER, content="Previous question"),
        ChatMessage(role=ChatRole.ASSISTANT, content="Previous answer"),
    ]
    messages = BaseModelClient.build_messages(
        system_prompt="You are helpful.",
        user_prompt="New question",
        history=history,
    )

    assert len(messages) == 4
    assert messages[0].role == ChatRole.SYSTEM
    assert messages[0].content == "You are helpful."
    assert messages[1].role == ChatRole.USER
    assert messages[2].role == ChatRole.ASSISTANT
    assert messages[3].role == ChatRole.USER

    dict_list = BaseModelClient.messages_to_dict(messages)
    assert len(dict_list) == 4
    assert dict_list[0] == {"role": "system", "content": "You are helpful."}
    assert dict_list[1] == {"role": "user", "content": "Previous question"}
    assert dict_list[2] == {"role": "assistant", "content": "Previous answer"}
    assert dict_list[3] == {"role": "user", "content": "New question"}
