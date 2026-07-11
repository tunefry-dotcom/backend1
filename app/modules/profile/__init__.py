"""Profile bounded context — an artist's basic details.

Persists the profile fields collected on the frontend `/profile` page and exposes
a `is_complete` signal used to gate plan payment: a user must fill the required
basic details before they can pay to upgrade.
"""
