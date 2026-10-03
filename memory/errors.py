from __future__ import annotations


class MemoryError(RuntimeError):
    def __init__(self, error_code: str, detail: str, status_code: int = 500):
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.status_code = int(status_code)
