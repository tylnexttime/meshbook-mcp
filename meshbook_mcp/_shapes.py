"""Output trimming: keep tool results LLM-sized.

Each function takes the API's camelCase dict (as the SDK returns it) and
keeps what a model needs to act on. ``full=True`` variants are for the
``get_*`` tools.
"""
from __future__ import annotations

from typing import Any


def _ts(value: Any) -> str:
    return (str(value or ""))[:19].replace("T", " ")


def mesh_out(m: dict[str, Any], active_id: str | None) -> dict[str, Any]:
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "type": m.get("meshType") or m.get("type"),
        "myRole": m.get("memberRole"),
        "active": bool(active_id) and m.get("id") == active_id,
    }


def contact_out(c: dict[str, Any], full: bool = False) -> dict[str, Any]:
    out = {
        "id": c.get("id"),
        "name": c.get("displayName"),
        "email": c.get("primaryEmail"),
        "company": c.get("primaryCompanyName") or c.get("companyName"),
        "phone": c.get("primaryPhone"),
        "role": c.get("jobTitle"),
    }
    if full:
        out.update(
            firstName=c.get("firstName"),
            lastName=c.get("lastName"),
            title=c.get("title"),
            notes=c.get("description"),
            companyId=c.get("primaryCompanyId"),
            customFields=c.get("customFields"),
            createdAt=c.get("createdAt"),
            updatedAt=c.get("updatedAt"),
        )
    if "tags" in c:
        out["tags"] = tag_names(c["tags"])
    return out


def company_out(c: dict[str, Any], full: bool = False) -> dict[str, Any]:
    out = {
        "id": c.get("id"),
        "name": c.get("name"),
        "website": c.get("website"),
        "industry": c.get("industry"),
        "email": c.get("email"),
        "phone": c.get("phone"),
    }
    if full:
        out.update(
            legalName=c.get("legalName"),
            abn=c.get("abn"),
            companySize=c.get("companySize"),
            description=c.get("description"),
            country=c.get("country"),
            customFields=c.get("customFields"),
            createdAt=c.get("createdAt"),
            updatedAt=c.get("updatedAt"),
        )
    return out


def lead_out(ld: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ld.get("id"),
        "title": ld.get("title"),
        "value": ld.get("valueAmount"),
        "currency": ld.get("currency"),
        "stage": ld.get("stageName") or ld.get("stageId"),
        "contact": ld.get("contactName") or ld.get("contactId"),
    }


def task_out(t: dict[str, Any], full: bool = False) -> dict[str, Any]:
    out = {
        "id": t.get("id"),
        "title": t.get("title"),
        "status": t.get("status"),
        "priority": t.get("priority"),
        "dueDate": t.get("dueDate"),
        "dueAt": t.get("dueAt"),
        "project": t.get("projectName") or t.get("projectId"),
    }
    if t.get("parentTaskId"):
        out["parentTaskId"] = t.get("parentTaskId")
    if full:
        out.update(
            projectId=t.get("projectId"),
            description=t.get("description"),
            assigneeUserId=t.get("assigneeUserId"),
            lead=t.get("leadName") or t.get("leadUserId"),
            assignees=t.get("assignees"),
            subtasks=[
                {"id": s.get("id"), "title": s.get("title"), "status": s.get("status")}
                for s in (t.get("subtasks") or [])
                if isinstance(s, dict)
            ],
            createdAt=t.get("createdAt"),
            updatedAt=t.get("updatedAt"),
        )
    if "tags" in t:
        out["tags"] = tag_names(t["tags"])
    return out


def project_out(p: dict[str, Any], full: bool = False) -> dict[str, Any]:
    out = {
        "id": p.get("id"),
        "name": p.get("name"),
        "code": p.get("code"),
        "status": p.get("status"),
        "progressPct": p.get("progressPct"),
        "targetEndDate": p.get("targetEndDate"),
    }
    if full:
        out.update(
            description=p.get("description"),
            ownerUserId=p.get("ownerUserId"),
            startDate=p.get("startDate"),
            members=[
                {
                    "userId": m.get("userId") or m.get("id"),
                    "name": m.get("displayName"),
                    "role": m.get("roleOnProject") or m.get("role"),
                }
                for m in (p.get("members") or [])
                if isinstance(m, dict)
            ],
            createdAt=p.get("createdAt"),
            updatedAt=p.get("updatedAt"),
        )
    return out


def project_member_out(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "projectId": m.get("projectId"),
        "userId": m.get("userId"),
        "name": m.get("displayName"),
        "role": m.get("roleOnProject"),
        "joinedAt": m.get("joinedAt"),
        "created": m.get("created"),
    }


def tag_names(tags: Any) -> list[Any]:
    out = []
    for t in tags or []:
        out.append(t.get("name") if isinstance(t, dict) else t)
    return out


def message_out(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m.get("id"),
        "author": (m.get("author") or {}).get("displayName"),
        "at": _ts(m.get("createdAt")),
        "body": m.get("bodyMd"),
        "replyTo": m.get("parentMessageId"),
    }


def notification_out(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": n.get("id"),
        "kind": n.get("kind"),
        "summary": n.get("summary"),
        "at": _ts(n.get("createdAt")),
    }


def export_out(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": e.get("id"),
        "status": e.get("status"),
        "createdAt": e.get("createdAt"),
        "expiresAt": e.get("expiresAt"),
        "byteSize": e.get("byteSize"),
        "error": e.get("error"),
    }


def channel_out(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "channelType": c.get("channelType"),
        "isPrivate": bool(c.get("isPrivate")),
        "topic": c.get("topic"),
        "unread": c.get("unread", c.get("unreadCount")),
    }


def member_out(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": m.get("username"),
        "displayName": m.get("displayName"),
        "role": m.get("role"),
        "identityType": m.get("identityType"),
        "canChat": m.get("canChat"),
        "joinedAt": m.get("joinedAt"),
        "userId": m.get("userId"),
    }


def search_hit_out(h: dict[str, Any]) -> dict[str, Any]:
    place = (
        ("DM" if h.get("channelType") == "dm" else "#" + (h.get("channelName") or "?"))
        if h.get("channelId")
        else ("feed" if h.get("entityType") == "mesh" else h.get("entityType"))
    )
    return {
        "id": h.get("id"),
        "place": place,
        "author": h.get("authorName"),
        "preview": h.get("preview"),
        "createdAt": h.get("createdAt"),
        "matchedArms": h.get("matchedArms"),
    }


# ─── calendar ──────────────────────────────────────────────────────────


def recurrence_text(r: dict[str, Any] | None) -> str | None:
    """'Weekly every 2 on MO,WE until 2026-12-31 in Australia/Sydney' — readable
    at a glance (the zone only when the series has one)."""
    if not r:
        return None
    text = str(r.get("frequency") or "?")
    n = r.get("intervalN") or 1
    if n and n != 1:
        text += f" every {n}"
    if r.get("byWeekday"):
        text += " on " + ",".join(str(d) for d in r["byWeekday"])
    if r.get("byMonthDay"):
        text += " on day " + ",".join(str(d) for d in r["byMonthDay"])
    if r.get("untilDate"):
        text += f" until {r['untilDate']}"
    elif r.get("countN"):
        text += f", {r['countN']} times"
    if r.get("tzid"):
        text += f" in {r['tzid']}"
    return text


def attendee_out(a: dict[str, Any]) -> dict[str, Any]:
    if a.get("userId"):
        kind, name = "user", a.get("userDisplayName")
    elif a.get("contactId"):
        kind, name = "contact", a.get("contactDisplayName") or a.get("contactPrimaryEmail")
    else:
        kind, name = "external", a.get("externalName") or a.get("externalEmail")
    return {
        "id": a.get("id"),
        "kind": kind,
        "name": name,
        "response": a.get("response"),
        "organiser": bool(a.get("isOrganiser")),
        "optional": bool(a.get("isOptional")),
    }


def event_out(ev: dict[str, Any], full: bool = False) -> dict[str, Any]:
    occ = "occurrenceStart" in ev
    out: dict[str, Any] = {
        "id": ev.get("id"),
        "title": ev.get("title"),
        "start": ev.get("occurrenceStart") if occ else ev.get("startAt"),
        "end": ev.get("occurrenceEnd") if occ else ev.get("endAt"),
        "allDay": bool(ev.get("isAllDay")),
        "type": ev.get("eventType"),
        "location": ev.get("location"),
        "meetingUrl": ev.get("meetingUrl"),
        "owner": ev.get("ownerName") or ev.get("ownerUserId"),
    }
    if occ and ev.get("seriesId"):
        out["seriesId"] = ev.get("seriesId")
        out["seriesStart"] = ev.get("startAt")
    if ev.get("linkedEntityType"):
        out["linked"] = {
            "type": ev.get("linkedEntityType"),
            "id": ev.get("linkedEntityId"),
            "name": ev.get("linkedEntityName"),
        }
    out["repeats"] = recurrence_text(ev.get("recurrence"))
    out["attendees"] = [attendee_out(a) for a in (ev.get("attendees") or []) if isinstance(a, dict)]
    if full:
        out.update(
            description=ev.get("description"),
            color=ev.get("colorHex"),
            customFields=ev.get("customFields"),
            recurrence=ev.get("recurrence"),
            ownerUserId=ev.get("ownerUserId"),
            createdAt=ev.get("createdAt"),
            updatedAt=ev.get("updatedAt"),
        )
    return out
