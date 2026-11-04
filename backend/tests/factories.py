"""Explicit fixture-only provider and trusted host identity composition."""
from dataclasses import replace
from pathlib import Path

from backend.app.application.dependencies import AgentDependencies
from backend.app.domain.identity import Principal
from backend.app.main import create_app as production_app
from backend.app.providers import LocalTravelDataPort, Providers
from backend.app.infrastructure.provider_cache import ProviderCache
from backend.app.storage import Store


class FixtureStore(Store):
    """Legacy test setup creates genuinely owned trips, never production legacy access."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixture_user = self.resolve_identity('fixture-host', 'default-user')

    def create_session(self, preference_id=None):
        with self.transaction():
            session = super().create_session(preference_id)
            self.assign_legacy_trip(session.id, self.fixture_user.user_id)
            return session


def fixture_principal(request):
    # Each app/store resolves a real user. No public identity header or global bypass.
    user = request.app.state.store.resolve_identity('fixture-host', 'default-user')
    return Principal(user.user_id, issuer='fixture-host', subject='default-user', operator=True)


def create_app(*, database_path=None, transport=None, **kwargs):
    dependencies = kwargs.get('dependencies') or AgentDependencies()
    kwargs['dependencies'] = replace(dependencies,
        repository_factory=dependencies.repository_factory or FixtureStore,
        identity_resolver=dependencies.identity_resolver or fixture_principal)
    if 'travel_data_factory' not in kwargs and dependencies.travel_data_factory is None:
        class FixtureDataPort(LocalTravelDataPort):
            async def aclose(self):
                self.providers.store.close()
        def data_factory(client):
            path = str(Path(database_path).with_suffix('.provider-fixture.sqlite3')) if database_path else ':memory:'
            return FixtureDataPort(Providers(client, ProviderCache(path)))
        kwargs['travel_data_factory'] = data_factory
    return production_app(database_path=database_path, transport=transport, **kwargs)
