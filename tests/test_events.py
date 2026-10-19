"""Calendar event tools (CALENDAR-CONTRACT.md, 2026-10-19), over the fake transport."""
from __future__ import annotations

import json

import pytest
from meshbook import config as mb_config

from meshbook_mcp import server
from tests._fakes import A1, C1, E1, MESH, MESH2, U1, U2, ok, ok_list

CAL = "/api/calendar-events"

EVENT = {
    "id": E1, "title": "Standup", "startAt": "2026-10-20T22:00:00+00:00",
    "endAt": "2026-10-20T22:15:00+00:00", "isAllDay": False, "eventType": "Meeting",
    "location": None, "meetingUrl": None, "ownerUserId": U1, "ownerName": "Chris",
    "linkedEntityType": None, "linkedEntityId": None, "colorHex": None,
    "recurrence": {"id": "r-1", "eventId": E1, "frequency": "Weekly", "intervalN": 2,
                   "untilDate": "2026-12-31", "countN": None, "byWeekday": ["TU"],
                   "byMonthDay": None},
    "attendees": [
        {"id": A1, "eventId": E1, "userId": U2, "contactId": None, "externalEmail": None,
         "response": "Accepted", "isOptional": False, "isOrganiser": False,
         "userDisplayName": "Wren"},
    ],
}


def _users(api):
    api.add("GET", "/api/users", ok_list([
        {"id": U1, "username": "chris", "displayName": "Chris"},
        {"id": U2, "username": "wren", "displayName": "Wren"},
    ]))


def test_list_events_window_expands_by_default(api):
    occ = dict(EVENT, occurrenceStart="2026-11-03T22:00:00+00:00",
               occurrenceEnd="2026-11-03T22:15:00+00:00", seriesId=E1)
    api.add("GET", CAL, ok_list([occ], total=1, limit=50, truncated=False))
    out = server.list_events("2026-11-01T00:00:00+11:00", "2026-11-08T00:00:00+11:00",
                             tz="Australia/Sydney")
    q = api.query()
    assert q["from"] == "2026-11-01T00:00:00+11:00" and q["to"] == "2026-11-08T00:00:00+11:00"
    assert q["expand"] == "true" and q["tz"] == "Australia/Sydney" and q["limit"] == "50"
    assert out["expanded"] is True
    assert out["count"] == 1 and out["total"] == 1 and out["truncated"] is False
    item = out["items"][0]
    assert item["start"] == "2026-11-03T22:00:00+00:00"          # the occurrence, not the series
    assert item["seriesId"] == E1 and item["seriesStart"] == EVENT["startAt"]
    assert item["repeats"] == "Weekly every 2 on TU until 2026-12-31"
    assert item["attendees"][0]["name"] == "Wren"


def test_list_events_without_window_does_not_expand(api):
    api.add("GET", CAL, ok_list([EVENT]))
    out = server.list_events()
    assert "expand" not in api.query() and out["expanded"] is False


def test_list_events_reports_expansion_cap(api):
    api.add("GET", CAL, ok_list([EVENT] * 3, total=500, limit=3, truncated=True))
    out = server.list_events("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z", limit=3)
    assert out["truncated"] is True and "500" in out["note"]
    assert out["nextOffset"] == 3


@pytest.mark.parametrize("bad", ["2026-10-20T09:00:00", "2026-10-20 09:00", "2026-10-20"])
def test_naive_datetimes_are_refused_before_the_wire(api, bad):
    with pytest.raises(server.MeshbookError) as exc:
        server.list_events(bad, "2026-10-21T00:00:00+11:00")
    assert exc.value.code == "naive_datetime"
    assert "UTC offset" in exc.value.message and "+11:00" in exc.value.message
    assert not api.calls("GET", CAL)
    with pytest.raises(server.MeshbookError) as exc:
        server.create_event("x", "2026-10-20T09:00:00+11:00", bad)
    assert exc.value.code == "naive_datetime"
    assert not api.calls("POST", CAL)


def test_garbage_datetime_is_bad_datetime(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.list_events("next tuesday", "2026-10-21T00:00:00+11:00")
    assert exc.value.code == "bad_datetime"


def test_create_event_full_body_with_dedupe(api):
    _users(api)
    api.add("GET", CAL, ok_list([]))                     # duplicate check: none
    api.add("POST", CAL, ok(EVENT))
    out = server.create_event(
        "Standup", "2026-10-21T09:00:00+11:00", "2026-10-21T09:15:00+11:00",
        event_type="meeting", location="Kitchen", attendees=["user:@wren"],
        repeat="fortnightly", repeat_until="2026-12-31", repeat_weekdays=["tu"],
    )
    dedupe = api.calls("GET", CAL)[0]
    assert api.query(dedupe)["from"] == "2026-10-21T09:00:00+11:00"
    body = api.body(api.calls("POST", CAL)[0])
    assert body["title"] == "Standup"
    assert body["startAt"] == "2026-10-21T09:00:00+11:00"
    assert body["endAt"] == "2026-10-21T09:15:00+11:00"
    assert body["eventType"] == "Meeting"
    assert body["location"] == "Kitchen"
    assert body["attendees"] == [{"userId": U2}]
    assert body["recurrence"] == {"frequency": "Weekly", "intervalN": 2,
                                  "untilDate": "2026-12-31", "byWeekday": ["TU"]}
    assert out["id"] == E1 and out["repeats"].startswith("Weekly every 2")


def test_create_event_duplicate_guard(api):
    api.add("GET", CAL, ok_list([dict(EVENT, title="  standup ",
                                      startAt="2026-10-20T22:00:00+00:00")]))
    with pytest.raises(server.MeshbookError) as exc:
        server.create_event("Standup", "2026-10-21T09:00:00+11:00", "2026-10-21T09:15:00+11:00")
    assert exc.value.code == "duplicate_event"
    assert "force=true" in exc.value.message and "dedupe" not in exc.value.message
    assert not api.calls("POST", CAL)


def test_create_event_force_skips_the_guard(api):
    api.add("POST", CAL, ok(EVENT))
    server.create_event("Standup", "2026-10-21T09:00:00+11:00", "2026-10-21T09:15:00+11:00",
                        force=True)
    assert not api.calls("GET", CAL)
    assert len(api.calls("POST", CAL)) == 1


def test_create_event_repeat_parts_need_repeat(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.create_event("x", "2026-10-21T09:00:00+11:00", "2026-10-21T10:00:00+11:00",
                            repeat_until="2026-12-31", force=True)
    assert exc.value.code == "bad_argument"


def test_create_event_owner_me(api):
    api.add("GET", "/api/me", ok({"user": {"id": U1, "username": "chris"}}))
    api.add("POST", CAL, ok(EVENT))
    server.create_event("x", "2026-10-21T09:00:00+11:00", "2026-10-21T10:00:00+11:00",
                        owner="me", force=True)
    assert api.body()["ownerUserId"] == U1


def test_update_event_sends_only_what_changed(api):
    api.add("PATCH", f"{CAL}/{E1}", ok(EVENT))
    server.update_event(E1, title="Retro", start="2026-10-22T10:00:00+11:00",
                        end="2026-10-22T11:00:00+11:00", clear=["location", "link"])
    assert api.body() == {"title": "Retro", "startAt": "2026-10-22T10:00:00+11:00",
                          "endAt": "2026-10-22T11:00:00+11:00", "location": None,
                          "linkedEntityType": None, "linkedEntityId": None}


def test_update_event_attendees_replace_and_clear(api):
    api.add("PATCH", f"{CAL}/{E1}", ok(EVENT))
    api.add("GET", "/api/contacts", ok_list([{"id": C1, "displayName": "Jane Doe"}]))
    server.update_event(E1, attendees=["contact:Jane Doe", "email:guest@example.com"])
    assert api.body() == {"attendees": [{"contactId": C1}, {"externalEmail": "guest@example.com"}]}
    server.update_event(E1, clear=["attendees"])
    assert api.body() == {"attendees": []}


def test_update_event_refuses_value_and_clear(api):
    with pytest.raises(server.MeshbookError, match="not both"):
        server.update_event(E1, location="Here", clear=["location"])
    with pytest.raises(server.MeshbookError, match="Clearable"):
        server.update_event(E1, clear=["title"])
    with pytest.raises(server.MeshbookError, match="Nothing to update"):
        server.update_event(E1)


def test_delete_event_needs_an_id(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.delete_event("Standup")
    assert exc.value.code == "bad_argument" and "list_events" in exc.value.message
    api.add("DELETE", f"{CAL}/{E1}", ok({"id": E1, "deleted": True}))
    assert server.delete_event(E1) == {"id": E1, "deleted": True}


def test_set_and_clear_recurrence(api):
    rule = {"id": "r-1", "eventId": E1, "frequency": "Monthly", "intervalN": 1,
            "untilDate": None, "countN": 6, "byWeekday": ["3TH"], "byMonthDay": None}
    api.add("PUT", f"{CAL}/{E1}/recurrence", ok(rule))
    out = server.set_event_recurrence(E1, "MONTHLY", count=6, weekdays=["3th"])
    assert api.body() == {"frequency": "Monthly", "intervalN": 1, "untilDate": None,
                          "countN": 6, "byWeekday": ["3TH"]}
    assert out["repeats"] == "Monthly on 3TH, 6 times"
    api.add("DELETE", f"{CAL}/{E1}/recurrence", ok({"id": "r-1", "eventId": E1, "deleted": True}))
    assert server.clear_event_recurrence(E1)["deleted"] is True


def test_set_recurrence_until_must_be_a_date(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.set_event_recurrence(E1, "weekly", until="2026-12-31T00:00:00Z")
    assert exc.value.code == "bad_date"


def test_attendee_add_and_remove(api):
    api.add("GET", "/api/contacts", ok_list([{"id": C1, "displayName": "Jane Doe",
                                              "primaryEmail": "jane@example.com"}]))
    api.add("POST", f"{CAL}/{E1}/attendees", ok({
        "id": A1, "eventId": E1, "contactId": C1, "contactDisplayName": "Jane Doe",
        "response": "NoResponse", "isOptional": True, "isOrganiser": False}))
    out = server.add_event_attendee(E1, contact="jane@example.com", optional=True)
    assert api.body() == {"contactId": C1, "isOptional": True}
    assert out == {"id": A1, "kind": "contact", "name": "Jane Doe", "response": "NoResponse",
                   "organiser": False, "optional": True}
    api.add("DELETE", f"{CAL}/{E1}/attendees/{A1}", ok({"id": A1, "deleted": True}))
    assert server.remove_event_attendee(E1, A1)["deleted"] is True


def test_add_attendee_needs_exactly_one_ref(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.add_event_attendee(E1, user=U1, email="x@example.com")
    assert exc.value.code == "bad_argument"


def test_get_event_full(api):
    api.add("GET", f"{CAL}/{E1}", ok(dict(EVENT, description="Daily sync", colorHex="#00ff00")))
    out = server.get_event(E1)
    assert out["description"] == "Daily sync" and out["color"] == "#00ff00"
    assert out["recurrence"]["frequency"] == "Weekly"
    assert out["attendees"][0]["id"] == A1


def test_per_call_mesh_overrides_without_persisting(api):
    api.add("GET", CAL, ok_list([]))
    server.list_events(mesh="second mesh")
    assert api.last.get_header("X-active-mesh-id") == MESH2
    assert mb_config.load_config()["active_mesh_id"] == MESH
    server.list_events()
    assert api.last.get_header("X-active-mesh-id") == MESH


def test_event_outputs_are_json(api):
    api.add("GET", CAL, ok_list([EVENT]))
    json.dumps(server.list_events())


# ─── 0.8.2: end defaults like the CLI (field note 7) ───────────────────


def _tool_schema(name):
    import asyncio

    from fastmcp import Client

    async def go():
        async with Client(server.mcp) as c:
            return {t.name: t for t in await c.list_tools()}[name]

    return asyncio.run(go())


def test_create_event_end_defaults_to_an_hour_after_start(api):
    api.add("POST", CAL, ok(EVENT))
    server.create_event("Standup", "2026-10-21T09:00:00+11:00", force=True)
    body = api.body()
    assert body["startAt"] == "2026-10-21T09:00:00+11:00"
    assert body["endAt"] == "2026-10-21T10:00:00+11:00"


def test_create_event_all_day_end_defaults_to_a_day_later(api):
    api.add("POST", CAL, ok(EVENT))
    server.create_event("Offsite", "2026-10-21T00:00:00+11:00", all_day=True, force=True)
    body = api.body()
    assert body["isAllDay"] is True
    assert body["endAt"] == "2026-10-22T00:00:00+11:00"


def test_create_event_keeps_an_explicit_end_and_positional_callers(api):
    api.add("POST", CAL, ok(EVENT))
    # 0.8.x callers pass end third, positionally: still works.
    server.create_event("Standup", "2026-10-21T09:00:00+11:00", "2026-10-21T09:15:00+11:00",
                        force=True)
    assert api.body()["endAt"] == "2026-10-21T09:15:00+11:00"


def test_create_event_schema_no_longer_requires_end():
    schema = _tool_schema("create_event").inputSchema
    assert set(schema["required"]) == {"title", "start"}


@pytest.mark.parametrize("name", ["create_event", "update_event", "list_events", "get_event"])
def test_event_tools_document_the_api_field_names(name):
    """Inputs and answers say start/end/type; the API (and meshbook-cli
    --json) says startAt/endAt/eventType. The descriptions name the mapping."""
    text = _tool_schema(name).description
    for api_key in ("startAt", "endAt", "eventType"):
        assert api_key in text, (name, api_key)


# ─── 0.8.2: a series time zone (field note 1) ──────────────────────────

SYD = "Australia/Sydney"
RULE_TZ = dict(EVENT["recurrence"], tzid=SYD)


def test_create_event_tz_becomes_the_series_tzid(api):
    api.add("POST", CAL, ok(dict(EVENT, recurrence=RULE_TZ)))
    out = server.create_event("Standup", "2027-03-23T10:00:00+11:00", repeat="weekly",
                              tz=SYD, force=True)
    assert api.body()["recurrence"] == {"frequency": "Weekly", "intervalN": 1,
                                        "untilDate": None, "tzid": SYD}
    assert out["recurrence"]["tzid"] == SYD and out["repeats"].endswith(f"in {SYD}")
    assert "note" not in out


def test_create_event_series_without_tz_says_dst_will_shift_it(api):
    api.add("POST", CAL, ok(EVENT))                      # the stored rule has no tzid
    out = server.create_event("Standup", "2027-03-23T10:00:00+11:00", repeat="weekly",
                              force=True)
    assert "tzid" not in api.body()["recurrence"]
    note = out["note"]
    assert "no time zone" in note and "daylight saving" in note and "tz=" in note


def test_create_event_tz_the_server_did_not_keep_is_reported(api):
    """A server from before the tzid change ignores the key (extra=ignore)."""
    api.add("POST", CAL, ok(EVENT))
    out = server.create_event("Standup", "2027-03-23T10:00:00+11:00", repeat="weekly",
                              tz=SYD, force=True)
    assert api.body()["recurrence"]["tzid"] == SYD
    assert "did not store" in out["note"]


def test_create_event_single_event_gets_no_zone_note(api):
    api.add("POST", CAL, ok(dict(EVENT, recurrence=None)))
    out = server.create_event("Lunch", "2027-03-23T12:00:00+11:00", tz=SYD, force=True)
    assert "recurrence" not in api.body() and "note" not in out


def test_create_event_refuses_a_malformed_zone_before_the_wire(api):
    with pytest.raises(server.MeshbookError) as exc:
        server.create_event("Standup", "2027-03-23T10:00:00+11:00", repeat="weekly",
                            tz="not a zone!", force=True)
    assert exc.value.code == "bad_argument"
    assert not api.calls("POST", CAL)


def test_set_event_recurrence_tz(api):
    api.add("PUT", f"{CAL}/{E1}/recurrence", ok(dict(RULE_TZ, frequency="Weekly")))
    out = server.set_event_recurrence(E1, "weekly", tz=SYD)
    assert api.body()["tzid"] == SYD
    assert out["tz"] == SYD and "note" not in out


def test_set_event_recurrence_without_tz_says_so(api):
    api.add("PUT", f"{CAL}/{E1}/recurrence", ok(EVENT["recurrence"]))
    out = server.set_event_recurrence(E1, "weekly")
    assert "tzid" not in api.body()          # absent keeps whatever zone is stored
    assert out["tz"] is None and "no time zone" in out["note"]


def test_update_event_tz_resends_the_series_rule_with_its_zone(api):
    api.add("GET", f"{CAL}/{E1}", ok(EVENT))
    api.add("PATCH", f"{CAL}/{E1}", ok(dict(EVENT, recurrence=RULE_TZ)))
    out = server.update_event(E1, tz=SYD)
    assert api.body() == {"recurrence": {
        "frequency": "Weekly", "intervalN": 2, "untilDate": "2026-12-31", "countN": None,
        "byWeekday": ["TU"], "byMonthDay": None, "tzid": SYD}}
    assert out["recurrence"]["tzid"] == SYD and "note" not in out


def test_update_event_tz_on_a_single_event(api):
    single = dict(EVENT, recurrence=None)
    api.add("GET", f"{CAL}/{E1}", ok(single))
    with pytest.raises(server.MeshbookError) as exc:
        server.update_event(E1, tz=SYD)
    assert exc.value.code == "bad_argument" and "does not repeat" in exc.value.message
    assert not api.calls("PATCH", f"{CAL}/{E1}")
    api.add("PATCH", f"{CAL}/{E1}", ok(dict(single, title="Retro")))
    out = server.update_event(E1, title="Retro", tz=SYD)
    assert api.body() == {"title": "Retro"}
    assert "does not repeat" in out["note"]


def test_update_event_on_a_zoneless_series_says_so(api):
    api.add("PATCH", f"{CAL}/{E1}", ok(EVENT))
    out = server.update_event(E1, title="Retro")
    assert "no time zone" in out["note"]
    assert not api.calls("GET", f"{CAL}/{E1}")        # no tz, no start: no extra read
