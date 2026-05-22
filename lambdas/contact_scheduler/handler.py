"""Contact Scheduler Lambda for AWS Ground Station.

Queries ListContacts for AVAILABLE slots in the next 48 hours,
selects the pass with maximum elevation on weekdays (Mon-Fri),
calls ReserveContact to schedule it, and publishes to SNS if
no suitable pass is found.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

groundstation_client = boto3.client("groundstation")
sns_client = boto3.client("sns")


def handler(event, context):
    """Lambda entry point for contact scheduling."""
    mission_profile_arn = os.environ["MISSION_PROFILE_ARN"]
    satellite_arn = os.environ["SATELLITE_ARN"]
    sns_topic_arn = os.environ["SNS_TOPIC_ARN"]
    min_elevation = float(os.environ.get("MINIMUM_ELEVATION_DEGREES", "10"))

    logger.info(
        json.dumps(
            {
                "action": "schedule_contact_start",
                "mission_profile_arn": mission_profile_arn,
                "satellite_arn": satellite_arn,
                "min_elevation": min_elevation,
            }
        )
    )

    now = datetime.now(timezone.utc)
    end_time = now + timedelta(hours=48)

    try:
        available_contacts = list_available_contacts(
            mission_profile_arn, satellite_arn, now, end_time
        )
    except ClientError as e:
        logger.error(
            json.dumps(
                {
                    "action": "list_contacts_error",
                    "error": str(e),
                }
            )
        )
        raise

    weekday_contacts = filter_weekday_contacts(available_contacts)

    logger.info(
        json.dumps(
            {
                "action": "contacts_filtered",
                "total_available": len(available_contacts),
                "weekday_contacts": len(weekday_contacts),
            }
        )
    )

    eligible_contacts = [
        c
        for c in weekday_contacts
        if c.get("maximumElevation", {}).get("value", 0) >= min_elevation
    ]

    if not eligible_contacts:
        message = (
            f"No suitable pass found for satellite {satellite_arn} "
            f"in next 48 hours (min elevation: {min_elevation} degrees, "
            f"weekdays only)."
        )
        logger.warning(json.dumps({"action": "no_suitable_pass", "message": message}))
        publish_no_pass_notification(sns_topic_arn, message)
        return {
            "statusCode": 200,
            "body": json.dumps({"scheduled": False, "reason": "no_suitable_pass"}),
        }

    best_contact = select_best_contact(eligible_contacts)

    logger.info(
        json.dumps(
            {
                "action": "best_contact_selected",
                "start_time": best_contact["startTime"].isoformat(),
                "end_time": best_contact["endTime"].isoformat(),
                "max_elevation": best_contact.get("maximumElevation", {}).get(
                    "value", 0
                ),
                "ground_station_id": best_contact.get("groundStationId", ""),
            }
        )
    )

    try:
        reservation = reserve_contact(
            mission_profile_arn,
            satellite_arn,
            best_contact["startTime"],
            best_contact["endTime"],
            best_contact.get("groundStationId", ""),
        )
    except ClientError as e:
        logger.error(
            json.dumps(
                {
                    "action": "reserve_contact_error",
                    "error": str(e),
                }
            )
        )
        raise

    logger.info(
        json.dumps(
            {
                "action": "contact_reserved",
                "contact_id": reservation.get("contactId", ""),
            }
        )
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "scheduled": True,
                "contact_id": reservation.get("contactId", ""),
                "start_time": best_contact["startTime"].isoformat(),
                "end_time": best_contact["endTime"].isoformat(),
            }
        ),
    }


def list_available_contacts(mission_profile_arn, satellite_arn, start_time, end_time):
    """Query Ground Station for available contacts in the time window."""
    contacts = []
    paginator = groundstation_client.get_paginator("list_contacts")

    page_iterator = paginator.paginate(
        missionProfileArn=mission_profile_arn,
        satelliteArn=satellite_arn,
        startTime=start_time,
        endTime=end_time,
        statusList=["AVAILABLE"],
    )

    for page in page_iterator:
        contacts.extend(page.get("contactList", []))

    return contacts


def filter_weekday_contacts(contacts):
    """Filter contacts to only include those on weekdays (Mon-Fri)."""
    weekday_contacts = []
    for contact in contacts:
        start_time = contact.get("startTime")
        if start_time and start_time.weekday() < 5:
            weekday_contacts.append(contact)
    return weekday_contacts


def select_best_contact(contacts):
    """Select the contact with the highest maximum elevation angle."""
    return max(
        contacts,
        key=lambda c: c.get("maximumElevation", {}).get("value", 0),
    )


def reserve_contact(
    mission_profile_arn, satellite_arn, start_time, end_time, ground_station_id
):
    """Reserve a contact slot with Ground Station."""
    response = groundstation_client.reserve_contact(
        missionProfileArn=mission_profile_arn,
        satelliteArn=satellite_arn,
        startTime=start_time,
        endTime=end_time,
        groundStation=ground_station_id,
    )
    return response


def publish_no_pass_notification(sns_topic_arn, message):
    """Publish notification when no suitable pass is found."""
    try:
        sns_client.publish(
            TopicArn=sns_topic_arn,
            Subject="Ground Station - No Suitable Pass Found",
            Message=message,
        )
    except ClientError as e:
        logger.error(
            json.dumps(
                {
                    "action": "sns_publish_error",
                    "error": str(e),
                }
            )
        )
