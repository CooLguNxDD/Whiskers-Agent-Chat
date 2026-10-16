"""Agent feed: posts, nested threads, reposts and quotes, follows, hashtags, timelines."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys

import pytest
from fastmcp import Client

from cat_fleet_chat import feed, mcp_app
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import chat_payloads, event_line, matches


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "feed.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


def pid(result: dict) -> int:
    return result["post"]["id"]


async def all_events(hub: Hub) -> list[dict]:
    return await hub.events_after(0, channel=None, limit=1_000)


async def until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def read_whole_thread(fetch, start_id: int) -> dict[int, dict]:
    """Every post reachable from ``start_id`` by following ``more_replies``.

    Checks that each nested node sits under its true parent and that a read
    never nests deeper than the window.
    """
    seen: dict[int, dict] = {}
    queue = [start_id]
    while queue:
        page = await fetch(queue.pop())
        json.dumps(page)
        stack = [(node, None, 1) for node in page["posts"]]
        while stack:
            node, parent, level = stack.pop()
            assert level <= feed.THREAD_NEST_MAX
            if parent is not None:
                assert node["parent_id"] == parent
            seen.setdefault(node["id"], node)
            if node["more_replies"]:
                assert node["replies"] == []
                queue.append(node["id"])
            stack.extend((child, node["id"], level + 1) for child in node["replies"])
    return seen


# ---- pure helpers --------------------------------------------------------


def test_hashtags_case_dedupe_and_punctuation():
    text = "Shipping #Fleet today! #fleet, #FLEET. (#ops) #café #snake_case #a-b end#glued ##double"
    assert feed.extract_hashtags(text) == ["fleet", "ops", "café", "snake_case", "a"]


def test_hashtags_allow_digits_and_underscores_only():
    assert feed.extract_hashtags("#42 #2026, #_ #__init #٣ #42") == ["42", "2026", "_", "__init", "٣"]


def test_hashtags_skip_code_urls_and_long_words():
    text = (
        "fixes #42 see https://example.test/page#anchor and example.test/#frag "
        "`#inline` ```\n#fenced\n``` &#39; #" + "x" * 51 + " #" + "y" * 50 + " #ok"
    )
    assert feed.extract_hashtags(text) == ["42", "y" * 50, "ok"]


def test_body_normalising_and_limits():
    assert feed.normalize_body("  hi\r\nthere  ") == "hi\nthere"
    with pytest.raises(HubError):
        feed.normalize_body("   ")
    with pytest.raises(HubError):
        feed.normalize_body("x" * (feed.BODY_MAX + 1))
    assert feed.normalize_body("", required=False) == ""


def test_cursor_round_trip_and_rejects_garbage():
    token = feed.encode_cursor(1234)
    assert "1234" not in token
    assert feed.decode_cursor(token) == 1234
    assert feed.decode_cursor(None) is None and feed.decode_cursor("") is None
    for bad in ("nope", "!!!", feed.encode_cursor(0), 12, "A" * 100):
        with pytest.raises(HubError) as info:
            feed.decode_cursor(bad)
        assert info.value.code == "validation_error"


def _row(post_id: int, parent: int | None, depth: int, **extra) -> dict:
    return {
        "id": post_id,
        "author": "ada",
        "body": f"post {post_id}",
        "parent_id": parent,
        "root_id": 1,
        "depth": depth,
        "repost_of": None,
        "quote_of": None,
        "created_at": "t",
        "deleted_at": None,
        **extra,
    }


def test_build_thread_is_iterative_at_any_depth():
    depth = 5_000
    posts = [feed.shape_post(_row(i + 1, i if i else None, i)) for i in range(depth)]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        roots = feed.build_thread(posts)
        walked = list(feed.iter_tree(roots))
    finally:
        sys.setrecursionlimit(old_limit)
    assert len(roots) == 1 and len(walked) == depth
    assert [node["id"] for node in walked] == list(range(1, depth + 1))
    # True nesting: every node holds exactly the next one.
    assert all(len(node["replies"]) == (0 if node["id"] == depth else 1) for node in walked)


def test_thread_window_top_always_moves_deeper_from_an_edge():
    assert feed.thread_window_top(0) == 0
    assert feed.thread_window_top(feed.THREAD_NEST_MAX - feed.THREAD_CONTEXT - 1) == 0
    top = 0
    for _ in range(100):
        edge = top + feed.THREAD_NEST_MAX - 1
        deeper = feed.thread_window_top(edge)
        assert top < deeper and edge - deeper == feed.THREAD_CONTEXT
        top = deeper


def test_build_thread_sibling_order_tombstones_and_orphans():
    rows = [
        _row(1, None, 0),
        _row(2, 1, 1, deleted_at="gone"),
        _row(3, 1, 1),
        _row(4, 2, 2),  # child of the deleted post survives
        _row(5, 1, 1),
    ]
    # Shuffled input still yields chronological siblings.
    shaped = [feed.shape_post(row) for row in reversed(rows)]
    (root,) = feed.build_thread(shaped, focus_id=4)
    assert [child["id"] for child in root["replies"]] == [2, 3, 5]
    tomb = root["replies"][0]
    assert tomb["deleted"] is True and tomb["body"] == "" and tomb["replies"][0]["id"] == 4
    assert tomb["replies"][0]["focus"] is True

    # A post whose parent is not in the list (window top, earlier page) is top level.
    window = [feed.shape_post(_row(i + 1, i, i)) for i in range(5, 10)]
    (top,) = feed.build_thread(window)
    assert top["id"] == 6 and top["parent_id"] == 5
    assert [node["id"] for node in feed.iter_tree([top])] == [6, 7, 8, 9, 10]


# ---- posts, replies, threads ----------------------------------------------


async def test_post_extracts_hashtags_and_has_counts(hub):
    result = await hub.feed_post("ada", "  Ship it #Fleet #fleet #ops  ")
    post = result["post"]
    assert post["author"] == "ada" and post["body"] == "Ship it #Fleet #fleet #ops"
    assert post["kind"] == "post" and post["depth"] == 0 and post["root_id"] == post["id"]
    assert post["hashtags"] == ["fleet", "ops"]
    assert (post["reply_count"], post["repost_count"], post["quote_count"]) == (0, 0, 0)
    with pytest.raises(HubError):
        await hub.feed_post("ada", "")
    with pytest.raises(HubError):
        await hub.feed_post("not a handle", "hi")


async def test_reply_chain_tracks_root_and_depth(hub):
    root = pid(await hub.feed_post("ada", "root #deep"))
    parent = root
    ids = [root]
    for i in range(55):
        reply = (await hub.feed_post("bob", f"reply {i}", parent_id=parent))["post"]
        assert reply["root_id"] == root and reply["depth"] == i + 1
        assert reply["parent_id"] == parent and reply["kind"] == "reply"
        parent = reply["id"]
        ids.append(parent)

    # A deep post: the window starts a few levels above it, ancestors come flat.
    thread = await hub.feed_timeline(scope="thread", post_id=ids[30])
    top = 30 - feed.THREAD_CONTEXT
    assert thread["root_id"] == root and thread["focus_id"] == ids[30]
    assert thread["top_id"] == ids[top] and thread["has_more"] is False
    assert [p["id"] for p in thread["ancestors"]] == ids[:top]
    assert all("replies" not in p for p in thread["ancestors"])
    walked = list(feed.iter_tree(thread["posts"]))
    assert [node["id"] for node in walked] == ids[top : top + feed.THREAD_NEST_MAX]
    assert [node for node in walked if node.get("focus")][0]["id"] == ids[30]
    assert [node["id"] for node in walked if node["more_replies"]] == [ids[top + feed.THREAD_NEST_MAX - 1]]
    json.dumps(thread)

    # Following more_replies from the root reaches every post under its true parent.
    async def fetch(post_id):
        return await hub.feed_timeline(scope="thread", post_id=post_id)

    seen = await read_whole_thread(fetch, root)
    assert sorted(seen) == ids
    assert seen[ids[55]]["depth"] == 55 and seen[ids[55]]["parent_id"] == ids[54]
    assert seen[root]["reply_count"] == 1


async def test_wide_thread_pages_by_cursor_and_keeps_the_focus(hub, monkeypatch):
    monkeypatch.setattr(feed, "THREAD_ROWS_MAX", 4)
    root = pid(await hub.feed_post("ada", "wide"))
    replies = [pid(await hub.feed_post("bob", f"r{i}", parent_id=root)) for i in range(9)]
    late = pid(await hub.feed_post("cy", "late", parent_id=replies[-1]))
    seen: list[int] = []
    cursor = None
    pages = []
    while True:
        page = await hub.feed_timeline(scope="thread", post_id=late, cursor=cursor)
        pages.append(page)
        seen.extend(node["id"] for node in feed.iter_tree(page["posts"]))
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    first = list(feed.iter_tree(pages[0]["posts"]))
    # The requested post and its parent chain are on the first page.
    assert [n["id"] for n in first if n.get("focus")] == [late]
    assert {root, replies[-1], late} <= {n["id"] for n in first}
    assert len(pages) == 3
    assert sorted(seen) == sorted([root, *replies, late]) and len(seen) == len(set(seen))
    # A reply whose parent is on an earlier page comes back top level, parent_id intact.
    assert all(node["parent_id"] == root for node in pages[1]["posts"])


async def test_reply_rules_parent_must_exist_live_and_depth_is_capped(hub, monkeypatch):
    with pytest.raises(HubError) as info:
        await hub.feed_post("ada", "hi", parent_id=999)
    assert info.value.code == "not_found"

    root = pid(await hub.feed_post("ada", "root"))
    await hub.feed_delete("ada", root)
    with pytest.raises(HubError) as info:
        await hub.feed_post("bob", "late", parent_id=root)
    assert info.value.code == "post_deleted"

    monkeypatch.setattr(feed, "DEPTH_MAX", 3)
    parent = pid(await hub.feed_post("ada", "fresh"))
    for _ in range(3):
        parent = pid(await hub.feed_post("bob", "deeper", parent_id=parent))
    with pytest.raises(HubError) as info:
        await hub.feed_post("bob", "too deep", parent_id=parent)
    assert info.value.code == "thread_too_deep" and info.value.details["max_depth"] == 3


async def test_thread_siblings_are_chronological(hub):
    root = pid(await hub.feed_post("ada", "question"))
    first = pid(await hub.feed_post("bob", "a", parent_id=root))
    second = pid(await hub.feed_post("cy", "b", parent_id=root))
    nested = pid(await hub.feed_post("ada", "a.1", parent_id=first))
    third = pid(await hub.feed_post("dee", "c", parent_id=root))
    (tree,) = (await hub.feed_timeline(scope="thread", post_id=root))["posts"]
    assert [child["id"] for child in tree["replies"]] == [first, second, third]
    assert tree["replies"][0]["replies"][0]["id"] == nested
    assert tree["reply_count"] == 3 and tree["focus"] is True


# ---- reposts and quotes ---------------------------------------------------


async def test_quote_and_repost_carry_the_original_inline(hub):
    original = pid(await hub.feed_post("ada", "original #news"))
    quote = (await hub.feed_post("bob", "so true #take", quote_of=original))["post"]
    assert quote["kind"] == "quote" and quote["quote_of"] == original
    assert quote["original"]["id"] == original and quote["original"]["body"] == "original #news"
    assert quote["original"]["original"] is None  # one level only

    repost = (await hub.feed_repost("cy", original))["post"]
    assert repost["kind"] == "repost" and repost["body"] == "" and repost["repost_of"] == original
    assert repost["original"]["author"] == "ada" and repost["original"]["hashtags"] == ["news"]

    # Quoting a quote inlines that quote, but not what it quoted.
    nested = (await hub.feed_post("dee", "meta", quote_of=quote["id"]))["post"]
    assert nested["original"]["id"] == quote["id"] and nested["original"]["original"] is None

    shown = (await hub.feed_get_post(original))["post"]
    assert (shown["repost_count"], shown["quote_count"]) == (1, 1)

    await hub.feed_delete("ada", original)
    still = (await hub.feed_get_post(repost["id"]))["post"]
    assert still["original"] == {"id": original, "deleted": True}


async def test_repost_is_idempotent_and_resolves_to_the_original(hub):
    original = pid(await hub.feed_post("ada", "post"))
    first = await hub.feed_repost("bob", original)
    again = await hub.feed_repost("bob", original)
    assert first["idempotent"] is False and again["idempotent"] is True
    assert pid(first) == pid(again)

    via_repost = await hub.feed_repost("cy", pid(first))
    assert via_repost["post"]["repost_of"] == original
    # Reposting your own post is fine, and so is reposting a repost of it.
    own = await hub.feed_repost("ada", pid(via_repost))
    assert own["post"]["repost_of"] == original
    assert (await hub.feed_get_post(original))["post"]["repost_count"] == 3

    await hub.feed_delete("bob", pid(first))
    back = await hub.feed_repost("bob", original)
    assert pid(back) == pid(first) and back["post"]["deleted"] is False

    with pytest.raises(HubError) as info:
        await hub.feed_repost("bob", 9_999)
    assert info.value.code == "not_found"


# ---- deletes --------------------------------------------------------------


async def test_soft_delete_keeps_a_tombstone_in_the_thread_only(hub):
    root = pid(await hub.feed_post("ada", "root #topic"))
    middle = pid(await hub.feed_post("bob", "middle #topic", parent_id=root))
    leaf = pid(await hub.feed_post("cy", "leaf", parent_id=middle))
    deleted = await hub.feed_delete("bob", middle)
    assert deleted["idempotent"] is False and deleted["post"]["deleted"] is True
    assert (await hub.feed_delete("bob", middle))["idempotent"] is True

    for scope, extra in (("global", {}), ("hashtag", {"tag": "topic"}), ("author", {"author": "bob"})):
        ids = [p["id"] for p in (await hub.feed_timeline(scope=scope, **extra))["posts"]]
        assert middle not in ids, scope

    (tree,) = (await hub.feed_timeline(scope="thread", post_id=leaf))["posts"]
    tomb = tree["replies"][0]
    assert tomb["id"] == middle and tomb["deleted"] is True and tomb["body"] == ""
    assert tomb["hashtags"] == [] and tomb["replies"][0]["id"] == leaf
    assert tree["reply_count"] == 0  # deleted replies do not count


async def test_delete_is_forbidden_for_anyone_but_the_author(hub):
    post = pid(await hub.feed_post("ada", "mine"))
    with pytest.raises(HubError) as info:
        await hub.feed_delete("bob", post)
    assert info.value.code == "forbidden" and info.value.status == 403
    assert (await hub.feed_get_post(post))["post"]["deleted"] is False
    with pytest.raises(HubError) as info:
        await hub.feed_delete("ada", 12_345)
    assert info.value.code == "not_found"


# ---- follows and timelines --------------------------------------------------


async def test_follow_and_unfollow_are_idempotent_and_never_self(hub):
    first = await hub.feed_follow("cy", "ada")
    assert first["following"] is True and first["idempotent"] is False
    assert (await hub.feed_follow("cy", "ada"))["idempotent"] is True
    await hub.feed_follow("bob", "ada")
    assert (await hub.feed_following("cy"))["following"] == ["ada"]
    assert (await hub.feed_followers("ada"))["followers"] == ["bob", "cy"]

    gone = await hub.feed_unfollow("cy", "ada")
    assert gone["following"] is False and gone["idempotent"] is False
    assert (await hub.feed_unfollow("cy", "ada"))["idempotent"] is True
    assert (await hub.feed_followers("ada"))["followers"] == ["bob"]

    with pytest.raises(HubError) as info:
        await hub.feed_follow("ada", "ada")
    assert info.value.code == "validation_error"


async def test_following_timeline_shows_only_followed_authors(hub):
    await hub.feed_post("ada", "from ada")
    await hub.feed_post("bob", "from bob")
    await hub.feed_post("cy", "from cy (the reader)")
    other = pid(await hub.feed_post("dee", "from dee"))
    await hub.feed_repost("ada", other)
    await hub.feed_follow("cy", "ada")
    await hub.feed_follow("cy", "bob")

    page = await hub.feed_timeline(scope="following", handle="cy")
    assert {p["author"] for p in page["posts"]} == {"ada", "bob"}
    assert len(page["posts"]) == 3  # ada's post, bob's post, ada's repost of dee
    assert [p["id"] for p in page["posts"]] == sorted((p["id"] for p in page["posts"]), reverse=True)
    assert (await hub.feed_timeline(scope="following", handle="dee"))["posts"] == []
    with pytest.raises(HubError):
        await hub.feed_timeline(scope="following")
    with pytest.raises(HubError):
        await hub.feed_timeline(scope="sideways")


async def test_hashtag_timeline_and_trending(hub):
    a = pid(await hub.feed_post("ada", "one #Fleet"))
    await hub.feed_post("bob", "nothing here")
    b = pid(await hub.feed_post("cy", "two #fleet #ops"))
    await hub.feed_post("dee", "three #ops")
    page = await hub.feed_timeline(scope="hashtag", tag="#FLEET")
    assert [p["id"] for p in page["posts"]] == [b, a]

    trending = await hub.feed_trending_tags()
    assert trending["tags"][:2] == [{"tag": "fleet", "count": 2}, {"tag": "ops", "count": 2}]
    assert (await hub.feed_trending_tags(limit=1))["tags"] == [{"tag": "fleet", "count": 2}]
    assert (await hub.feed_trending_tags(since="2999-01-01T00:00:00Z"))["tags"] == []
    with pytest.raises(HubError):
        await hub.feed_trending_tags(since="yesterday-ish")
    with pytest.raises(HubError):
        await hub.feed_timeline(scope="hashtag", tag="bad tag!")


async def test_cursor_paging_is_stable_under_concurrent_inserts(hub):
    before = [pid(await hub.feed_post("ada", f"post {i}")) for i in range(25)]
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        page = await hub.feed_timeline(scope="global", cursor=cursor, limit=7)
        seen.extend(p["id"] for p in page["posts"])
        pages += 1
        # Others keep posting while we page backwards.
        await hub.feed_post("bob", f"interleaved {pages}")
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert pages == 4
    assert len(seen) == len(set(seen))  # no duplicates
    assert seen == sorted(before, reverse=True)  # no skips, none of the new posts


async def test_client_request_id_makes_a_post_idempotent(hub):
    first = await hub.feed_post("ada", "once #only", client_request_id="req-1")
    again = await hub.feed_post("ada", "once #only", client_request_id="req-1")
    assert first == again
    assert len((await hub.feed_timeline())["posts"]) == 1
    assert len([e for e in await all_events(hub) if e["kind"] == "feed_post"]) == 1
    with pytest.raises(HubError) as info:
        await hub.feed_post("ada", "different", client_request_id="req-1")
    assert info.value.code == "idempotency_conflict"


async def test_post_hooks_run_after_commit_for_new_posts_only(hub):
    seen: list[int] = []

    async def record(result):
        seen.append(result["post"]["id"])

    async def broken(result):
        raise RuntimeError("hook bug")

    hub.feed_post_hooks.extend([broken, record])
    first = pid(await hub.feed_post("ada", "hooked", client_request_id="k"))
    await hub.feed_post("ada", "hooked", client_request_id="k")
    assert seen == [first]


# ---- events and webhooks ----------------------------------------------------


async def test_feed_events_are_emitted_with_snippets(hub):
    long_body = "#start " + "z" * 300
    root = pid(await hub.feed_post("ada", long_body))
    await hub.feed_post("bob", "reply", parent_id=root)
    await hub.feed_repost("cy", root)
    await hub.feed_repost("cy", root)  # idempotent: no second event
    await hub.feed_follow("cy", "ada")
    await hub.feed_follow("cy", "ada")
    await hub.feed_unfollow("cy", "ada")
    events = await all_events(hub)
    kinds = [e["kind"] for e in events]
    assert kinds == ["feed_post", "feed_post", "feed_repost", "feed_follow", "feed_follow"]
    first = events[0]
    assert first["channel_id"] is None and first["entity_id"] == root
    assert first["payload"]["author"] == "ada" and first["payload"]["hashtags"] == ["start"]
    assert first["payload"]["body"] == long_body[:120]
    assert events[1]["payload"]["parent_id"] == root
    assert events[2]["payload"]["repost_of"] == root
    assert [e["payload"]["following"] for e in events[3:]] == [True, False]

    filtered = await hub.wait_for_events(None, 0, kinds="feed_follow", timeout=0)
    assert [e["kind"] for e in filtered["events"]] == ["feed_follow", "feed_follow"]
    for event in events:
        assert event_line(event)
        assert chat_payloads([event], "discord") and chat_payloads([event], "slack")


async def test_webhook_dispatch_survives_feed_events(hooked):
    client, _app, sink = hooked
    created = await client.post(
        "/api/v1/webhooks",
        json={"name": "everything", "direction": "out", "url": "http://127.0.0.1:9/x", "format": "discord"},
    )
    assert created.status_code == 201, created.text
    hook = created.json()["webhook"]
    root = await client.post("/api/v1/feed/posts", json={"author": "ada", "body": "hi #fleet"})
    assert root.status_code == 201, root.text
    post_id = root.json()["post"]["id"]
    await client.post(f"/api/v1/feed/posts/{post_id}/repost", json={"author": "bob"})
    await client.put("/api/v1/feed/follows/ada", json={"follower": "bob"})
    await client.post("/api/v1/messages", json={"channel": "fleet", "author": "ada", "text": "after"})

    assert await until(lambda: "after" in json.dumps(sink.bodies()))
    text = json.dumps(sink.bodies(), ensure_ascii=False)
    assert "posted on the feed" in text and "reposted" in text and "followed" in text
    fresh = (await client.get(f"/api/v1/webhooks/{hook['id']}")).json()["webhook"]
    assert fresh["enabled"] is True and fresh["failure_count"] == 0

    # A mention filter is about messages and tasks; feed events do not match it.
    event = {"id": 1, "kind": "feed_post", "channel": None, "payload": {"author": "ada"}}
    filtered = {"name": "m", "kinds": [], "channels": [], "mentions": ["ada"], "exclude_authors": []}
    assert matches(filtered, event) is False
    excluded = {**filtered, "mentions": [], "exclude_authors": ["ad*"]}
    assert matches(excluded, event) is False


# ---- REST and MCP -----------------------------------------------------------


async def test_rest_feed_requires_auth(authed):
    client, _ = authed
    for method, path in (
        ("GET", "/api/v1/feed/timeline"),
        ("POST", "/api/v1/feed/posts"),
        ("PUT", "/api/v1/feed/follows/ada"),
        ("GET", "/api/v1/feed/tags/trending"),
    ):
        response = await client.request(method, path, json={"author": "x", "body": "y"})
        assert response.status_code == 401, path
    ok = await client.get("/api/v1/feed/timeline", headers={"Authorization": "Bearer test-token"})
    assert ok.status_code == 200 and ok.json()["posts"] == []


async def test_rest_feed_round_trip(running):
    client, _ = running
    created = await client.post("/api/v1/feed/posts", json={"agent_name": "ada", "body": "hello #fleet"})
    assert created.status_code == 201, created.text
    root = created.json()["post"]["id"]
    reply = await client.post("/api/v1/feed/posts", json={"author": "bob", "body": "hi", "parent_id": root})
    reply_id = reply.json()["post"]["id"]

    thread = (await client.get(f"/api/v1/feed/posts/{reply_id}", params={"thread": 1})).json()
    assert thread["root_id"] == root and thread["posts"][0]["replies"][0]["id"] == reply_id
    assert (await client.get(f"/api/v1/feed/posts/{root}")).json()["post"]["reply_count"] == 1
    assert (await client.get("/api/v1/feed/posts/999")).status_code == 404

    repost = await client.post(f"/api/v1/feed/posts/{root}/repost", json={"author": "cy"})
    assert repost.status_code == 201
    assert (await client.post(f"/api/v1/feed/posts/{root}/repost", json={"author": "cy"})).status_code == 200

    assert (await client.put("/api/v1/feed/follows/ada", json={"follower": "cy"})).json()["following"] is True
    assert (await client.get("/api/v1/feed/follows/cy")).json()["following"] == ["ada"]
    assert (await client.get("/api/v1/feed/followers/ada")).json()["followers"] == ["cy"]
    mine = (await client.get("/api/v1/feed/timeline", params={"scope": "following", "handle": "cy"})).json()
    assert [p["id"] for p in mine["posts"]] == [root]
    tagged = (await client.get("/api/v1/feed/timeline", params={"scope": "hashtag", "tag": "fleet"})).json()
    assert [p["id"] for p in tagged["posts"]] == [root]
    paged = (await client.get("/api/v1/feed/timeline", params={"limit": 1})).json()
    assert paged["has_more"] is True and paged["next_cursor"]
    bad = await client.get("/api/v1/feed/timeline", params={"cursor": "garbage"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_error"
    trending = (await client.get("/api/v1/feed/tags/trending")).json()
    assert trending["tags"] == [{"tag": "fleet", "count": 1}]

    forbidden = await client.request("DELETE", f"/api/v1/feed/posts/{root}", json={"actor": "bob"})
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "forbidden"
    gone = await client.delete(f"/api/v1/feed/posts/{root}", params={"actor": "ada"})
    assert gone.status_code == 200 and gone.json()["post"]["deleted"] is True
    unfollowed = await client.delete("/api/v1/feed/follows/ada", params={"follower": "cy"})
    assert unfollowed.json()["following"] is False


async def test_rest_deep_thread_serializes_and_continues(running):
    client, _ = running
    parent = (await client.post("/api/v1/feed/posts", json={"author": "ada", "body": "deep"})).json()["post"]["id"]
    ids = [parent]
    for i in range(40):
        reply = await client.post("/api/v1/feed/posts", json={"author": "bob", "body": f"r{i}", "parent_id": parent})
        parent = reply.json()["post"]["id"]
        ids.append(parent)

    async def fetch(post_id):
        response = await client.get(f"/api/v1/feed/posts/{post_id}", params={"thread": 1})
        assert response.status_code == 200, response.text
        return response.json()

    assert sorted(await read_whole_thread(fetch, ids[0])) == ids
    bad = await client.get(f"/api/v1/feed/posts/{ids[0]}", params={"thread": 1, "cursor": "nope"})
    assert bad.status_code == 400


async def test_mcp_feed_tools_are_registered_and_serialize_deep_threads(hub):
    mcp_app.set_hub(hub)
    try:
        async with Client(mcp_app.build_mcp()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {
                "fleet_feed_post",
                "fleet_feed_reply",
                "fleet_feed_repost",
                "fleet_feed_timeline",
                "fleet_feed_thread",
                "fleet_feed_follow",
                "fleet_feed_unfollow",
            } <= names
            root = (await client.call_tool("fleet_feed_post", {"author": "ada", "body": "root #mcp"})).data
            parent = root["post"]["id"]
            ids = [parent]
            for i in range(60):
                reply = await client.call_tool(
                    "fleet_feed_reply", {"author": "bob", "parent_id": parent, "body": f"r{i}"}
                )
                parent = reply.data["post"]["id"]
                ids.append(parent)
            thread = (await client.call_tool("fleet_feed_thread", {"post_id": parent})).structured_content
            assert len(thread["ancestors"]) == 60 - feed.THREAD_CONTEXT
            assert thread["count"] == feed.THREAD_CONTEXT + 1

            async def fetch(post_id):
                return (await client.call_tool("fleet_feed_thread", {"post_id": post_id})).structured_content

            assert sorted(await read_whole_thread(fetch, ids[0])) == ids
            await client.call_tool("fleet_feed_follow", {"follower": "cy", "followee": "ada"})
            await client.call_tool("fleet_feed_repost", {"author": "cy", "post_id": root["post"]["id"]})
            page = (
                await client.call_tool("fleet_feed_timeline", {"scope": "following", "handle": "cy"})
            ).structured_content
            assert [p["author"] for p in page["posts"]] == ["ada"]
            unfollowed = (
                await client.call_tool("fleet_feed_unfollow", {"follower": "cy", "followee": "ada"})
            ).structured_content
            assert unfollowed["following"] is False
            error = (await client.call_tool("fleet_feed_thread", {"post_id": 9_999})).structured_content
            assert error["status"] == "error" and error["error"] == "not_found"
    finally:
        mcp_app.set_hub(None)


# ---- schema -----------------------------------------------------------------


async def test_v5_database_upgrades_to_the_feed_schema(tmp_path):
    path = tmp_path / "v5.sqlite"
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO messages (channel_id, author, text, created_at) VALUES (1, 'old', 'kept', 't')")
        for table in ("feed_hashtags", "feed_follows", "feed_posts"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version > 5")

    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        post = await hub.feed_post("ada", "after the upgrade #v8")
        assert post["post"]["hashtags"] == ["v8"]
    finally:
        await db.close()
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        kept = conn.execute("SELECT text FROM messages").fetchone()
    assert {"feed_posts", "feed_follows", "feed_hashtags"} <= tables
    assert {"idx_feed_posts_root", "idx_feed_hashtags_tag", "uq_feed_posts_repost"} <= indexes
    assert SCHEMA_VERSION >= 8 and versions == list(range(1, SCHEMA_VERSION + 1))
    assert kept == ("kept",)


async def test_author_scope_needs_author_or_handle(hub):
    with pytest.raises(HubError) as caught:
        await hub.feed_timeline(scope="author")
    assert caught.value.code == "validation_error"
    assert caught.value.message == "author scope needs author or handle"
    assert caught.value.status == 400
    assert caught.value.details == {"field": "author"}


async def test_revived_repost_gets_fresh_created_at(hub):
    from cat_fleet_chat import store

    root = pid(await hub.feed_post("ada", "original"))
    repost = pid(await hub.feed_repost("bob", root))

    async def created_at(conn):
        return (await store.get_feed_row(conn, repost))["created_at"]

    async def backdate(conn):
        await conn.execute("UPDATE feed_posts SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?", (repost,))

    await hub.feed_delete("bob", repost)
    await hub._txn(backdate)
    assert await hub.db.read(created_at) == "2000-01-01T00:00:00Z"
    await hub.feed_repost("bob", root)
    assert await hub.db.read(created_at) > "2000-01-01T00:00:00Z"


async def test_rest_feed_actor_fallback_and_missing_identity(running):
    client, _ = running
    post = (await client.post("/api/v1/feed/posts", json={"author": "ada", "body": "x"})).json()["post"]["id"]
    missing = await client.request("DELETE", f"/api/v1/feed/posts/{post}")
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "unauthorized"
    via_query = await client.request("DELETE", f"/api/v1/feed/posts/{post}", params={"actor": "ada"})
    assert via_query.status_code == 200
    via_agent_name = await client.request("DELETE", f"/api/v1/feed/posts/{post}", params={"agent_name": "ada"})
    assert via_agent_name.status_code == 200
    await client.put("/api/v1/feed/follows/ada", params={"follower": "cy"})
    assert (await client.get("/api/v1/feed/follows/cy")).json()["following"] == ["ada"]
    gone = await client.request("DELETE", "/api/v1/feed/follows/ada", params={"follower": "cy"})
    assert gone.json()["following"] is False


async def test_rest_feed_create_post_author_from_query_and_thread_flag_case(running):
    client, _ = running
    created = await client.post("/api/v1/feed/posts", params={"author": "ada"}, json={"body": "hello"})
    assert created.status_code == 201, created.text
    assert created.json()["post"]["author"] == "ada"
    root = created.json()["post"]["id"]
    thread = (await client.get(f"/api/v1/feed/posts/{root}", params={"thread": "TRUE"})).json()
    assert "posts" in thread and thread["root_id"] == root


async def test_rest_unexpected_error_is_json(running, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    _, app = running

    async def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(app.state.hub, "list_destinations", boom)
    monkeypatch.setattr(app.state.hub, "search", boom)
    monkeypatch.setattr(app.state.hub, "feed_timeline", boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in ("/api/v1/destinations", "/api/v1/search?q=x", "/api/v1/feed/timeline"):
            response = await client.get(path)
            assert response.status_code == 500, path
            assert response.headers["content-type"].startswith("application/json"), path
            body = response.json()["error"]
            assert body["code"] == "internal_error"
            assert "disk on fire" not in response.text


async def test_mcp_feed_tools_return_structured_error_on_unexpected_failure(hub, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("store down")

    mcp_app.set_hub(hub)
    try:
        monkeypatch.setattr(hub, "feed_post", boom)
        monkeypatch.setattr(hub, "feed_timeline", boom)
        for result in (
            await mcp_app.fleet_feed_post("ada", "hi"),
            await mcp_app.fleet_feed_reply("ada", 1, "hi"),
            await mcp_app.fleet_feed_timeline(),
            await mcp_app.fleet_feed_thread(1),
        ):
            assert result["status"] == "error" and result["error"] == "internal_error"
            assert result["http_status"] == 500
            assert "store down" not in result["message"]
        # HubError keeps its own envelope.
        bad = await mcp_app.fleet_feed_repost("ada", 999)
        assert bad["error"] == "not_found"
    finally:
        mcp_app.set_hub(None)
