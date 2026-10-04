from contextvars import ContextVar

current_account_id: ContextVar[str] = ContextVar("current_account_id", default="default")
