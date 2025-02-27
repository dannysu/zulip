import time
from functools import wraps
from typing import Any, Callable, Dict, List, Set

from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _
from typing_extensions import Concatenate, ParamSpec

from zerver.lib.addressee import get_user_profiles_by_ids
from zerver.lib.exceptions import JsonableError, ResourceNotFoundError
from zerver.lib.message import normalize_body, truncate_topic
from zerver.lib.recipient_users import recipient_for_user_profiles
from zerver.lib.streams import access_stream_by_id
from zerver.lib.timestamp import timestamp_to_datetime
from zerver.lib.validator import (
    check_dict_only,
    check_float,
    check_int,
    check_list,
    check_required_string,
    check_string,
    check_string_in,
    check_union,
)
from zerver.models import Draft, UserProfile
from zerver.tornado.django_api import send_event

ParamT = ParamSpec("ParamT")
VALID_DRAFT_TYPES: Set[str] = {"", "private", "stream"}

# A validator to verify if the structure (syntax) of a dictionary
# meets the requirements to be a draft dictionary:
draft_dict_validator = check_dict_only(
    required_keys=[
        ("type", check_string_in(VALID_DRAFT_TYPES)),
        ("to", check_list(check_int)),  # The ID of the stream to send to, or a list of user IDs.
        ("topic", check_string),  # This string can simply be empty for private type messages.
        ("content", check_required_string),
    ],
    optional_keys=[
        ("timestamp", check_union([check_int, check_float])),  # A Unix timestamp.
    ],
)


def further_validated_draft_dict(
    draft_dict: Dict[str, Any], user_profile: UserProfile
) -> Dict[str, Any]:
    """Take a draft_dict that was already validated by draft_dict_validator then
    further sanitize, validate, and transform it. Ultimately return this "further
    validated" draft dict. It will have a slightly different set of keys the values
    for which can be used to directly create a Draft object."""

    content = normalize_body(draft_dict["content"])

    timestamp = draft_dict.get("timestamp", time.time())
    timestamp = round(timestamp, 6)
    if timestamp < 0:
        # While it's not exactly an invalid timestamp, it's not something
        # we want to allow either.
        raise JsonableError(_("Timestamp must not be negative."))
    last_edit_time = timestamp_to_datetime(timestamp)

    topic = ""
    recipient_id = None
    to = draft_dict["to"]
    if draft_dict["type"] == "stream":
        topic = truncate_topic(draft_dict["topic"])
        if "\0" in topic:
            raise JsonableError(_("Topic must not contain null bytes"))
        if len(to) != 1:
            raise JsonableError(_("Must specify exactly 1 stream ID for stream messages"))
        stream, sub = access_stream_by_id(user_profile, to[0])
        recipient_id = stream.recipient_id
    elif draft_dict["type"] == "private" and len(to) != 0:
        to_users = get_user_profiles_by_ids(set(to), user_profile.realm)
        try:
            recipient_id = recipient_for_user_profiles(to_users, False, None, user_profile).id
        except ValidationError as e:  # nocoverage
            raise JsonableError(e.messages[0])

    return {
        "recipient_id": recipient_id,
        "topic": topic,
        "content": content,
        "last_edit_time": last_edit_time,
    }


def draft_endpoint(
    view_func: Callable[Concatenate[HttpRequest, UserProfile, ParamT], HttpResponse]
) -> Callable[Concatenate[HttpRequest, UserProfile, ParamT], HttpResponse]:
    @wraps(view_func)
    def draft_view_func(
        request: HttpRequest,
        user_profile: UserProfile,
        /,
        *args: ParamT.args,
        **kwargs: ParamT.kwargs,
    ) -> HttpResponse:
        if not user_profile.enable_drafts_synchronization:
            raise JsonableError(_("User has disabled synchronizing drafts."))
        return view_func(request, user_profile, *args, **kwargs)

    return draft_view_func


def do_create_drafts(draft_dicts: List[Dict[str, Any]], user_profile: UserProfile) -> List[Draft]:
    """Create drafts in bulk for a given user based on the draft dicts. Since
    currently, the only place this method is being used (apart from tests) is from
    the create_draft view, we assume that the drafts_dicts are syntactically valid
    (i.e. they satisfy the draft_dict_validator)."""
    draft_objects = []
    for draft_dict in draft_dicts:
        valid_draft_dict = further_validated_draft_dict(draft_dict, user_profile)
        draft_objects.append(
            Draft(
                user_profile=user_profile,
                recipient_id=valid_draft_dict["recipient_id"],
                topic=valid_draft_dict["topic"],
                content=valid_draft_dict["content"],
                last_edit_time=valid_draft_dict["last_edit_time"],
            )
        )

    created_draft_objects = Draft.objects.bulk_create(draft_objects)

    event = {
        "type": "drafts",
        "op": "add",
        "drafts": [draft.to_dict() for draft in created_draft_objects],
    }
    send_event(user_profile.realm, event, [user_profile.id])

    return created_draft_objects


def do_edit_draft(draft_id: int, draft_dict: Dict[str, Any], user_profile: UserProfile) -> None:
    """Edit/update a single draft for a given user. Since the only place this method is being
    used from (apart from tests) is the edit_draft view, we assume that the drafts_dict is
    syntactically valid (i.e. it satisfies the draft_dict_validator)."""
    try:
        draft_object = Draft.objects.get(id=draft_id, user_profile=user_profile)
    except Draft.DoesNotExist:
        raise ResourceNotFoundError(_("Draft does not exist"))
    valid_draft_dict = further_validated_draft_dict(draft_dict, user_profile)
    draft_object.content = valid_draft_dict["content"]
    draft_object.topic = valid_draft_dict["topic"]
    draft_object.recipient_id = valid_draft_dict["recipient_id"]
    draft_object.last_edit_time = valid_draft_dict["last_edit_time"]
    draft_object.save()

    event = {"type": "drafts", "op": "update", "draft": draft_object.to_dict()}
    send_event(user_profile.realm, event, [user_profile.id])


def do_delete_draft(draft_id: int, user_profile: UserProfile) -> None:
    """Delete a draft belonging to a particular user."""
    try:
        draft_object = Draft.objects.get(id=draft_id, user_profile=user_profile)
    except Draft.DoesNotExist:
        raise ResourceNotFoundError(_("Draft does not exist"))

    draft_id = draft_object.id
    draft_object.delete()

    event = {"type": "drafts", "op": "remove", "draft_id": draft_id}
    send_event(user_profile.realm, event, [user_profile.id])
