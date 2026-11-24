"""データの置き場所（Googleスプレッドシート / テスト・デモ用メモリ）。

Pi の SD カードには何も書かない設計。状態はすべてスプレッドシートにあり、
Pi はメモリ上のキャッシュを持つだけ。
"""
from __future__ import annotations

import threading

HEADER_SCHEDULE = ["館", "日付", "曜日", "鍵", "ごみ(正)", "ごみ(副)", "交代済"]
HEADER_LOG = [
    "操作日時", "館", "種別",
    "人1", "日付1(変更前)", "役割1",
    "人2", "日付2(変更前)", "役割2",
    "備考",
]
HEADER_CLOSED = ["日付", "理由"]


class MemoryBackend:
    """テスト・デモ用。プロセスが終われば消える。"""

    def __init__(self, roster=None, closed=None):
        self.roster = roster or []
        self.closed = closed or [HEADER_CLOSED]
        self.schedule = [list(HEADER_SCHEDULE)]
        self.log = [list(HEADER_LOG)]
        self.fail_writes = False
        self._lock = threading.Lock()

    def _check(self):
        if self.fail_writes:
            raise RuntimeError("書き込み失敗(テスト)")

    def read_roster(self):
        return [list(r) for r in self.roster]

    def read_closed_days(self):
        return [list(r) for r in self.closed]

    def read_schedule(self):
        return [list(r) for r in self.schedule]

    def read_log(self):
        return [list(r) for r in self.log]

    def write_schedule(self, rows):
        self._check()
        self.schedule = [list(HEADER_SCHEDULE)] + [list(r) for r in rows]

    def update_schedule_rows(self, updates):
        """updates: [(シート上の行番号(1始まり), [値...]), ...]"""
        self._check()
        for rownum, values in updates:
            self.schedule[rownum - 1] = list(values)

    def append_log(self, row):
        self._check()
        self.log.append(list(row))


class SheetsBackend:
    def __init__(self, spreadsheet_id, credentials_file, sheet_names):
        import gspread

        self._gspread = gspread
        self.names = sheet_names
        gc = gspread.service_account(filename=credentials_file)
        self.sh = gc.open_by_key(spreadsheet_id)
        self._ws = {}

    def _worksheet(self, key, header=None, create=True):
        title = self.names[key]
        if title in self._ws:
            return self._ws[title]
        try:
            ws = self.sh.worksheet(title)
        except self._gspread.WorksheetNotFound:
            if not create or header is None:
                raise
            ws = self.sh.add_worksheet(title=title, rows=1000, cols=max(len(header), 8))
            ws.update(range_name="A1", values=[header], value_input_option="RAW")
        self._ws[title] = ws
        return ws

    def read_roster(self):
        return self._worksheet("roster", create=False).get_all_values()

    def read_closed_days(self):
        return self._worksheet("closed", HEADER_CLOSED).get_all_values()

    def read_schedule(self):
        return self._worksheet("schedule", HEADER_SCHEDULE).get_all_values()

    def read_log(self):
        return self._worksheet("log", HEADER_LOG).get_all_values()

    def write_schedule(self, rows):
        ws = self._worksheet("schedule", HEADER_SCHEDULE)
        values = [HEADER_SCHEDULE] + rows
        if len(values) + 20 > ws.row_count:
            ws.resize(rows=len(values) + 200)
        ws.clear()
        ws.update(range_name="A1", values=values, value_input_option="RAW")

    def update_schedule_rows(self, updates):
        ws = self._worksheet("schedule", HEADER_SCHEDULE)
        data = [
            {"range": f"A{rownum}:G{rownum}", "values": [values]}
            for rownum, values in updates
        ]
        ws.batch_update(data, value_input_option="RAW")

    def append_log(self, row):
        ws = self._worksheet("log", HEADER_LOG)
        ws.append_row(row, value_input_option="RAW")
