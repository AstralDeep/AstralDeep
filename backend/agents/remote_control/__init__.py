"""Mutating remote-compute verb library (feature 063).

These verbs are no longer their own agent: they are unioned into the single
remote-compute-1 agent (agents.remote_compute). Every destructive verb still
routes through the US3 confirmation gate before it can act, keyed on that agent's
id — merging the agents did not merge the safety classes."""
