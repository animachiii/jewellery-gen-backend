from dataclasses import dataclass, field


@dataclass
class RecordedCall:
    method: str
    range_: str
    values: list[str]


@dataclass
class FakeSheetsClient:
    """Records calls instead of hitting the real Sheets API (docs/conventions.md
    → Testing: no test may call a real external service)."""

    calls: list[RecordedCall] = field(default_factory=list)
    fail_next: bool = False
    _next_row: int = 2  # row 1 is the header
    rows: list[list[str]] = field(default_factory=list)  # what read_all_rows returns

    def append_row(self, sheet_id: str, tab: str, values: list[str]) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated Sheets API failure")
        row = self._next_row
        self._next_row += 1
        range_ = f"{tab}!A{row}:G{row}"
        self.calls.append(RecordedCall("append", range_, list(values)))
        return range_

    def update_row(self, sheet_id: str, range_: str, values: list[str]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated Sheets API failure")
        self.calls.append(RecordedCall("update", range_, list(values)))

    def read_all_rows(self, sheet_id: str, tab: str) -> list[list[str]]:
        return self.rows
