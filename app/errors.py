from typing import Any


class ImageError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: Any = None,
        status_code: int = 400
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details
        self.status_code = status_code

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "details": self.details,
        }
