import ast
import json
from datetime import datetime, timezone

import httpx

from app.domain.riot import LEAGUE_FLEX, LEAGUE_SOLO, RiotId

from .http import api_issue, request_api


URL = "https://mcp-api.op.gg/mcp"
PROTOCOL = "2025-03-26"
SUPPORTED_PROTOCOLS = {PROTOCOL, "2025-06-18", "2025-11-25"}
REGIONS = {
    "br1": "BR", "eun1": "EUNE", "euw1": "EUW", "jp1": "JP", "kr": "KR",
    "la1": "LAN", "la2": "LAS", "me1": "ME", "na1": "NA", "oc1": "OCE",
    "ru": "RU", "sg2": "SG", "tr1": "TR", "tw2": "TW", "vn2": "VN",
}
QUEUES = {"SOLORANKED": LEAGUE_SOLO, "FLEXRANKED": LEAGUE_FLEX}
DIVISIONS = {1: "I", 2: "II", 3: "III", 4: "IV"}
DIVISION_TIERS = {"IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"}
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}
FIELDS = [
    "data.summoner.{game_name,tagline,updated_at}",
    "data.summoner.league_stats[].{game_type,win,lose,updated_at}",
    "data.summoner.league_stats[].tier_info.{tier,division,lp}",
]


def rpc_result(response: httpx.Response, request_id: int) -> dict:
    if "text/event-stream" in response.headers.get("Content-Type", ""):
        # A POST may include notifications before the reply with our request ID.
        messages = (
            json.loads(line.removeprefix("data:").lstrip())
            for line in response.text.splitlines()
            if line.startswith("data:")
        )
        message = next((item for item in messages if isinstance(item, dict) and item.get("id") == request_id), None)
    else:
        message = response.json()
    if not isinstance(message, dict) or message.get("id") != request_id:
        raise ValueError("Missing OP.GG MCP response")
    result = message.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError("OP.GG MCP rejected the request")
    return result


def arguments(node: ast.AST, name: str, count: int) -> list[ast.AST]:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != name
        or node.keywords
        or len(node.args) != count
    ):
        raise ValueError("Unexpected OP.GG profile structure")
    return node.args


def scalar(node: ast.AST):
    if isinstance(node, ast.Constant) and type(node.value) in (str, int):
        return node.value
    if isinstance(node, ast.Name) and node.id == "null":
        return None
    raise ValueError("Unexpected OP.GG profile value")


def updated_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp.astimezone(timezone.utc) if timestamp.tzinfo else None


def parse_profile(result: dict, requested: RiotId) -> tuple[dict[str, dict], datetime | None]:
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise ValueError("Missing OP.GG profile")
    text = content[0].get("text")
    if not isinstance(text, str) or len(text) > 16_384:
        raise ValueError("Invalid OP.GG profile")
    # OP.GG serializes selected fields as class declarations followed by one expression.
    # Read its syntax tree without evaluating any part of the response.
    _, separator, expression = text.partition("\n\n")
    if not separator:
        raise ValueError("Missing OP.GG profile data")
    try:
        root = ast.parse(expression.strip(), mode="eval").body
    except (SyntaxError, RecursionError):
        raise ValueError("Invalid OP.GG profile data") from None
    data, = arguments(root, "LolGetSummonerProfile", 1)
    summoner, = arguments(data, "Data", 1)
    name, tag, updated_at, leagues = arguments(summoner, "Summoner", 4)
    name, tag = scalar(name), scalar(tag)
    if (
        not isinstance(name, str) or not isinstance(tag, str)
        or (name.casefold(), tag.casefold()) != requested.normalized
        or not isinstance(leagues, ast.List)
    ):
        raise ValueError("OP.GG returned a different profile")
    ranks = {}
    for league in leagues.elts:
        kind, rank_info, wins, losses, ranked_at = arguments(league, "LeagueStat", 5)
        queue = QUEUES.get(scalar(kind))
        if queue is None:
            continue
        tier, division, _ = map(scalar, arguments(rank_info, "TierInfo", 3))
        if tier in (None, "UNRANKED"):
            continue
        if queue in ranks or not (tier in APEX_TIERS or tier in DIVISION_TIERS and division in DIVISIONS):
            raise ValueError("Invalid OP.GG ranked entry")
        ranks[queue] = {
            "tier": tier,
            "division": DIVISIONS.get(division),
            "wins": scalar(wins),
            "losses": scalar(losses),
            "updated_at": updated_time(scalar(ranked_at)),
        }
    return ranks, updated_time(scalar(updated_at))


async def ranked_profile(http: httpx.AsyncClient, region: str, account: RiotId):
    headers = {"Accept": "application/json, text/event-stream"}

    async def post(method: str, params=None, request_id=None):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if request_id is not None:
            body["id"] = request_id
        response = await request_api(
            http, "opgg", method, "POST", URL, headers=headers, json=body, follow_redirects=False, timeout=5
        )
        return response.raise_for_status()

    opened = await post("initialize", {
        "protocolVersion": PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "PartyUp bot", "version": "1.0"},
    }, request_id=1)
    session = opened.headers.get("Mcp-Session-Id")
    if session:
        headers["Mcp-Session-Id"] = session
    operation = "initialize"
    try:
        version = rpc_result(opened, 1).get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_PROTOCOLS:
            raise ValueError("Unsupported OP.GG MCP protocol version")
        headers["MCP-Protocol-Version"] = version
        operation = "notifications/initialized"
        await post("notifications/initialized")
        operation = "tools/call"
        response = await post("tools/call", {
            "name": "lol_get_summoner_profile",
            "arguments": {
                "game_name": account.name,
                "tag_line": account.tag,
                "region": REGIONS[region],
                "desired_output_fields": FIELDS,
            },
        }, request_id=2)
        return parse_profile(rpc_result(response, 2), account)
    except ValueError:
        api_issue("opgg", operation, "invalid_mcp_response")
        raise
    finally:
        if session:
            try:
                await request_api(
                    http, "opgg", "close_session", "DELETE", URL,
                    headers=headers, follow_redirects=False, timeout=2,
                )
            except (httpx.RequestError, httpx.InvalidURL):
                pass
