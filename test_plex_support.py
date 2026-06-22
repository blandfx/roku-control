import unittest
from unittest.mock import AsyncMock, patch

from plex_support import (
    _catalog_item,
    _parse_server_resources,
    PlexError,
    choose_show_episode,
    format_metadata_title,
    get_media_state,
    is_plex_video_id,
    make_plex_video_id,
    parse_plex_video_id,
)


class PlexIdentityTests(unittest.TestCase):
    def test_stable_id_round_trip(self):
        video_id = make_plex_video_id("server123", "5323")
        self.assertEqual(video_id, "plex:server123:5323")
        self.assertEqual(parse_plex_video_id(video_id), ("server123", "5323"))
        self.assertTrue(is_plex_video_id(video_id))

    def test_legacy_id_is_recognized_but_not_resumable(self):
        self.assertTrue(is_plex_video_id("plex_437984"))
        with self.assertRaisesRegex(PlexError, "predates reliable resume"):
            parse_plex_video_id("plex_437984")

    def test_episode_title(self):
        title = format_metadata_title({
            "type": "episode",
            "title": "Chickenrat",
            "grandparentTitle": "Bluey",
            "parentIndex": 1,
            "index": 46,
        })
        self.assertEqual(title, "Bluey - S01E46 - Chickenrat")

    def test_movie_title(self):
        self.assertEqual(
            format_metadata_title({"type": "movie", "title": "Arrival", "year": 2016}),
            "Arrival (2016)",
        )

    def test_v2_resource_schema(self):
        resources = _parse_server_resources(
            '''<resources><resource name="Shared Server" product="Plex Media Server"
               provides="server" clientIdentifier="server123" accessToken="resource-token"
               owned="0"><connections><connection uri="https://example.plex.direct:32400"
               local="0" relay="0" /></connections></resource></resources>''',
            "account-token",
        )
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["id"], "server123")
        self.assertEqual(resources[0]["token"], "resource-token")
        self.assertEqual(resources[0]["connections"][0]["uri"], "https://example.plex.direct:32400")

    def test_kid_tv_catalog_contains_show_not_episode(self):
        item = _catalog_item("server123", "Kid TV Shows", {
            "ratingKey": "5000", "type": "show", "title": "Bluey", "year": 2018,
        })
        self.assertEqual(item["video_id"], "plex-show:server123:5000")
        self.assertEqual(item["title"], "Bluey")
        self.assertIn("bluey", item["search_text"])
        self.assertEqual(item["thumbnail_url"], "api/plex/art/server123/5000")
        self.assertIsNone(_catalog_item("server123", "Kid TV Shows", {
            "ratingKey": "5323", "type": "episode", "title": "Chickenrat"
        }))

    def test_show_continue_prefers_in_progress_episode(self):
        episode = choose_show_episode([
            {"ratingKey": "1", "type": "episode", "parentIndex": 1, "index": 1, "duration": 1000, "viewCount": 1},
            {"ratingKey": "2", "type": "episode", "parentIndex": 1, "index": 2, "duration": 1000, "viewOffset": 400},
            {"ratingKey": "3", "type": "episode", "parentIndex": 1, "index": 3, "duration": 1000},
        ])
        self.assertEqual(episode["ratingKey"], "2")

    def test_show_continue_uses_next_unwatched_episode(self):
        episode = choose_show_episode([
            {"ratingKey": "1", "type": "episode", "parentIndex": 1, "index": 1, "viewCount": 1},
            {"ratingKey": "2", "type": "episode", "parentIndex": 1, "index": 2},
            {"ratingKey": "3", "type": "episode", "parentIndex": 1, "index": 3},
        ])
        self.assertEqual(episode["ratingKey"], "2")

    def test_kid_movie_catalog_item_includes_year(self):
        item = _catalog_item("server123", "Kid Movies", {
            "ratingKey": "88", "type": "movie", "title": "Paddington", "year": 2014
        })
        self.assertEqual(item["title"], "Paddington (2014)")
        self.assertEqual(item["subtitle"], "Kid Movies • 2014")


class PlexMediaStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_identity_survives_missing_account(self):
        timeline = {
            "state": "playing",
            "server_id": "server123",
            "rating_key": "5323",
            "position_ms": 12000,
            "duration_ms": 43000,
        }
        with patch("plex_support.poll_client_timeline", AsyncMock(return_value=timeline)), patch(
            "plex_support.get_metadata", AsyncMock(side_effect=PlexError("not connected"))
        ):
            state = await get_media_state("192.0.2.10", "client", None)
        self.assertEqual(state["video_id"], "plex:server123:5323")
        self.assertIn("images.unsplash.com", state["thumbnail_url"])

    async def test_connected_account_uses_protected_art_route(self):
        timeline = {
            "state": "paused",
            "server_id": "server123",
            "rating_key": "5323",
            "position_ms": 12000,
            "duration_ms": 43000,
        }
        with patch("plex_support.poll_client_timeline", AsyncMock(return_value=timeline)), patch(
            "plex_support.get_metadata", AsyncMock(side_effect=PlexError("temporarily unavailable"))
        ):
            state = await get_media_state("192.0.2.10", "client", "token")
        self.assertEqual(state["thumbnail_url"], "api/plex/art/server123/5323")


if __name__ == "__main__":
    unittest.main()
