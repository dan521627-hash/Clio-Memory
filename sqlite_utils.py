"""Small SQLite helpers shared by the sidecar stores."""

import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Keep transaction semantics and always release the database file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
