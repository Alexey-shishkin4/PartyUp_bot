from datetime import datetime, timedelta, timezone

import httpx

from app.integrations.fortnite import FortniteClient, RequestBudget
from app.integrations.fortnite_stats import raw_activity

ACCOUNT_ID = "a" * 32


async def test_fortnite_import_uses_exact_window_and_keeps_playtime_and_ranks_per_mode():
    def handler(request):
        if request.url.path == "/v1/accounts/lookup-by-display-name":
            assert request.url.params["displayName"] == "Test Player"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "accounts": [{"accountId": ACCOUNT_ID, "accountDetails": {"epic": "Test Player"}}],
                },
            )
        if request.url.path == "/v1/stats/account":
            assert request.url.params["timeframe"] == "lifetime"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "groupedStats": {
                        "keyboardmouse": {"solo": {"minutesplayed": 300}, "duo": {"minutesplayed": 120}},
                        "gamepad": {"solo": {"minutesplayed": 30}},
                    },
                },
            )
        if request.url.path == "/v1/ranked/account-ranks":
            return httpx.Response(
                200,
                json={
                    "modes": [
                        {
                            "rankingType": "ranked-br-combined",
                            "rankingTrackId": "s1",
                            "currentDivision": {"divisionName": "Diamond II"},
                        },
                        {
                            "rankingType": "ranked-zb",
                            "rankingTrackId": "s1",
                            "currentDivision": {"divisionName": "Gold I"},
                        },
                    ]
                },
            )
        assert request.url.path == f"/api/v2/stats/{ACCOUNT_ID}"
        assert request.headers["x-api-key"] == "window-key"
        params = request.url.params
        if "startTime" in params:
            assert int(params["endTime"]) - int(params["startTime"]) == 30 * 86400
            assert abs(
                datetime.fromtimestamp(int(params["endTime"]), timezone.utc) - datetime.now(timezone.utc)
            ) < timedelta(seconds=10)
            return httpx.Response(
                200,
                json={
                    "stats": {
                        "br_minutesplayed_keyboardmouse_m0_playlist_defaultsolo": 50,
                        "br_minutesplayed_keyboardmouse_m0_playlist_nobuildbr_duo": 80,
                        "br_minutesplayed_gamepad_m0_playlist_nobuildbr_duo": 20,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "stats": {
                    "br_minutesplayed_keyboardmouse_m0_playlist_defaultsolo": 350,
                    "br_minutesplayed_keyboardmouse_m0_playlist_nobuildbr_duo": 100,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await FortniteClient(client, api_fortnite_key="window-key").fetch("Test Player")

    assert result.account.display_name == "Test Player"
    assert result.playtime.total_minutes == 450
    assert result.playtime.modes == {
        "team:solo": 330,
        "team:duo": 120,
        "battle_royale": 350,
        "zero_build": 100,
    }
    assert result.activity.favorite_mode() == "zero_build"
    assert result.ranks["zero_build"].name == "Gold I"


async def test_dashboard_fallback_keeps_lifetime_separate_from_recent_modes():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "fnapi.osirion.gg":
            return httpx.Response(429, headers={"Retry-After": "90"})
        assert request.url.host == "fortnite-api.com"
        assert request.headers["authorization"] == "dashboard-key"
        return httpx.Response(
            200,
            json={
                "data": {
                    "account": {"id": ACCOUNT_ID, "name": "Dashboard Player"},
                    "stats": {"all": {"overall": {"minutesPlayed": 900}, "solo": {"minutesPlayed": 200}}},
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await FortniteClient(client, fortnite_api_key="dashboard-key").fetch("Dashboard Player")
    assert len(calls) == 2
    assert result.playtime.total_minutes == 900
    assert result.playtime.modes == {"team:solo": 200}
    assert result.activity is None and result.ranks is None


def test_raw_activity_counts_unique_playlists_and_ignores_overlapping_summaries():
    payload = {
        "stats": {
            "br_minutesplayed_all_m0_playlist_defaultsolo": 20,
            "br_minutesplayed_gamepad_m0_playlist_defaultsolo": 20,
            "br_minutesplayed_gamepad_m1_playlist_defaultsolo": 90,
            "br_minutesplayed_gamepad_m0": 100,
            "br_matchesplayed_gamepad_m0_playlist_nobuildbr_solo": 5,
        }
    }
    activity = raw_activity(payload)
    assert activity.minutes == {"battle_royale": 20}
    assert activity.matches == {"zero_build": 5}
    assert activity.favorite_mode() == "battle_royale"
    assert raw_activity({"stats": {}}).favorite_mode() is None


def test_reload_rotation_maps_ranked_and_unranked_playlists_to_modes():
    activity = raw_activity(
        {
            "stats": {
                "br_minutesplayed_keyboardmouse_m0_playlist_habanero_blendheaded_solo": 20,
                "br_minutesplayed_keyboardmouse_m0_playlist_habanero_sourspawn_duos": 30,
                "br_minutesplayed_keyboardmouse_m0_playlist_sourspawnsquad": 40,
                "br_minutesplayed_keyboardmouse_m0_playlist_habanero_timberstake_solo": 50,
                "br_minutesplayed_keyboardmouse_m0_playlist_habanero_nobuild_timberstake_squads": 10,
            }
        }
    )
    assert activity.minutes == {"reload": 140, "reload_zero_build": 10}
    assert activity.favorite_mode() == "reload"


def test_request_budget_honors_local_ceiling_and_provider_retry_after():
    budget = RequestBudget(per_minute=2)
    assert budget.reserve() and budget.reserve() and not budget.reserve()
    budget = RequestBudget()
    budget.observe(httpx.Response(429, headers={"Retry-After": "120"}))
    assert not budget.reserve()
