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
    if food_request["status"] not in ("open", "partially_fulfilled"):
        return None
    if donation["category"].casefold() != food_request["category"].casefold():
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
    explanation = (
        f"Category and dietary requirements are compatible; "
        f"{'same city' if proximity_score == 100 else 'different cities'}; "
        f"quantity fit {quantity_score:.0f}%; urgency {food_request['urgency']}/5."
    )
    return {
        "score": round(score, 2),
        "components": components,
        "explanation": explanation,
    }


def get_recommendations(db, donation=None, food_request=None, limit=None):
    if donation is None and food_request is None:
        return []

    if donation is not None and food_request is not None:
        pairs = [(donation, food_request)]
    elif donation is not None:
        requests = db.execute(
            """SELECT * FROM requests
               WHERE category = ? AND status IN ('open', 'partially_fulfilled')
               ORDER BY urgency DESC, created_at ASC""",
            (donation["category"],),
        ).fetchall()
        pairs = [(donation, item) for item in requests]
    else:
        donations = db.execute(
            """SELECT donations.*, donors.organization_name AS donor_name,
                      locations.city AS donor_city
               FROM donations
               JOIN donors ON donors.user_id = donations.donor_id
               LEFT JOIN locations ON locations.user_id = donations.donor_id
               WHERE donations.category = ?
                 AND donations.status IN ('available', 'partially_allocated')
               ORDER BY donations.created_at ASC""",
            (food_request["category"],),
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
