from backend.app.application.quality_metrics import product_funnel
from backend.app.planner import Planner
from backend.app.storage import Store
import pytest


def test_funnel_has_finite_labels_and_does_not_copy_private_values():
    output = product_funnel({'messages_received': 4, 'simulated_checkouts': 2,
                             'private-user-id': 'secret', 'mean_first_recommendation_seconds': None},
                            {'completed': 3, 'canceled': 1, 'secret-job': 99})
    assert output['counts']['requests'] == 4
    assert output['counts']['simulated_receipts'] == 2
    assert output['mean_first_useful_seconds'] is None
    assert output['retained_commands']['completed'] == 3
    assert 'secret' not in str(output) and 'booking_conversion' not in output


@pytest.mark.asyncio
async def test_idempotent_command_does_not_inflate_request_or_action_denominators(tmp_path):
    store = Store(str(tmp_path / 'quality.db'))
    owner = store.create_user().user_id
    trip = store.create_owned_session(owner)
    job, _ = store.admit_message(owner, trip.id, 'Объясни', 'quality-command', 0, action='explain')
    same, duplicate = store.admit_message(owner, trip.id, 'Объясни', 'quality-command', 0, action='explain')
    assert same.id == job.id and duplicate
    await Planner(store, None, None).run(job.id)
    same, duplicate = store.admit_message(owner, trip.id, 'Объясни', 'quality-command', 0, action='explain')
    metrics = store.metrics()['product_funnel']
    assert metrics['counts']['requests'] == 1
    assert metrics['counts']['explanations'] == 1
    assert metrics['counts']['idempotent_replays'] == 2
    assert metrics['retained_commands']['completed'] == 1
    store.close()
