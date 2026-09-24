"""Marks backend/audit/tests as a package. The suite is Postgres integration tests, each
isolating by actor_user_id and cleaning up via retention, except test_pii.py, which
needs no DB.
"""
