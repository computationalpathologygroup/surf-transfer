"""Per-run file/byte budget."""

from __future__ import annotations


class RunBudget:
    """Tracks how many files/bytes a single run is allowed to download."""

    def __init__(self, max_files: int | None = None, max_bytes: int | None = None):
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.files_done = 0
        self.bytes_done = 0
        self.stop_reason: str | None = None
        self.oversized_file = False

    @property
    def exhausted(self) -> bool:
        return self.stop_reason is not None

    def can_start(self, size: int) -> bool:
        """Whether a file of this size may begin downloading under the budget."""
        if self.exhausted:
            return False
        if self.max_files is not None and self.files_done >= self.max_files:
            self.stop_reason = f"reached --max-files limit ({self.max_files} file(s))"
            return False
        if self.max_bytes is not None:
            if size > self.max_bytes:
                self.oversized_file = True
                self.stop_reason = (
                    f"file size ({size} bytes) exceeds --max-bytes budget ({self.max_bytes} bytes)"
                )
                return False
            if self.bytes_done + size > self.max_bytes:
                self.stop_reason = f"reached --max-bytes limit ({self.max_bytes} bytes)"
                return False
        return True

    def record(self, size: int) -> None:
        self.files_done += 1
        self.bytes_done += size
