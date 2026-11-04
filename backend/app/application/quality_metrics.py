"""Finite product denominators; simulation is never booking conversion."""
TERMINAL_STATES = ('completed', 'failed', 'superseded', 'canceled', 'interrupted')


def product_funnel(counters, command_states):
    names = {
        'requests': 'messages_received', 'clarifications': 'clarifications',
        'recommendations': 'recommendations_completed', 'selections': 'options_selected',
        'simulated_receipts': 'simulated_checkouts', 'comparisons': 'actions_compare',
        'explanations': 'actions_explain', 'idempotent_replays': 'command_dedup_hits',
    }
    return {
        'counts': {label: counters.get(name, 0) for label, name in names.items()},
        'retained_commands': {state: command_states.get(state, 0)
                              for state in ('queued', 'running', *TERMINAL_STATES)},
        'first_useful_recommendations': counters.get('first_recommendations', 0),
        'mean_first_useful_seconds': counters.get('mean_first_recommendation_seconds'),
        'mean_completed_plan_seconds': counters.get('mean_recommendation_seconds'),
        'latency_policy': 'First useful and completed enrichment coincide in the current atomic plan publication.',
        'retention_policy': 'Counters expire after 90 inactive days; terminal commands after 30 days. Counts are not unique people.',
    }
