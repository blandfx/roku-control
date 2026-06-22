import os
import tempfile
import unittest
from unittest.mock import patch

import database


class HistoryDeletionTests(unittest.TestCase):
    def test_delete_removes_grouped_media_history(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            with patch.object(database, "DB_PATH", path):
                database.db_init()
                first_id = database.db_add_history_entry(
                    "192.0.2.1", "One", "video-a", "Video A", "thumb", 100, 10
                )
                database.db_add_history_entry(
                    "192.0.2.2", "Two", "video-a", "Video A", "thumb", 100, 20
                )
                database.db_add_history_entry(
                    "192.0.2.1", "One", "video-b", "Video B", "thumb", 100, 30
                )
                self.assertEqual(database.db_delete_history_entry(first_id), 2)
                self.assertEqual([item["video_id"] for item in database.db_get_history()], ["video-b"])
        finally:
            os.unlink(path)

    def test_plex_history_shows_only_latest_episode_per_show(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            with patch.object(database, "DB_PATH", path):
                database.db_init()
                database.db_add_history_entry(
                    "192.0.2.1", "One", "plex:server:100", "Bluey - S01E01 - Magic Xylophone", "thumb", 420, 100
                )
                database.db_add_history_entry(
                    "192.0.2.1", "One", "plex:server:101", "Bluey - S01E02 - Hospital", "thumb", 420, 20
                )
                database.db_add_history_entry(
                    "192.0.2.1", "One", "plex:server:200", "Another Show - S01E01 - Pilot", "thumb", 1800, 30
                )

                history = database.db_get_history()
                self.assertEqual(
                    [item["video_title"] for item in history],
                    ["Another Show - S01E01 - Pilot", "Bluey - S01E02 - Hospital"],
                )
        finally:
            os.unlink(path)

    def test_deleting_latest_plex_episode_deletes_show_group(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            with patch.object(database, "DB_PATH", path):
                database.db_init()
                database.db_add_history_entry(
                    "192.0.2.1", "One", "plex:server:100", "Bluey - S01E01 - Magic Xylophone", "thumb", 420, 100
                )
                latest_id = database.db_add_history_entry(
                    "192.0.2.1", "One", "plex:server:101", "Bluey - S01E02 - Hospital", "thumb", 420, 20
                )

                self.assertEqual(database.db_delete_history_entry(latest_id), 2)
                self.assertEqual(database.db_get_history(), [])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
