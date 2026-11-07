from datetime import datetime


WEIGHTS = {
    "quantity": 0.25,
    "compatibility": 0.20,
    "proximity": 0.20,
    "timing": 0.15,
    "urgency": 0.10,
    "fairness": 0.10,
}


def csv_values(value):
    return {part.strip().casefold() for part in (value or "").split(",") if part.strip()}


def parse_time(value):
    return datetime.fromisoformat(value)


def score_match(db, donation, food_request):
    if donation["status"] not in ("available", "partially_allocated"):
        return None
    if not donation["safety_confirmed"]:
        return None
    if food_request["status"] not in ("open", "partially_fulfilled"):
        return None
    if donation["category"].casefold() != food_request["category"].casefold():
        return None
    if donation["quantity_unit"] != food_request["quantity_unit"]:
        return None
    if (
        food_request["storage_preference"] != "any"
        and donation["storage_method"] != food_request["storage_preference"]
    ):
        return None
    if donation["remaining_quantity"] <= 0 or food_request["remaining_quantity"] <= 0:
        return None
    prior_allocation = db.execute(
        """SELECT status FROM allocations
           WHERE donation_id = ? AND request_id = ?""",
        (donation["id"], food_request["id"]),
    ).fetchone()
    if prior_allocation and prior_allocation["status"] == "collected":
        return None

    now = datetime.now()
    try:
        expires_at = parse_time(donation["expires_at"])
        deadline = parse_time(food_request["collection_deadline"])
    except (TypeError, ValueError):
        return None
    if expires_at <= now or deadline <= now or expires_at < deadline:
        return None
    donation_pickup_start = donation["pickup_start"]
    donation_pickup_end = donation["pickup_end"]
    request_pickup_start = food_request["pickup_start"]
    request_pickup_end = food_request["pickup_end"]
    if not all(
        (donation_pickup_start, donation_pickup_end, request_pickup_start, request_pickup_end)
    ):
        return None
    try:
        pickup_overlap_start = max(
            parse_time(donation_pickup_start), parse_time(request_pickup_start)
        )
        pickup_overlap_end = min(
            parse_time(donation_pickup_end), parse_time(request_pickup_end)
        )
    except (TypeError, ValueError):
        return None
    if pickup_overlap_start >= pickup_overlap_end:
        return None

    donated_tags = csv_values(donation["dietary_tags"])
    dietary_needs = csv_values(food_request["dietary_requirements"])
    allergens = csv_values(donation["allergens"])
    excluded_allergens = csv_values(food_request["excluded_allergens"])
    if not dietary_needs.issubset(donated_tags) or excluded_allergens.intersection(allergens):
        return None

    quantity_score = min(
        100.0,
        donation["remaining_quantity"] / food_request["remaining_quantity"] * 100,
    )
    compatibility_score = 100.0
    donor_city = db.execute(
        "SELECT city FROM locations WHERE user_id = ?",
        (donation["donor_id"],),
    ).fetchone()
    recipient_city = db.execute(
        "SELECT city FROM locations WHERE user_id = ?",
        (food_request["recipient_id"],),
    ).fetchone()
    proximity_score = (
        100.0
        if donor_city
        and recipient_city
        and donor_city["city"].casefold() == recipient_city["city"].casefold()
        else 35.0
    )

    hours_after_deadline = max(0.0, (expires_at - deadline).total_seconds() / 3600)
    timing_score = max(0.0, 100.0 - max(0.0, hours_after_deadline - 24) / 3.36)
    overlap_hours = (
        pickup_overlap_end - pickup_overlap_start
    ).total_seconds() / 3600
    timing_score = min(100.0, timing_score * 0.75 + min(100, overlap_hours * 10) * 0.25)
    urgency_score = food_request["urgency"] * 20.0
    recent_allocations = db.execute(
        """SELECT COUNT(*) AS allocation_count FROM allocations
           WHERE request_id IN (
               SELECT id FROM requests WHERE recipient_id = ?
           ) AND created_at >= datetime('now', '-30 days')""",
        (food_request["recipient_id"],),
    ).fetchone()["allocation_count"]
    fairness_score = 100.0 / (1 + recent_allocations)

    components = {
        "quantity": quantity_score,
        "compatibility": compatibility_score,
        "proximity": proximity_score,
        "timing": timing_score,
        "urgency": urgency_score,
        "fairness": fairness_score,
    }
    score = sum(components[key] * WEIGHTS[key] for key in WEIGHTS)
    weighted_components = {
        key: round(components[key] * WEIGHTS[key], 2) for key in WEIGHTS
    }
    explanation = (
        f"Category, quantity unit, dietary, and storage requirements are compatible; "
        f"{'same city' if proximity_score == 100 else 'different cities'}; "
        f"quantity fit {quantity_score:.0f}% {donation['quantity_unit']}; "
        f"urgency {food_request['urgency']}/5."
    )
    return {
        "score": round(score, 2),
        "components": components,
        "weighted_components": weighted_components,
        "explanation": explanation,
        "pickup_overlap_start": pickup_overlap_start,
        "pickup_overlap_end": pickup_overlap_end,
    }


def get_recommendations(db, donation=None, food_request=None, limit=None):
    if donation is None and food_request is None:
        return []

    if donation is not None and food_request is not None:
        pairs = [(donation, food_request)]
    elif donation is not None:
        requests = db.execute(
            """SELECT requests.*, recipients.organization_name AS recipient_name,
                      recipients.organization_type AS recipient_type
               FROM requests
               JOIN recipients ON recipients.user_id = requests.recipient_id
               WHERE requests.category = ? AND requests.quantity_unit = ?
                 AND requests.status IN ('open', 'partially_fulfilled')
               ORDER BY requests.urgency DESC, requests.created_at ASC""",
            (donation["category"], donation["quantity_unit"]),
        ).fetchall()
        pairs = [(donation, item) for item in requests]
    else:
        donations = db.execute(
            """SELECT donations.*, donors.organization_name AS donor_name,
                      donors.organization_type AS donor_type, donors.phone AS donor_phone,
                      locations.city AS donor_city
               FROM donations
               JOIN donors ON donors.user_id = donations.donor_id
               LEFT JOIN locations ON locations.user_id = donations.donor_id
               WHERE donations.category = ?
                 AND donations.quantity_unit = ?
                 AND donations.status IN ('available', 'partially_allocated')
               ORDER BY donations.created_at ASC""",
            (food_request["category"], food_request["quantity_unit"]),
        ).fetchall()
        pairs = [(item, food_request) for item in donations]

    results = []
    for available_food, request_item in pairs:
        result = score_match(db, available_food, request_item)
        if result is None:
            continue
        results.append(
            {
                "donation_id": available_food["id"],
                "request_id": request_item["id"],
                "food_name": available_food["food_name"],
                "category": available_food["category"],
                "available_quantity": available_food["remaining_quantity"],
                "requested_quantity": request_item["remaining_quantity"],
                "quantity_unit": available_food["quantity_unit"],
                "storage_method": available_food["storage_method"],
                "packaging": available_food["packaging"],
                "prepared_at": available_food["prepared_at"],
                "dietary_tags": available_food["dietary_tags"],
                "allergens": available_food["allergens"],
                "pickup_start": available_food["pickup_start"],
                "pickup_end": available_food["pickup_end"],
                "pickup_notes": available_food["pickup_notes"],
                "expires_at": available_food["expires_at"],
                "people_served": request_item["people_served"],
                "urgency": request_item["urgency"],
                "collection_deadline": request_item["collection_deadline"],
                "dietary_requirements": request_item["dietary_requirements"],
                "excluded_allergens": request_item["excluded_allergens"],
                "collection_notes": request_item["collection_notes"],
                "storage_preference": request_item["storage_preference"],
                "recipient_name": (
                    request_item["recipient_name"]
                    if "recipient_name" in request_item.keys()
                    else _recipient_name(db, request_item["recipient_id"])
                ),
                "recipient_type": (
                    request_item["recipient_type"]
                    if "recipient_type" in request_item.keys()
                    else _recipient_type(db, request_item["recipient_id"])
                ),
                "donor_type": (
                    available_food["donor_type"]
                    if "donor_type" in available_food.keys()
                    else _donor_type(db, available_food["donor_id"])
                ),
                "donor_phone": (
                    available_food["donor_phone"]
                    if "donor_phone" in available_food.keys()
                    else ""
                ),
                "pickup_overlap_start": (
                    result["pickup_overlap_start"].isoformat(timespec="minutes")
                    if result["pickup_overlap_start"] is not None
                    else None
                ),
                "pickup_overlap_end": (
                    result["pickup_overlap_end"].isoformat(timespec="minutes")
                    if result["pickup_overlap_end"] is not None
                    else None
                ),
                "city": (
                    available_food["donor_city"]
                    or _city_for_user(db, available_food["donor_id"])
                    if "donor_city" in available_food.keys()
                    else _city_for_user(db, available_food["donor_id"])
                ),
                "donor_name": (
                    available_food["donor_name"]
                    if "donor_name" in available_food.keys()
                    else _donor_name(db, available_food["donor_id"])
                ),
                "score": result["score"],
                "explanation": result["explanation"],
                "components": result["components"],
                "weighted_components": result["weighted_components"],
            }
        )
    results.sort(key=lambda item: (-item["score"], item["food_name"].casefold()))
    return results if limit is None else results[:limit]


def _city_for_user(db, user_id):
    row = db.execute("SELECT city FROM locations WHERE user_id = ?", (user_id,)).fetchone()
    return row["city"] if row else "Location not provided"


def _donor_name(db, user_id):
    row = db.execute(
        "SELECT organization_name FROM donors WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["organization_name"] if row else "Food donor"


def _donor_type(db, user_id):
    row = db.execute(
        "SELECT organization_type FROM donors WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["organization_type"] if row else "Food donor"


def _recipient_name(db, user_id):
    row = db.execute(
        "SELECT organization_name FROM recipients WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["organization_name"] if row else "Recipient organisation"


def _recipient_type(db, user_id):
    row = db.execute(
        "SELECT organization_type FROM recipients WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["organization_type"] if row else "Recipient organisation"
